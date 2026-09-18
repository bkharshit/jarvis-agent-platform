"""SqlKnowledgeRepo integration (S8, ADR 0020 §1, D63): dataset/document/
chunk round-trip, strict tenancy, cosine search ranking (verifiable with
deterministic vectors), the snapshot-reference 409 refusal, the document
cascade, and the dimension mismatch's honest SQL failure."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jarvis.domain.agent import (
    AgentDefinition,
    KnowledgeBinding,
    ModelRef,
    StrategyConfig,
)
from jarvis.domain.knowledge import (
    ChunkConfig,
    EmbeddingConfig,
    KnowledgeChunk,
    KnowledgeDataset,
    KnowledgeDocument,
)
from jarvis.persistence.repositories import (
    SqlAgentRepo,
    SqlAuthRepo,
    SqlKnowledgeRepo,
)

pytestmark = pytest.mark.db

TENANT = "default"
OTHER_TENANT = "tenant-b"


def _dataset(name: str, *, dimension: int = 4) -> KnowledgeDataset:
    return KnowledgeDataset(
        id=str(uuid4()),
        name=name,
        embedding_config=EmbeddingConfig(
            provider="mock", model="mock-embedder", dimension=dimension
        ),
        chunk_config=ChunkConfig(),
    )


def _doc(dataset: KnowledgeDataset, content: str = "hello") -> KnowledgeDocument:
    return KnowledgeDocument(
        id=str(uuid4()),
        dataset_id=dataset.id,
        name="doc",
        content=content,
        chunk_count=1,
    )


def _chunk(document: KnowledgeDocument, index: int, embedding: list[float]) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=str(uuid4()),
        document_id=document.id,
        dataset_id=document.dataset_id,
        chunk_index=index,
        content=f"chunk-{index}",
        embedding=embedding,
    )


@pytest.fixture
def repo(container) -> SqlKnowledgeRepo:
    return SqlKnowledgeRepo(async_sessionmaker(container.engine, expire_on_commit=False))


@pytest.fixture
def make_tenant(container):
    async def go(tenant_id: str) -> None:
        auth = SqlAuthRepo(async_sessionmaker(container.engine, expire_on_commit=False))
        await auth.create_tenant(tenant_id, tenant_id)

    return go


async def test_dataset_roundtrip(repo: SqlKnowledgeRepo) -> None:
    dataset = _dataset("docs")
    created = await repo.create_dataset(dataset, tenant_id=TENANT)
    loaded = await repo.get_dataset(dataset.id, tenant_id=TENANT)
    assert loaded is not None
    assert loaded.embedding_config.dimension == 4
    assert loaded == created


async def test_datasets_are_strictly_tenant_scoped(repo: SqlKnowledgeRepo, make_tenant) -> None:
    await make_tenant(OTHER_TENANT)
    dataset = await repo.create_dataset(_dataset("mine"), tenant_id=TENANT)
    assert await repo.get_dataset(dataset.id, tenant_id=OTHER_TENANT) is None
    assert await repo.list_datasets(tenant_id=OTHER_TENANT) == []


async def test_names_unique_per_tenant(repo: SqlKnowledgeRepo) -> None:
    await repo.create_dataset(_dataset("dupe"), tenant_id=TENANT)
    with pytest.raises(IntegrityError):
        await repo.create_dataset(_dataset("dupe"), tenant_id=TENANT)


async def test_update_writes_only_mutable_fields(repo: SqlKnowledgeRepo) -> None:
    dataset = await repo.create_dataset(_dataset("mutable"), tenant_id=TENANT)
    # The caller passes the full domain object (wholesale PATCH) — but the
    # repo must write name/description ONLY, so a mutated config is a no-op
    # (the immutability guarantee does not live in the caller's honesty).
    drifted = dataset.model_copy(deep=True)
    drifted.embedding_config = EmbeddingConfig(provider="x", model="y", dimension=9)
    drifted.name = "renamed"
    updated = await repo.update_dataset(drifted, tenant_id=TENANT)
    assert updated is not None
    assert updated.name == "renamed"
    assert updated.embedding_config == dataset.embedding_config
    assert updated.chunk_config == dataset.chunk_config


async def test_document_scoping_and_cascade(repo: SqlKnowledgeRepo, make_tenant, container) -> None:
    await make_tenant(OTHER_TENANT)
    dataset = await repo.create_dataset(_dataset("with-docs"), tenant_id=TENANT)
    document = _doc(dataset)
    await repo.create_document(document, [_chunk(document, 0, [1.0, 0, 0, 0])], tenant_id=TENANT)

    assert (await repo.list_documents(dataset.id, tenant_id=TENANT))[0].id == document.id
    assert await repo.list_documents(dataset.id, tenant_id=OTHER_TENANT) == []
    assert await repo.get_document(document.id, dataset.id, tenant_id=OTHER_TENANT) is None
    assert await repo.delete_document(document.id, dataset.id, tenant_id=OTHER_TENANT) is False

    assert await repo.delete_document(document.id, dataset.id, tenant_id=TENANT) is True
    assert await repo.list_documents(dataset.id, tenant_id=TENANT) == []
    # The chunk cascaded with the document (FK ON DELETE CASCADE).
    engine = async_sessionmaker(container.engine, expire_on_commit=False)
    async with engine() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM knowledge_chunks WHERE document_id = :d"),
                {"d": document.id},
            )
        ).scalar_one()
    assert count == 0


async def test_create_document_refuses_foreign_dataset(repo: SqlKnowledgeRepo, make_tenant) -> None:
    await make_tenant(OTHER_TENANT)
    dataset = await repo.create_dataset(_dataset("foreign"), tenant_id=TENANT)
    document = _doc(dataset)
    # The route resolves the dataset scoped first, so this is
    # defense-in-depth — but the repo refuses the foreign write honestly.
    with pytest.raises(ValueError):
        await repo.create_document(
            document, [_chunk(document, 0, [1.0, 0, 0, 0])], tenant_id=OTHER_TENANT
        )


async def test_search_ranks_by_cosine(repo: SqlKnowledgeRepo) -> None:
    dataset = await repo.create_dataset(_dataset("ranked"), tenant_id=TENANT)
    document = _doc(dataset)
    # Deterministic 4-dim vectors: the [1,0,0,0] chunk is the best match
    # for the query; [0,1,0,0] second; the rest never surface.
    await repo.create_document(
        document,
        [
            _chunk(document, 0, [1.0, 0.0, 0.0, 0.0]),
            _chunk(document, 1, [0.0, 1.0, 0.0, 0.0]),
            _chunk(document, 2, [0.0, 0.0, 1.0, 0.0]),
        ],
        tenant_id=TENANT,
    )

    hits = await repo.search_chunks([dataset.id], [1.0, 0.0, 0.0, 0.0], limit=2)
    assert [chunk.chunk_index for chunk in hits] == [0, 1]
    assert hits[0].content == "chunk-0"

    other = await repo.search_chunks([dataset.id], [0.0, 0.0, 1.0, 0.0], limit=1)
    assert [chunk.chunk_index for chunk in other] == [2]

    assert await repo.search_chunks([], [1.0, 0.0, 0.0, 0.0], limit=5) == []


async def test_search_refuses_dimension_mismatch(repo: SqlKnowledgeRepo) -> None:
    dataset = await repo.create_dataset(_dataset("pinned"), tenant_id=TENANT)
    document = _doc(dataset)
    await repo.create_document(document, [_chunk(document, 0, [1.0, 0, 0, 0])], tenant_id=TENANT)
    # pgvector's "different vector dimensions 4 and 2" — asyncpg raises
    # DataError, but SQLAlchemy's asyncpg translation surfaces it as a base
    # DBAPIError (the sqlstate 22xxx class is not mapped to DataError).
    with pytest.raises(DBAPIError):
        await repo.search_chunks([dataset.id], [1.0, 0.0], limit=1)


async def test_delete_refused_while_snapshots_reference(repo: SqlKnowledgeRepo, container) -> None:
    agents = SqlAgentRepo(async_sessionmaker(container.engine, expire_on_commit=False))
    dataset = await repo.create_dataset(_dataset("bound"), tenant_id=TENANT)
    await agents.create(
        AgentDefinition(
            id="a-knowledge-ref",
            name="knowledge-ref",
            model=ModelRef(provider="mock", model="m"),
            strategy=StrategyConfig(type="function_calling"),
            knowledge=KnowledgeBinding(datasets=[dataset.id]),
        )
    )
    assert await repo.count_dataset_references(dataset.id) >= 1
    assert await repo.delete_dataset(dataset.id, tenant_id=TENANT) is False
