"""Knowledge routes (S8, ADR 0020 §6): dataset CRUD and synchronous
document ingest — the eval-dataset precedent (first-class tenant resources,
wholesale PATCH, config immutability, delete-while-referenced 409).

Ingest is SYNCHRONOUS (D65): chunk → embed → persist document + chunks in
one transaction. An embedder failure persists an honest `error` document
row and answers 502 embedding_unavailable — the row is the receipt (D66's
honesty discipline), never a silent 201."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError
from jarvis.api.routes._run_routes import ContainerDep
from jarvis.api.schemas import (
    KnowledgeDatasetCreate,
    KnowledgeDatasetList,
    KnowledgeDatasetUpdate,
    KnowledgeDocumentCreate,
    KnowledgeDocumentList,
)
from jarvis.domain.knowledge import KnowledgeChunk, KnowledgeDataset, KnowledgeDocument
from jarvis.models.errors import ModelError
from jarvis.models.types import EmbeddingRequest
from jarvis.runtime.knowledge.chunking import chunk_text
from jarvis.runtime.knowledge.retrieval import RetrievalError

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

__all__ = ["router"]


async def _visible_dataset(
    container: AppContainer, dataset_id: str, tenant_id: str
) -> KnowledgeDataset:
    dataset = await container.knowledge.get_dataset(dataset_id, tenant_id=tenant_id)
    if dataset is None:
        raise ApiError(404, "not_found", f"knowledge dataset {dataset_id!r} not found")
    return dataset


@router.get("/datasets")
async def list_datasets(
    auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> KnowledgeDatasetList:
    items = await container.knowledge.list_datasets(tenant_id=auth.principal.tenant_id)
    return KnowledgeDatasetList(items=items)


@router.post("/datasets", status_code=201)
async def create_dataset(
    req: KnowledgeDatasetCreate,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> KnowledgeDataset:
    dataset = KnowledgeDataset(
        id=str(uuid4()),
        name=req.name,
        description=req.description,
        embedding_config=req.embedding_config,
        chunk_config=req.chunk_config,
    )
    created = await container.knowledge.create_dataset(dataset, tenant_id=auth.principal.tenant_id)
    return created


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> KnowledgeDataset:
    return await _visible_dataset(container, dataset_id, auth.principal.tenant_id)


@router.patch("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: str,
    req: KnowledgeDatasetUpdate,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> KnowledgeDataset:
    existing = await _visible_dataset(container, dataset_id, auth.principal.tenant_id)
    updated = existing.model_copy(update={"name": req.name, "description": req.description})
    result = await container.knowledge.update_dataset(updated, tenant_id=auth.principal.tenant_id)
    if result is None:
        raise ApiError(404, "not_found", f"knowledge dataset {dataset_id!r} not found")
    return result


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> None:
    await _visible_dataset(container, dataset_id, auth.principal.tenant_id)
    deleted = await container.knowledge.delete_dataset(
        dataset_id, tenant_id=auth.principal.tenant_id
    )
    if not deleted:
        references = await container.knowledge.count_dataset_references(dataset_id)
        raise ApiError(
            409,
            "conflict",
            f"knowledge dataset {dataset_id!r} is referenced by {references} agent "
            "version snapshot(s); delete refused",
        )


@router.get("/datasets/{dataset_id}/documents")
async def list_documents(
    dataset_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> KnowledgeDocumentList:
    await _visible_dataset(container, dataset_id, auth.principal.tenant_id)
    items = await container.knowledge.list_documents(dataset_id, tenant_id=auth.principal.tenant_id)
    return KnowledgeDocumentList(items=items)


@router.post("/datasets/{dataset_id}/documents", status_code=201)
async def create_document(
    dataset_id: str,
    req: KnowledgeDocumentCreate,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> KnowledgeDocument:
    """Synchronous ingest (D65): chunk → embed → persist document + chunks
    in ONE transaction. An embedder failure persists an honest `error` row
    and answers 502 — never a silent 201."""
    dataset = await _visible_dataset(container, dataset_id, auth.principal.tenant_id)
    if not req.content.strip():
        raise ApiError(422, "validation", "document content must not be blank")
    if len(req.content) > container.settings.knowledge_max_doc_chars:
        raise ApiError(
            422,
            "validation",
            f"document content exceeds the {container.settings.knowledge_max_doc_chars} "
            "character cap",
        )
    config = dataset.embedding_config
    blocks = chunk_text(req.content, dataset.chunk_config)
    try:
        embedder = await container.models.resolve_embedder(config, principal=auth.principal)
        vectors: list[list[float]] = []
        batch_size = container.settings.knowledge_embed_batch
        for start in range(0, len(blocks), batch_size):
            batch = blocks[start : start + batch_size]
            response = await embedder.embed(EmbeddingRequest(model=config.model, input=batch))
            if len(response.vectors) != len(batch):
                raise RetrievalError(
                    f"embedder returned {len(response.vectors)} vectors for {len(batch)} inputs"
                )
            for vector in response.vectors:
                if len(vector) != config.dimension:
                    raise RetrievalError(
                        f"embedding dimension {len(vector)} does not match the "
                        f"dataset's pinned dimension {config.dimension}"
                    )
            vectors.extend(response.vectors)
    except (ModelError, RetrievalError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        await container.knowledge.create_document(
            KnowledgeDocument(
                id=str(uuid4()),
                dataset_id=dataset.id,
                name=req.name,
                content=req.content,
                status="error",
                error=error,
            ),
            [],
            tenant_id=auth.principal.tenant_id,
        )
        raise ApiError(502, "embedding_unavailable", f"document ingest failed: {error}") from None
    document = KnowledgeDocument(
        id=str(uuid4()),
        dataset_id=dataset.id,
        name=req.name,
        content=req.content,
        status="ready",
        chunk_count=len(blocks),
    )
    chunks = [
        KnowledgeChunk(
            id=str(uuid4()),
            document_id=document.id,
            dataset_id=dataset.id,
            chunk_index=index,
            content=block,
            embedding=vectors[index],
        )
        for index, block in enumerate(blocks)
    ]
    return await container.knowledge.create_document(
        document, chunks, tenant_id=auth.principal.tenant_id
    )


@router.get("/datasets/{dataset_id}/documents/{document_id}")
async def get_document(
    dataset_id: str,
    document_id: str,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> KnowledgeDocument:
    await _visible_dataset(container, dataset_id, auth.principal.tenant_id)
    document = await container.knowledge.get_document(
        document_id, dataset_id, tenant_id=auth.principal.tenant_id
    )
    if document is None:
        raise ApiError(404, "not_found", f"document {document_id!r} not found")
    return document


@router.delete("/datasets/{dataset_id}/documents/{document_id}", status_code=204)
async def delete_document(
    dataset_id: str,
    document_id: str,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> None:
    await _visible_dataset(container, dataset_id, auth.principal.tenant_id)
    deleted = await container.knowledge.delete_document(
        document_id, dataset_id, tenant_id=auth.principal.tenant_id
    )
    if not deleted:
        raise ApiError(404, "not_found", f"document {document_id!r} not found")
