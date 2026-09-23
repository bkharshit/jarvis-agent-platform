"""S8 commit 6 — the agents routes' knowledge validation (ADR 0020 §4/§5,
D66/D67): the publish-time 422s (missing/foreign datasets, mixed embedding
setups, retriever bindings without a resolvable dataset_id). The route
helpers are tested directly against a fake repo view — the HTTP layer is
the integration suite's."""

from types import SimpleNamespace

import pytest

from jarvis.api.errors import ApiError
from jarvis.api.routes.agents import _validate_knowledge
from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.knowledge import EmbeddingConfig, KnowledgeDataset

# --- the doubles ---------------------------------------------------------------


def _dataset(dataset_id="ds-1", *, dimension=8, model="mock-embed"):
    return KnowledgeDataset(
        id=dataset_id,
        name=dataset_id,
        embedding_config=EmbeddingConfig(
            provider="mock", model=model, base_url=None, dimension=dimension
        ),
    )


class _FakeRepo:
    def __init__(self, datasets=()):
        self._datasets = {d.id: d for d in datasets}

    async def get_dataset(self, dataset_id, *, tenant_id):
        return self._datasets.get(dataset_id)


def _auth():
    return SimpleNamespace(principal=SimpleNamespace(tenant_id="t1"))


def _container(repo):
    return SimpleNamespace(knowledge=repo)


def _definition(**kw):
    base = dict(
        id="a1",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
    )
    base.update(kw)
    return AgentDefinition(**base)


# --- the knowledge binding (D66) ------------------------------------------------


class TestKnowledgeBinding:
    async def test_valid_binding_passes(self):
        repo = _FakeRepo([_dataset()])
        definition = _definition(knowledge={"datasets": ["ds-1"], "top_k": 2})

        await _validate_knowledge(_container(repo), _auth(), definition)

    async def test_missing_dataset_is_a_422(self):
        repo = _FakeRepo()
        definition = _definition(knowledge={"datasets": ["ds-gone"]})

        with pytest.raises(ApiError) as exc:
            await _validate_knowledge(_container(repo), _auth(), definition)
        assert exc.value.status_code == 422
        assert "ds-gone" in exc.value.message
        assert "does not exist" in exc.value.message

    async def test_mixed_embedding_setups_are_a_422(self):
        """One query embedding must serve every bound dataset — a binding
        across two signatures is unanswerable by construction."""
        repo = _FakeRepo([_dataset("ds-1"), _dataset("ds-2", dimension=16)])
        definition = _definition(knowledge={"datasets": ["ds-1", "ds-2"]})

        with pytest.raises(ApiError) as exc:
            await _validate_knowledge(_container(repo), _auth(), definition)
        assert exc.value.status_code == 422
        assert "mixes embedding setups" in exc.value.message

    async def test_same_model_through_two_datasets_passes(self):
        repo = _FakeRepo([_dataset("ds-1"), _dataset("ds-2")])
        definition = _definition(knowledge={"datasets": ["ds-1", "ds-2"]})

        await _validate_knowledge(_container(repo), _auth(), definition)


# --- the retriever tool bindings (D67) -------------------------------------------


class TestRetrieverBindings:
    async def test_valid_retriever_binding_passes(self):
        repo = _FakeRepo([_dataset()])
        definition = _definition(
            tools=[ToolBinding(name="retriever__ds-1", config={"dataset_id": "ds-1"})]
        )

        await _validate_knowledge(_container(repo), _auth(), definition)

    async def test_retriever_binding_without_dataset_id_is_a_422(self):
        repo = _FakeRepo([_dataset()])
        definition = _definition(tools=[ToolBinding(name="retriever__ds-1")])

        with pytest.raises(ApiError) as exc:
            await _validate_knowledge(_container(repo), _auth(), definition)
        assert exc.value.status_code == 422
        assert "dataset_id" in exc.value.message

    async def test_retriever_binding_unknown_dataset_is_a_422(self):
        repo = _FakeRepo()
        definition = _definition(
            tools=[ToolBinding(name="retriever__ds-x", config={"dataset_id": "ds-x"})]
        )

        with pytest.raises(ApiError) as exc:
            await _validate_knowledge(_container(repo), _auth(), definition)
        assert exc.value.status_code == 422
        assert "retriever__ds-x" in exc.value.message

    async def test_no_knowledge_at_all_passes(self):
        await _validate_knowledge(_container(_FakeRepo()), _auth(), _definition())
