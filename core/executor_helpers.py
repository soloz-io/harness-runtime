"""Shared helper functions used by event handlers and executor.

Extracted from ``executor.py`` to allow reuse across handlers
without circular imports.
"""

import json
import uuid
from typing import Any

from deepagents.middleware.filesystem import FilesystemMiddleware
from langgraph.types import Command


def get_middleware_tools() -> list[dict[str, Any]]:
    """Return FilesystemMiddleware tool definitions from deepagents."""
    mw = FilesystemMiddleware()
    return [{"name": t.name, "description": t.description or ""} for t in mw.tools]


def compute_tools(agent_definition: dict[str, Any]) -> Any:
    """Compute the full tools list for the system frame.

    For star-topology definitions, uses the root-level "tools" field.
    For custom DAG definitions, unions tools across all nodes' config.tools
    plus FilesystemMiddleware tools from deepagents.
    """
    root_tools = agent_definition.get("tools", [])
    if root_tools:
        return root_tools

    middleware_tools = get_middleware_tools()
    seen_names: set[str] = {t["name"] for t in middleware_tools}
    tools: list[dict[str, Any]] = list(middleware_tools)

    nodes = agent_definition.get("nodes", [])
    tool_defs = agent_definition.get("tool_definitions", [])
    tool_def_map = {t.get("name"): t for t in tool_defs if isinstance(t, dict)}

    for node in nodes:
        node_config = node.get("config", {})
        for name in node_config.get("tools", []):
            if name not in seen_names:
                seen_names.add(name)
                if name in tool_def_map:
                    tools.append(dict(tool_def_map[name]))
                else:
                    tools.append({"name": name, "description": ""})

    return tools


def serialize_content(content: Any) -> str:
    """Serialize arbitrary content to a string for tool results."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return json.dumps(content, default=str)
    return str(content)


def serialize_messages_for_values(messages: list) -> list[dict[str, Any]]:
    """Serialize LangGraph message objects to plain dicts for values channel."""
    serialized: list[dict[str, Any]] = []
    for msg in messages:
        msg_id = getattr(msg, "id", None)
        if not msg_id:
            msg_id = str(uuid.uuid4())
        entry: dict[str, Any] = {
            "id": msg_id,
            "type": getattr(msg, "type", "unknown"),
        }
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            entry["content"] = content
        elif isinstance(content, list):
            entry["content"] = content
        else:
            entry["content"] = str(content)
        tool_calls = getattr(msg, "tool_calls", [])
        if tool_calls:
            entry["tool_calls"] = tool_calls
        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        name = getattr(msg, "name", None)
        if name:
            entry["name"] = name
        additional_kwargs = getattr(msg, "additional_kwargs", {})
        if additional_kwargs:
            entry["additional_kwargs"] = additional_kwargs
        serialized.append(entry)
    return serialized


def extract_interrupt_payload(interrupt_val: Any) -> Any:
    """Extract a serializable payload from a LangGraph interrupt value."""
    if isinstance(interrupt_val, (list, tuple)) and len(interrupt_val) > 0:
        raw = interrupt_val[0]
        if hasattr(raw, "value"):
            return raw.value
        if isinstance(raw, dict):
            return raw
        return raw
    return interrupt_val


def extract_tool_finished_content(raw_output: Any) -> str:
    """Extract display-friendly content from a tool's finished output."""
    if isinstance(raw_output, Command) and raw_output.update:
        raw_output = raw_output.update

    if isinstance(raw_output, dict):
        msgs = raw_output.get("messages", [])
        if msgs:
            last_msg = msgs[-1]
            msg_content = getattr(last_msg, "content", "")
            if isinstance(msg_content, list):
                return " ".join(b.get("text", "") for b in msg_content if isinstance(b, dict))
            if not isinstance(msg_content, str):
                return str(msg_content)
            return msg_content
        return serialize_content(raw_output)
    return serialize_content(raw_output)
