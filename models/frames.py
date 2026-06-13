from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# -- Content Blocks --

@dataclass
class TextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class ThinkingBlock:
    type: str = "thinking"
    thinking: str = ""
    signature: str = ""


@dataclass
class ToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    type: str = "tool_result"
    tool_use_id: str = ""
    content: str = ""
    is_error: bool = False


ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


# -- Incoming Frames (SDK -> Server) --

@dataclass
class ControlRequest:
    type: str = "control_request"
    request_id: str = ""
    request: dict[str, Any] = field(default_factory=dict)

    @property
    def subtype(self) -> str:
        return str(self.request.get("subtype", ""))


@dataclass
class UserMessage:
    type: str = "user"
    message: dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    parent_tool_use_id: Optional[str] = None


IncomingFrame = ControlRequest | UserMessage


# -- Outgoing Frames (Server -> SDK) --

@dataclass
class SystemInitFrame:
    type: str = "system"
    subtype: str = "init"
    session_id: str = ""
    model: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AssistantFrame:
    type: str = "assistant"
    message: dict[str, Any] = field(default_factory=dict)
    parent_tool_use_id: Optional[str] = None
    session_id: Optional[str] = None

    @classmethod
    def build(cls, *, session_id: str, model: str, content: list[dict[str, Any]],
              parent_tool_use_id: Optional[str] = None) -> "AssistantFrame":
        return cls(
            message={"model": model, "content": content},
            parent_tool_use_id=parent_tool_use_id,
            session_id=session_id,
        )


@dataclass
class UserEchoFrame:
    type: str = "user"
    message: dict[str, Any] = field(default_factory=dict)
    parent_tool_use_id: Optional[str] = None
    session_id: Optional[str] = None

    @classmethod
    def build(cls, *, session_id: str, content: list[dict[str, Any]]) -> "UserEchoFrame":
        return cls(
            message={"role": "user", "content": content},
            session_id=session_id,
        )


@dataclass
class StreamEventFrame:
    type: str = "stream_event"
    session_id: str = ""
    event: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text_delta(cls, *, session_id: str, text: str, index: int = 0) -> "StreamEventFrame":
        return cls(
            session_id=session_id,
            event={
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
        )


@dataclass
class ResultFrame:
    type: str = "result"
    subtype: str = "success"
    session_id: str = ""
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 1
    total_cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    structured_response: Optional[Any] = None
    files: Optional[dict[str, Any]] = None


@dataclass
class ControlResponseFrame:
    type: str = "control_response"
    response: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, *, request_id: str, **extra: Any) -> "ControlResponseFrame":
        return cls(response={"request_id": request_id, "subtype": "success", **extra})

    @classmethod
    def error(cls, *, request_id: str, error: str) -> "ControlResponseFrame":
        return cls(response={"request_id": request_id, "subtype": "error", "error": error})


OutgoingFrame = SystemInitFrame | AssistantFrame | UserEchoFrame | StreamEventFrame | ResultFrame | ControlResponseFrame


def frame_to_dict(frame: OutgoingFrame) -> dict[str, Any]:
    return asdict(frame)
