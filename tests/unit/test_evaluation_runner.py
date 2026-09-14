"""llm_judge + scoring runner + eval-run creation service (S11, D50)."""

import asyncio

import pytest

from jarvis.api.errors import ApiError
from jarvis.api.evaluation_service import create_eval_run, validate_dataset_scorers
from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig
from jarvis.domain.auth import Principal
from jarvis.domain.evaluation import EvalCase, EvalDataset, EvalObservation
from jarvis.domain.message import Message
from jarvis.models.errors import ModelError
from jarvis.models.types import ModelRequest, ModelResponse
from jarvis.ports.queue import RunQueueMessage
from jarvis.scoring.judge import LlmJudgeScorer
from jarvis.scoring.runner import score_result


def _dataset(**overrides: object) -> EvalDataset:
    base: dict[str, object] = {
        "id": "ds-1",
        "name": "smoke",
        "cases": [
            {"id": "c-1", "input": "what is 2+2?", "expected": "4"},
            {"id": "c-2", "input": "capital of France?", "expected": "Paris"},
        ],
        "scorers": [{"name": "exact"}],
    }
    base.update(overrides)
    return EvalDataset.model_validate(base)


def _obs(final_message: str = "4") -> EvalObservation:
    return EvalObservation(status="succeeded", final_message=final_message)


class _JudgeClient:
    """A resolved ModelClient fake: scripted verdict replies."""

    def __init__(self, replies: str | list[str] | Exception) -> None:
        self._replies = [replies] if isinstance(replies, str) else replies
        self.requests: list[ModelRequest] = []
        self.ref = ModelRef(provider="mock", model="judge-1")

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if isinstance(self._replies, Exception):
            raise self._replies
        reply = self._replies.pop(0)
        return ModelResponse(message=Message(role="assistant", content=reply))


class _JudgeFactory:
    """ModelProviderFactory stand-in resolving to a scripted client."""

    def __init__(self, client: object) -> None:
        self._client = client
        self.resolved = 0

    async def resolve(self, ref: object, *, principal: object = None) -> object:
        self.resolved += 1
        return self._client


class TestJudgeScorer:
    def _score(self, reply: str | list[str] | Exception) -> tuple[_JudgeClient, object]:
        client = _JudgeClient(reply)
        score = asyncio.run(
            LlmJudgeScorer(client).score(
                EvalCase(id="c", input="what is 2+2?", expected="4"), _obs()
            )
        )
        return client, score

    def test_pass_verdict(self) -> None:
        client, score = self._score("VERDICT: PASS\nREASON: answer is correct")
        assert score.passed is True and score.score == 1.0
        assert "correct" in (score.detail or "")
        # the judge call carries the case + answer, temperature 0
        req = client.requests[0]
        assert req.temperature == 0.0
        assert "what is 2+2?" in req.messages[1].content

    def test_fail_verdict_and_case_insensitive(self) -> None:
        _, score = self._score("verdict: fail\nreason: wrong city")
        assert score.passed is False and "wrong city" in (score.detail or "")

    def test_no_verdict_is_honest(self) -> None:
        _, score = self._score("The answer looks correct!")
        assert score.passed is None and "VERDICT" in (score.detail or "")

    def test_model_error_is_honest(self) -> None:
        _, score = self._score(ModelError("judge down"))
        assert score.passed is None and "judge down" in (score.detail or "")

    def test_reason_spanning_lines_is_captured(self) -> None:
        _, score = self._score("VERDICT: FAIL\nREASON: expected Paris\nbut got Lyon")
        assert "but got Lyon" in (score.detail or "")


class TestScoreResult:
    def test_mixed_scorers_run_in_order(self) -> None:
        ds = _dataset(
            scorers=[{"name": "exact"}, {"name": "llm_judge"}],
            judge_model={"provider": "mock", "model": "judge-1"},
        )
        factory = _JudgeFactory(_JudgeClient("VERDICT: PASS\nREASON: ok"))
        scores = asyncio.run(
            score_result(
                ds, EvalCase(id="c", input="q", expected="4"), _obs(), model_factory=factory
            )
        )
        assert [s.scorer for s in scores] == ["exact", "llm_judge"]
        assert scores[0].passed is True and scores[1].passed is True
        assert factory.resolved == 1

    def test_llm_judge_without_judge_model_is_no_verdict(self) -> None:
        ds = _dataset(scorers=[{"name": "llm_judge"}])
        scores = asyncio.run(score_result(ds, EvalCase(id="c", input="q"), _obs()))
        assert scores[0].passed is None and "unavailable" in (scores[0].detail or "")

    def test_resolution_failure_is_honest(self) -> None:
        class _FailingFactory:
            async def resolve(self, ref: object, *, principal: object = None) -> object:
                raise ModelError("no credential")

        ds = _dataset(
            scorers=[{"name": "llm_judge"}], judge_model={"provider": "mock", "model": "judge-1"}
        )
        scores = asyncio.run(
            score_result(ds, EvalCase(id="c", input="q"), _obs(), model_factory=_FailingFactory())
        )
        assert scores[0].passed is None and "resolution failed" in (scores[0].detail or "")

    def test_deterministic_scorers_need_no_factory(self) -> None:
        scores = asyncio.run(
            score_result(_dataset(), EvalCase(id="c", input="q", expected="4"), _obs())
        )
        assert scores[0].passed is True


