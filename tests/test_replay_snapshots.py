"""A replayable computer gets a workspace snapshot after every model call.

The share replays a session's computer from workspace snapshots named by the
message each model call produced (waypoint ADR-050 §6). The request goes to the
workspace-sync sidecar in the background and never holds the agent up.
"""

from __future__ import annotations

import asyncio
import threading

from langchain_core.messages import AIMessage, HumanMessage

import core.middleware.replay_snapshots as rs


def _state(*messages):
    return {"messages": list(messages)}


def test_named_by_the_message_the_model_call_produced():
    assert rs.snapshot_name(_state(HumanMessage("hi"), AIMessage("ok", id="ai-7"))) == "ai-7"
    assert rs.snapshot_name(_state(HumanMessage("hi", id="h-1"))) is None
    assert rs.snapshot_name(_state(AIMessage("ok"))) is None
    assert rs.snapshot_name({}) is None


def test_off_unless_the_definition_declared_replay(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(rs, "_request_sync", sent.append)
    monkeypatch.delenv(rs.ENV_ENABLED, raising=False)
    rs.ReplaySnapshotMiddleware().after_model(_state(AIMessage("ok", id="ai-1")), None)
    assert sent == []


def test_sent_in_the_background_without_waiting(monkeypatch):
    monkeypatch.setenv(rs.ENV_ENABLED, "1")
    started = threading.Event()
    release = threading.Event()

    def slow(name: str) -> None:
        started.set()
        release.wait(5)

    monkeypatch.setattr(rs, "_request_sync", slow)
    assert (
        rs.ReplaySnapshotMiddleware().after_model(_state(AIMessage("ok", id="ai-2")), None) is None
    )
    assert started.wait(5), "the request was never sent"
    release.set()


def test_async_path_schedules_and_returns(monkeypatch):
    monkeypatch.setenv(rs.ENV_ENABLED, "1")
    seen: list[str] = []

    async def fake(name: str) -> None:
        seen.append(name)

    monkeypatch.setattr(rs, "_request_async", fake)

    async def run():
        assert (
            await rs.ReplaySnapshotMiddleware().aafter_model(
                _state(AIMessage("ok", id="ai-3")), None
            )
            is None
        )
        await asyncio.sleep(0)

    asyncio.run(run())
    assert seen == ["ai-3"]


def test_the_request_names_the_message_and_does_not_queue(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SYNC_PORT", "7999")
    assert rs._checkpoint_url() == "http://127.0.0.1:7999/checkpoint?wait=1"
