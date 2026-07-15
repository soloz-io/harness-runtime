"""Typed execution state — replaces the stringly-typed state dict.

Ensures all state mutations are type-checked and discoverable.
"""

from dataclasses import dataclass, field
from typing import Any

from core.types import Namespace


@dataclass
class ExecutionState:
    """Shared state across v3 event processing.

    Mutated by event handlers during graph execution and consumed
    by the final result publisher.
    """

    streamed_text: str = ""
    current_tool_use_blocks: list[dict[str, Any]] = field(default_factory=list)
    ns_to_tool_call: dict[Namespace, str] = field(default_factory=dict)
    subagent_names: dict[Namespace, str] = field(default_factory=dict)
    last_structured_response: dict[str, Any] | None = None
    last_files: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    values_messages_count: int = 0
    subagent_stream_outputs: dict[str, str] = field(default_factory=dict)
