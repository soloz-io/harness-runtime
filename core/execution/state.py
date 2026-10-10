"""Typed execution state — replaces the stringly-typed state dict.

Ensures all state mutations are type-checked and discoverable.
"""

from dataclasses import dataclass, field
from typing import Any

from core.execution.types import Namespace


@dataclass
class ExecutionState:
    """Shared state across v3 event processing.

    Mutated by event handlers during graph execution and consumed
    by the final result publisher.
    """

    streamed_text: str = ""
    current_tool_use_blocks: list[dict[str, Any]] = field(default_factory=list)
    ns_to_tool_call: dict[Namespace, str] = field(default_factory=dict)
    # Each agent's last published todo list, so an unchanged list is not resent (ADR-052).
    subagent_todos: dict[Namespace, list] = field(default_factory=dict)
    subagent_names: dict[Namespace, str] = field(default_factory=dict)
    last_structured_response: dict[str, Any] | None = None
    last_files: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    values_messages_count: int = 0
    # Whether the root agent's latest state holds an `ask_user` call the user has
    # not answered. Recomputed on every root values event; read when the turn
    # ends, to report the question to Waypoint (core/waypoint_reports/questions.py).
    unanswered_question: bool = False
    # Whether the root agent wrote a reply this turn that the session had not
    # recorded before. Read when the turn ends, to report the reply to Waypoint
    # (core/waypoint_reports/replies.py).
    replied: bool = False
    subagent_stream_outputs: dict[str, str] = field(default_factory=dict)
    subagent_final_outputs: dict[str, str] = field(default_factory=dict)
    subagent_values_messages_count: dict[Namespace, int] = field(default_factory=dict)
    workspace_id: str = ""
    app_id: str | None = None
