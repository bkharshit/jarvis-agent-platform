"""SSE over the API: framing, delta order, terminal close, Last-Event-ID
resume with exactly-once delivery."""

from __future__ import annotations

import pytest

from jarvis.domain.message import ToolCall
from tests.integration.conftest import parse_sse

pytestmark = pytest.mark.db


@pytest.mark.db
async def test_sse_framing_delta_order_terminal_close(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("hello streaming world"))

    resp = await client.post(f"/v1/agents/{agent.id}/stream", json={"input": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store"

    frames = parse_sse(resp.text)
    types = [t for _, t, _ in frames]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"  # terminal ends the stream
    assert types.count("run.completed") == 1

    ids = [i for i, _, _ in frames]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)  # durable cursors

    deltas = [d["text"] for _, t, d in frames if t == "text.delta"]
    assert "".join(deltas) == "hello streaming world"  # delta order preserved

    sequences = [d["sequence"] for _, _, d in frames]
    assert sequences == sorted(sequences)  # per-run gapless


@pytest.mark.db
async def test_last_event_id_resume_is_exactly_once(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "2+2"})])
    )
    mock.add_turn(turn("four"))

    # First connection: consume a few frames, then drop mid-run.
    first_frames = []
    run_id = None
    async with client.stream(
        "POST", f"/v1/agents/{agent.id}/stream", json={"input": "compute"}
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                cursor = int(line.removeprefix("id: "))
            elif line.startswith("data: "):
                import json

                data = json.loads(line.removeprefix("data: "))
                run_id = run_id or data["run_id"]
                first_frames.append((cursor, data["type"]))
            if len(first_frames) >= 4:  # run.started + iteration + invocation + first delta
                break
    assert run_id is not None
    last_seen = first_frames[-1][0]

    # Reconnect with Last-Event-ID + run_id in the body.
    resp = await client.post(
        f"/v1/agents/{agent.id}/stream",
        json={"input": "compute", "run_id": run_id},
        headers={"Last-Event-ID": str(last_seen)},
    )
    assert resp.status_code == 200
    resumed = parse_sse(resp.text)

    # Exactly once: resumed cursors continue after the drop with no overlap,
    # and the union reconstructs the whole gapless run.
    resumed_ids = [i for i, _, _ in resumed]
    assert all(i > last_seen for i in resumed_ids)
    assert resumed[-1][1] == "run.completed"

    full = await client.post(
        f"/v1/agents/{agent.id}/stream",
        json={"input": "compute", "run_id": run_id},
        headers={"Last-Event-ID": "-1"},
    )
    all_frames = parse_sse(full.text)
    all_ids = [i for i, _, _ in all_frames]
    assert all_ids == sorted(set(all_ids))
    seen = set(i for i, _ in first_frames) | set(resumed_ids)
    assert seen == set(all_ids)  # nothing missed, nothing duplicated


@pytest.mark.db
async def test_finished_run_resume_replays_from_db(client, container, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("all done"))
    first = await client.post(f"/v1/agents/{agent.id}/stream", json={"input": "hi"})
    frames = parse_sse(first.text)
    run_id = frames[0][2]["run_id"]
    assert frames[-1][1] == "run.completed"  # finished

    # Drop the in-memory sink (Phase 1 never drops it — this simulates a
    # restart) so the resume must replay from the DB in cursor space.
    container.bus.drop(run_id)
    middle = frames[len(frames) // 2][0]
    resp = await client.post(
        f"/v1/agents/{agent.id}/stream",
        json={"input": "hi", "run_id": run_id},
        headers={"Last-Event-ID": str(middle)},
    )
    resumed = parse_sse(resp.text)
    assert [i for i, _, _ in resumed] == [i for i, _, _ in frames if i > middle]


@pytest.mark.db
async def test_stream_unknown_run_404(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent.id}/stream",
        json={"input": "hi", "run_id": "missing"},
    )
    assert resp.status_code == 404


@pytest.mark.db
async def test_stream_rejects_bad_last_event_id(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent.id}/stream",
        json={"input": "hi", "run_id": "nope"},
        headers={"Last-Event-ID": "not-a-number"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["kind"] == "validation"
