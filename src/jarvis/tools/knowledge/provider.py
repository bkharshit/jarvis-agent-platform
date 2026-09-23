"""The retriever tool family (S8, ADR 0020 §5, D67) — `retriever__<dataset>`.

One tool per bound dataset: the binding's config carries the dataset
identity (`dataset_id` — never parsed from the name, the S14 rule) and
the `top_k` knob; eager per-segment resolution resolves it TENANT-SCOPED
(D29: a foreign id reads as absent), the tool answers with the D68
one-call contract via the shared KnowledgeRetrieval service.
Retrieval failures surface as `RetrieverResolutionError` — a
ToolResolutionError — so the runtime's existing catch persists the
terminal `tool` state; resolution failures here name the dataset."""

from __future__ import annotations

from typing import Any

from jarvis.domain.agent import AgentDefinition, ToolBinding
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.knowledge import KnowledgeDataset
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.persistence.repositories import SqlKnowledgeRepo
from jarvis.runtime.knowledge.retrieval import KnowledgeRetrieval
from jarvis.tools.base import BaseTool
from jarvis.tools.mcp.errors import RetrieverResolutionError
from jarvis.tools.mcp.provider import McpTooling
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime

RETRIEVER_PREFIX = "retriever__"

_RETRIEVE_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The search query — what to look up in the knowledge base",
        }
    },
    "required": ["query"],
}


def is_retriever_binding(name: str) -> bool:
    """True for any `retriever__` binding (the publish validation keys off
    this, never the dataset-id text)."""
    return name.startswith(RETRIEVER_PREFIX)


class RetrieverToolProvider:
    """The runtime's retriever seam, beside MCP and sub-agents (D67).
    Resolution is eager per segment; the tool carries the run-scoped
    tenant/principal captured here."""

    def __init__(self, *, repo: SqlKnowledgeRepo, retrieval: KnowledgeRetrieval) -> None:
        self._repo = repo
        self._retrieval = retrieval

    async def resolve(
        self, bindings: list[ToolBinding], agent: AgentDefinition, ctx: ExecutionContext
    ) -> McpTooling:
        """Mint one RetrieverTool per enabled `retriever__*` binding. The
        binding's identity is its config's `dataset_id` — never parsed
        from the name (the S14 rule) — resolved TENANT-SCOPED here (D29:
        a foreign dataset reads as absent); a dataset that doesn't
        resolve raises RetrieverResolutionError naming the binding."""
        tools: list[BaseTool] = []
        for tool_binding in bindings:
            if not is_retriever_binding(tool_binding.name):
                continue
            # Identity is the config's dataset_id — NEVER parsed from the
            # name (the S14 rule, D67).
            dataset_id = tool_binding.config.get("dataset_id")
            if not isinstance(dataset_id, str) or not dataset_id:
                raise RetrieverResolutionError(
                    tool_binding.name,
                    "binding carries no dataset_id in its config — the identity "
                    "is the config's dataset_id, never the name",
                )
            dataset = await self._repo.get_dataset(dataset_id, tenant_id=ctx.tenant_id or "")
            if dataset is None:
                raise RetrieverResolutionError(
                    tool_binding.name, "dataset not found in this tenant"
                )
            top_k = tool_binding.config.get("top_k")
            bad_top_k = top_k is not None and (
                isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1
            )
            if bad_top_k:
                raise RetrieverResolutionError(
                    tool_binding.name, f"config top_k {top_k!r} is not a positive integer"
                )
            tools.append(
                RetrieverTool(
                    self._descriptor(tool_binding.name, dataset.name),
                    retrieval=self._retrieval,
                    dataset=dataset,
                    tenant_id=ctx.tenant_id or "",
                    principal=ctx.principal,
                    top_k=top_k,
                    min_score=None,
                )
            )
        registry = InMemoryToolRegistry()
        for tool in tools:
            registry.register(tool)
        return McpTooling(
            registry=registry,
            tool_runtime=ToolRuntime(registry),
            aclose=_no_op_close,
        )

    @staticmethod
    def _descriptor(name: str, dataset_name: str) -> ToolDescriptor:
        return ToolDescriptor(
            name=name,
            description=(
                f"Retrieve relevant excerpts from the {dataset_name!r} knowledge "
                "dataset. Pass the search query."
            ),
            parameters=_RETRIEVE_PARAMETERS,
        )


async def _no_op_close() -> None:
    """The retriever holds no connections — nothing to close."""


class RetrieverTool(BaseTool):
    """`retriever__<dataset>` — one D68 call, rendered context out."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        *,
        retrieval: KnowledgeRetrieval,
        dataset: KnowledgeDataset,
        tenant_id: str,
        principal: Any,
        top_k: int | None,
        min_score: float | None,
    ) -> None:
        super().__init__(descriptor)
        self._retrieval = retrieval
        self._dataset = dataset
        self._tenant_id = tenant_id
        self._principal = principal
        self._top_k = top_k
        self._min_score = min_score

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("retriever tool requires a non-empty 'query' string")
        rendered = await self._retrieval.retrieve(
            [self._dataset],
            query,
            tenant_id=self._tenant_id,
            principal=self._principal,
            top_k=self._top_k,
            min_score=self._min_score,
        )
        # An honest miss beats an empty tool output — the model can act on it.
        return rendered or "no relevant knowledge found for this query"


__all__ = ["RETRIEVER_PREFIX", "RetrieverToolProvider", "is_retriever_binding"]
