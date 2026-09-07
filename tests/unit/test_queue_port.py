"""RunQueue port message contract (ADR 0008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from jarvis.ports.queue import RunQueueMessage


class TestRunQueueMessage:
    def test_defaults(self):
        before = datetime.now(UTC)
        message = RunQueueMessage(run_id="r1", agent_id="a1", agent_version_id="v1", input="hi")
        assert message.session_id is None
        assert message.user_id is None
        assert message.trace_id == ""
        assert message.metadata == {}
        assert message.variables == {}
        assert message.deadline is None
        assert message.enqueued_at >= before

    def test_deadline_is_absolute(self):
        # The deadline is computed at enqueue time so it survives the
        # cross-process hop — a worker never re-derives it.
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        message = RunQueueMessage(
            run_id="r1",
            agent_id="a1",
            agent_version_id="v1",
            input="hi",
            deadline=deadline,
        )
        assert message.deadline == deadline

    def test_roundtrip_json(self):
        message = RunQueueMessage(
            run_id="r1",
            agent_id="a1",
            agent_version_id="v1",
            input="hi",
            metadata={"k": "v"},
            variables={"x": 1},
        )
        restored = RunQueueMessage.model_validate(message.model_dump(mode="json"))
        assert restored == message

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            RunQueueMessage(
                run_id="r1",
                agent_id="a1",
                agent_version_id="v1",
                input="hi",
                surprise=1,  # type: ignore[call-arg]
            )

    def test_required_fields(self):
        with pytest.raises(ValidationError) as exc:
            RunQueueMessage(run_id="r1")  # type: ignore[call-arg]
        missing = {e["loc"][0] for e in exc.value.errors() if e["type"] == "missing"}
        assert missing == {"agent_id", "agent_version_id", "input"}


class TestResumeRequest:
    def test_content_resume(self):
        from jarvis.ports.queue import ResumeRequest

        resume = ResumeRequest(kind="content", content="deploy staging")
        assert resume.approved is None
        restored = ResumeRequest.model_validate(resume.model_dump(mode="json"))
        assert restored == resume

    def test_tool_approval_resume(self):
        from jarvis.ports.queue import ResumeRequest

        assert ResumeRequest(kind="tool_approval", approved=True).content is None
        with pytest.raises(ValidationError):
            ResumeRequest(kind="weird")  # type: ignore[arg-type]

    def test_decisions_resume_roundtrip(self):
        from jarvis.ports.queue import ResumeRequest

        resume = ResumeRequest(kind="decisions", decisions={"c1": True, "c2": False})
        restored = ResumeRequest.model_validate(resume.model_dump(mode="json"))
        assert restored == resume
        assert restored.decisions == {"c1": True, "c2": False}

    def test_kind_requires_its_answer_field(self):
        from jarvis.ports.queue import ResumeRequest

        with pytest.raises(ValidationError):
            ResumeRequest(kind="decisions")  # no map
        with pytest.raises(ValidationError):
            ResumeRequest(kind="decisions", decisions={})  # empty map
        with pytest.raises(ValidationError):
            ResumeRequest(kind="tool_approval")  # no boolean
        with pytest.raises(ValidationError):
            ResumeRequest(kind="content")  # no text

    def test_message_carries_resume_roundtrip(self):
        from jarvis.ports.queue import ResumeRequest, RunQueueMessage

        message = RunQueueMessage(
            run_id="r1",
            agent_id="a1",
            agent_version_id="v1",
            input="original",
            resume=ResumeRequest(kind="tool_approval", approved=False),
        )
        restored = RunQueueMessage.model_validate(message.model_dump(mode="json"))
        assert restored == message
        assert message.resume is not None and message.resume.approved is False
