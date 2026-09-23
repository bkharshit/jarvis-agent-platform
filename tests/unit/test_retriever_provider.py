"""The retriever tool family provider (S8, ADR 0020 §5, D67): the binding's
identity comes from its config's dataset_id — NEVER parsed from the name
(the S14 rule) — tenant-scoped resolution, the top_k knob, and the
RetrieverResolutionError paths. The HTTP/e2e paths are the integration
suite's."""

from types import SimpleNamespace

import pytest

from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.knowledge import EmbeddingConfig, KnowledgeDataset
from jarvis.tools.knowledge.provider import (
    RETRIEVER_PREFIX,
    RetrieverToolProvider,
    is_retriever_binding,
)
from jarvis.tools.mcp.errors import RetrieverResolutionError

# --- the doubles ---------------------------------------------------------------


def _dataset(dataset_id="ds-1", name="Notes"):
    return KnowledgeDataset(
        id=dataset_id,
        name=name,
        embedding_config=EmbeddingConfig(provider="mock", model="mock-embed", dimension=8),
    )


class _FakeRepo:
    def __init__(self, datasets=()):
        self._datasets = {d.id: d for d in datasets}
        self.fetched: list[tuple[str, str]] = []

    async def get_dataset(self, dataset_id, *, tenant_id):
        self.fetched.append((dataset_id, tenant_id))
        return self._datasets.get(dataset_id)


def _ctx():
    return ExecutionContext(run_id="r1", agent_id="a1", agent_version_id="v1", tenant_id="t1")


def _agent():
    return AgentDefinition(
        id="a1",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
    )


def _provider(repo=None):
    return RetrieverToolProvider(repo=repo or _FakeRepo(), retrieval=SimpleNamespace())


# --- the tests -----------------------------------------------------------------


class TestPrefix:
    def test_prefix_detection(self):
        assert RETRIEVER_PREFIX == "retriever__"
        assert is_retriever_binding("retriever__ds-1")
        assert not is_retriever_binding("calculator")
        assert not is_retriever_binding("mcp__server")
        assert not is_retriever_binding("sub__agent")


class TestResolve:
    async def test_identity_comes_from_config_not_name(self):
        """The S14 rule, applied (D67): the config's dataset_id names the
        dataset, whatever the binding's text says."""
        repo = _FakeRepo([_dataset()])
        provider = _provider(repo)
        binding = ToolBinding(name="retriever__wibble", config={"dataset_id": "ds-1"})

        tooling = await provider.resolve([binding], _agent(), _ctx())

        tool = tooling.registry.get("retriever__wibble")
        assert tool is not None
        assert "Notes" in tool.descriptor.description
        assert repo.fetched == [("ds-1", "t1")]

    async def test_missing_dataset_id_config_is_an_error(self):
        provider = _provider()
        binding = ToolBinding(name="retriever__ds-1")

        with pytest.raises(RetrieverResolutionError) as exc:
            await provider.resolve([binding], _agent(), _ctx())
        assert "dataset_id" in str(exc.value)

    async def test_unknown_dataset_names_the_binding(self):
        provider = _provider(_FakeRepo())
        binding = ToolBinding(name="retriever__ds-x", config={"dataset_id": "ds-x"})

        with pytest.raises(RetrieverResolutionError) as exc:
            await provider.resolve([binding], _agent(), _ctx())
        assert "retriever__ds-x" in str(exc.value)
        assert "not found" in str(exc.value)

    async def test_top_k_knob_validated(self):
        repo = _FakeRepo([_dataset()])
        provider = _provider(repo)
        binding = ToolBinding(
            name="retriever__ds-1", config={"dataset_id": "ds-1", "top_k": "seven"}
        )

        with pytest.raises(RetrieverResolutionError) as exc:
            await provider.resolve([binding], _agent(), _ctx())
        assert "top_k" in str(exc.value)

    async def test_valid_top_k_resolves(self):
        repo = _FakeRepo([_dataset()])
        provider = _provider(repo)
        binding = ToolBinding(name="retriever__ds-1", config={"dataset_id": "ds-1", "top_k": 3})

        tooling = await provider.resolve([binding], _agent(), _ctx())
        assert tooling.registry.get("retriever__ds-1") is not None

    async def test_non_retriever_bindings_are_skipped(self):
        provider = _provider()
        binding = ToolBinding(name="calculator", config={})

        tooling = await provider.resolve([binding], _agent(), _ctx())
        assert tooling.registry.descriptors() == []
