"""Job tracking (ADR-015): every job a tool result reports as started is tracked."""

import json

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from core.middleware.background_jobs.middleware import _track, started_jobs


def tool_result(text):
    return ToolMessage(content=text, tool_call_id="c1", name="run_tool")


def test_one_job_is_tracked():
    result = _track(tool_result(json.dumps({"status": "QUEUED", "run_id": "r1"})))
    assert isinstance(result, Command)
    assert result.update["pending_jobs"] == ["r1"]


def test_every_job_a_cli_starts_is_tracked():
    # One contract line per job, then the CLI's own summary (no run_id of its own).
    output = "\n".join(
        [
            "[video_cli] POST .../runs/start",
            json.dumps({"status": "QUEUED", "run_id": "r1", "scene_id": "003"}),
            json.dumps({"status": "QUEUED", "run_id": "r2", "scene_id": "007"}),
            json.dumps(
                {"success": True, "status": "QUEUED", "jobs": [{"run_id": "r1"}, {"run_id": "r2"}]}
            ),
        ]
    )
    assert started_jobs(tool_result(output)) == ["r1", "r2"]
    assert _track(tool_result(output)).update["pending_jobs"] == ["r1", "r2"]


def test_a_dispatchers_output_field_is_read():
    inner = "\n".join(json.dumps({"status": "QUEUED", "run_id": r}) for r in ("r1", "r2"))
    assert started_jobs(tool_result(json.dumps({"success": True, "output": inner}))) == ["r1", "r2"]


def test_no_job_leaves_the_result_alone():
    message = tool_result(json.dumps({"status": "WRITTEN"}))
    assert _track(message) is message