def _version(agent_id: str) -> AgentVersion:
    return AgentVersion(
        id="ver-9",
        agent_id=agent_id,
        version=3,
        snapshot=AgentDefinition(
            id=agent_id,
            name="eval-target",
            model=ModelRef(provider="mock", model="mock-1"),
            strategy=StrategyConfig(type="function_calling"),
        ),
    )


class _FakeAgents:
    def __init__(self, version: AgentVersion | None) -> None:
        self._version = version

    async def latest_version(self, agent_id: str) -> AgentVersion | None:
        return self._version


class _FakeExecutions:
    def __init__(self) -> None:
        self.calls: list[tuple[object, RunQueueMessage]] = []

    async def create_queued_run(self, result: object, message: RunQueueMessage) -> None:
        self.calls.append((result, message))


class _FakeEvalRepo:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def create_run(
        self, dataset, agent_id, agent_version_id, children, *, run_id=None, tenant_id=None
    ):
        self.kwargs = {
            "dataset": dataset,
            "agent_id": agent_id,
            "agent_version_id": agent_version_id,
            "children": children,
            "run_id": run_id,
            "tenant_id": tenant_id,
        }
        # mirror the repo contract: the persisted run comes back
        from jarvis.domain.evaluation import EvalRun

        return EvalRun(
            id=run_id or "generated",
            dataset_id=dataset.id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            dataset=dataset,
        )


def _create_run(
    executions: _FakeExecutions, eval_repo: _FakeEvalRepo, dataset=None, agent_id: str = "a-1"
):
    return asyncio.run(
        create_eval_run(
            settings=Settings(_env_file=None),
            agents=_FakeAgents(_version(agent_id)),
            evals=eval_repo,
            executions=executions,
            dataset=dataset or _dataset(),
            agent_id=agent_id,
            principal=Principal(tenant_id="t1", mode="session"),
        )
    )


class TestCreateEvalRun:
    def test_children_are_ordinary_pinned_runs(self) -> None:
        executions = _FakeExecutions()
        eval_repo = _FakeEvalRepo()
        run = _create_run(executions, eval_repo)
        assert len(executions.calls) == 2
        for _result, message in executions.calls:
            assert message.kind == "agent"
            assert message.agent_version_id == "ver-9"  # pinned, not latest-at-claim
            assert message.session_id is None  # isolated case runs
            stamp = message.metadata["eval"]
            assert stamp["eval_run_id"] == run.id
            assert stamp["case_id"] in {"c-1", "c-2"}
        # the eval repo got the same run_id + the child run_ids, in order
        child_ids = [child_run_id for _, child_run_id in eval_repo.kwargs["children"]]
        message_ids = [message.run_id for _, message in executions.calls]
        assert child_ids == message_ids
        assert eval_repo.kwargs["run_id"] == run.id
        assert eval_repo.kwargs["agent_version_id"] == "ver-9"
        assert eval_repo.kwargs["tenant_id"] == "t1"

    def test_case_variables_and_inputs_carried(self) -> None:
        ds = _dataset(cases=[{"id": "c-1", "input": "hi {{name}}", "variables": {"name": "x"}}])
        executions = _FakeExecutions()
        _create_run(executions, _FakeEvalRepo(), dataset=ds)
        message = executions.calls[0][1]
        assert message.input == "hi {{name}}"
        assert message.variables == {"name": "x"}

    def test_missing_version_404(self) -> None:
        executions = _FakeExecutions()
        with pytest.raises(ApiError) as exc:
            asyncio.run(
                create_eval_run(
                    settings=Settings(_env_file=None),
                    agents=_FakeAgents(None),
                    evals=_FakeEvalRepo(),
                    executions=executions,
                    dataset=_dataset(),
                    agent_id="ghost",
                    principal=Principal(tenant_id="t1", mode="session"),
                )
            )
        assert exc.value.status_code == 404
        assert executions.calls == []  # nothing enqueued on the failure path

    def test_llm_judge_without_judge_model_422(self) -> None:
        ds = _dataset(scorers=[{"name": "llm_judge"}])
        with pytest.raises(ApiError) as exc:
            _create_run(_FakeExecutions(), _FakeEvalRepo(), dataset=ds)
        assert exc.value.status_code == 422
        # the same rule at the dataset boundary
        with pytest.raises(ApiError):
            validate_dataset_scorers(ds)
        validate_dataset_scorers(_dataset())  # valid datasets pass

    def test_eval_run_id_stamped_before_enqueue(self) -> None:
        """D48 ordering: the metadata's eval_run_id equals the id the repo
        later persists — the children reference the eval run that will
        exist by the time anyone reads them."""
        executions = _FakeExecutions()
        eval_repo = _FakeEvalRepo()
        run = _create_run(executions, eval_repo)
        stamps = {message.metadata["eval"]["eval_run_id"] for _, message in executions.calls}
        assert stamps == {run.id}
