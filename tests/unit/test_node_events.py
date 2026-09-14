"""S6 (D43): `node_id` on the event envelope, `node.started`/`node.completed`
types, and the NodeSink wrapper — stamps every event, refuses terminals.

Backward compatibility is the replay contract: old persisted events
(deserialized without a node_id field) replay as `node_id: None`, and
`validate_event_sequence`/cursor semantics are untouched."""

from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from jarvis.domain.events import (
    EventSequenceError,
    ExecutionEvent,
    NodeCompleted,
    NodeStarted,
    RunCompleted,
    RunFailed,
    TextDelta,
    is_terminal,
    validate_event_sequence,
)
from jarvis.domain.message import Usage
from jarvis.events.bus import InProcessEventSink, NodeSink

_adapter = TypeAdapter(ExecutionEvent)


def _delta(text: str) -> TextDelta:
    return TextDelta(event_id=str(uuid4()), run_id="r1", text=text)


def _terminal() -> RunCompleted:
    return RunCompleted(
        event_id=str(uuid4()),
        run_id="r1",
        final_message="done",
        total_usage=Usage(),
        iterations=1,
    )


def _failed() -> RunFailed:
    return RunFailed(
        event_id=str(uuid4()),
        run_id="r1",
        error="boom",
        error_kind="model",
        total_usage=Usage(),
    )


class TestEnvelopeNodeId:
    def test_default_is_none_and_old_events_replay_as_none(self):
        """The replay contract: an old persisted event (no node_id field)
        deserializes with node_id None."""
        event = _delta("hello")
        assert event.node_id is None
        replayed = _adapter.validate_python(event.model_dump(mode="json"))
        assert replayed.node_id is None  # type: ignore[union-attr]

    def test_node_id_round_trips_through_the_union(self):
        event = _adapter.validate_python(
            {
                "type": "text.delta",
                "event_id": str(uuid4()),
                "run_id": "r1",
                "node_id": "summarize",
                "text": "hi",
            }
        )
        assert isinstance(event, TextDelta)
        assert event.node_id == "summarize"


class TestNodeEventTypes:
    def test_node_started_carries_the_boundary(self):
        event = NodeStarted(event_id=str(uuid4()), run_id="r1", node_id="n1", node_type="agent")
        assert event.node_id == "n1"
        assert not is_terminal(event)
        # the payload node_id IS the envelope node_id (one field, overridden)
        assert NodeStarted.model_fields["node_id"].is_required()

    def test_node_completed_round_trip(self):
        event = NodeCompleted(
            event_id=str(uuid4()),
            run_id="r1",
            node_id="n1",
            node_type="tool",
            output="42",
        )
        data = event.model_dump(mode="json")
        assert data["is_error"] is False  # the D43 default
        replayed = _adapter.validate_python(data)
        assert isinstance(replayed, NodeCompleted)
        assert replayed.node_id == "n1"

    def test_node_events_validate_in_a_sequence(self):
        events = [
            NodeStarted(event_id=str(uuid4()), run_id="r1", node_id="n1", node_type="agent"),
            _terminal(),
        ]
        for index, event in enumerate(events):
            event.sequence = index
        validate_event_sequence(events)  # non-terminal boundary before terminal


class TestNodeSink:
    def _sink(self) -> InProcessEventSink:
        return InProcessEventSink("r1")

    async def test_stamps_node_id_on_every_appended_event(self):
        sink = self._sink()
        node = NodeSink(sink, "n1")
        await node.append(_delta("hello"))
        stamped = sink.events[0]
        assert stamped.node_id == "n1"
        assert stamped.sequence == 0

    async def test_refuses_terminal_append(self):
        node = NodeSink(self._sink(), "n1")
        with pytest.raises(EventSequenceError, match="terminal"):
            await node.append(_terminal())

    async def test_refuses_finalize(self):
        node = NodeSink(self._sink(), "n1")
        with pytest.raises(EventSequenceError, match="finalize"):
            await node.finalize(_terminal())

    async def test_rejects_a_foreign_node_id(self):
        node = NodeSink(self._sink(), "n1")
        with pytest.raises(EventSequenceError, match="does not match"):
            await node.append(_delta("x").model_copy(update={"node_id": "n2"}))

    async def test_inner_sink_terminal_visibility(self):
        """The run sink is untouched by the wrapper: the runtime finalizes
        it directly after the node's events."""
        sink = self._sink()
        node = NodeSink(sink, "n1")
        await node.append(_delta("hello"))
        await sink.finalize(_terminal())
        types = [e.type for e in sink.events]
        assert types == ["text.delta", "run.completed"]
        assert sink.events[0].node_id == "n1"
        assert sink.events[1].node_id is None
        validate_event_sequence(sink.events)  # type: ignore[arg-type]

    async def test_run_failed_through_node_sink_is_refused(self):
        """The no-node.failed rule from the other side: even a run.failed
        naming the node may not enter through the node's sink."""
        node = NodeSink(self._sink(), "n1")
        with pytest.raises(EventSequenceError, match="terminal"):
            await node.append(_failed())
