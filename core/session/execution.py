import base64
from typing import Any, Optional

import httpx
import structlog

from core.factory import build_agent_from_definition
from core.tool_registry import ToolRegistry

logger = structlog.get_logger(__name__)

# DeepSeek caps inline images at 32 MiB; stay well under so the base64
# expansion (~4/3) still fits comfortably within a single request.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


async def inline_object_attachments(
    attachments: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    """Download ``source.type == "object"`` **image** attachments and return
    them as inline base64 (``source.type == "data"``) instead.

    The model provider is reached through AI_GATEWAY_BASE_URL
    (api.deepseek.com in this deployment). Handing it an object-storage URL
    makes *its* servers fetch the bytes, which fails outright here: a real
    sandbox turn died with ``400 ... Failed to download image from
    https://hel1.your-objectstorage.com/...`` even though that URL serves a
    valid 368KB image/png publicly (HTTP 200, verified directly). The
    provider simply cannot reach Hetzner object storage. DeepSeek documents
    base64 data URIs as a first-class alternative to URLs, so fetching here
    — inside the cluster, where the bucket *is* reachable — and inlining the
    bytes removes the dependency on provider-side egress entirely.

    Non-image attachments (voice notes) are left untouched: they are never
    handed to the provider as blocks — only named as a text line in
    ``_build_message_content`` — so there is nothing to fetch, and
    downloading them would just burn the size budget on bytes the model
    never sees.

    Raises on any download failure rather than silently dropping the image:
    a turn that quietly proceeds without an attachment the user explicitly
    supplied produces confidently wrong work.
    """
    if not attachments:
        return attachments

    result: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for a in attachments:
            source = a["source"]
            if source["type"] != "object" or not str(a.get("mime", "")).startswith("image/"):
                result.append(a)
                continue

            url = source["value"]
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"Attachment {url} is {len(data)} bytes, over the "
                    f"{MAX_ATTACHMENT_BYTES}-byte inline limit."
                )

            mime = a["mime"]
            encoded = base64.b64encode(data).decode("ascii")
            logger.info("attachment_inlined", url=url, bytes=len(data), mime=mime)
            result.append(
                {
                    **a,
                    "source": {"type": "data", "value": f"data:{mime};base64,{encoded}"},
                    "source_url": url,
                }
            )
    return result


def _build_message_content(
    user_content: str, attachments: Optional[list[dict[str, Any]]]
) -> str | list[dict[str, Any]]:
    """Return plain text, or LangChain's provider-agnostic multimodal content
    blocks when attachments are present. `source.type == "object"` (the only
    value the SDK produces in v1 — attachments are uploaded to S3 before this
    is ever called) maps to LangChain's `source_type: "url"` image block, so
    the provider resolves the URL itself with no fetch-and-re-encode here.
    `source.type == "data"` is kept for forward compatibility with a future
    caller that skips the upload step.

    Each image block is preceded by a plain `"text"` block stating its URL.
    An image block's `url`/`data` field is consumed by the provider to fetch
    or render the image for vision input — it is not exposed to the model as
    readable text (confirmed against a real transcript: an orchestrator
    asked to forward an attachment's URL to a subagent's task() description
    reported "<url not provided>", even though the image block, with the URL
    in it, was genuinely part of its own message). Without a text block
    carrying the literal URL, the model can see the picture but has no way
    to recite its source — so it can't be blamed for not forwarding what it
    was never actually given as text. The orchestrator's own
    `15-attachment-forwarding.md` instructions assume this text is present.

    Non-image attachments (voice notes, kind ``audio``) get the text line
    ONLY — ``[Attached audio: <url>]``. Emitting an image block for
    ``audio/webm`` makes the provider answer ``400`` and the whole turn
    dies; the SDK therefore sends every attachment through and trusts this
    function to be the one translator. The model still receives the URL as
    readable text, which is all it can act on: providers in this deployment
    take no audio input, and consuming/transcribing the note is a follow-up.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": user_content}] if user_content else []
    for a in attachments or []:
        source = a["source"]
        is_image = str(a.get("mime", "")).startswith("image/")
        if not is_image:
            # Never inline base64 into the message: for an object source the
            # URL is the citable reference; for a data source (forward
            # compat only — v1 always uploads first) fall back to filename
            # or mime rather than dumping the bytes into readable text.
            if source["type"] == "object":
                ref = source["value"]
            else:
                ref = a.get("filename") or a["mime"]
            content.append({"type": "text", "text": f"[Attached {a.get('kind', 'file')}: {ref}]"})
            continue
        if source["type"] == "object":
            content.append({"type": "text", "text": f"[Attached image: {source['value']}]"})
            content.append(
                {
                    "type": "image",
                    "source_type": "url",
                    "url": source["value"],
                    "mime_type": a["mime"],
                }
            )
        elif source["type"] == "data":
            # source_url survives inline_object_attachments' base64 conversion
            # so the model can still cite/forward the original location.
            origin = a.get("source_url")
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached image: {origin}]"
                        if origin
                        else "[Attached image: inline data, no URL available]"
                    ),
                }
            )
            content.append(
                {
                    "type": "image",
                    "source_type": "base64",
                    "data": source["value"].split(",", 1)[1],
                    "mime_type": a["mime"],
                }
            )
    return content if attachments else user_content


def prepare_turn_input(
    base_payload: dict[str, Any],
    user_content: str,
    role: str = "user",
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Return a shallow copy of ``base_payload`` with ``user_content`` appended as a message.

    This is a pure function — no side effects.
    """
    payload = dict(base_payload)
    messages = list(payload.get("messages", []))
    if user_content or attachments:
        messages.append(
            {"role": role, "content": _build_message_content(user_content, attachments)}
        )
    payload["messages"] = messages
    return payload


def consume_resume(session: Any) -> Optional[Any]:
    """Consume and clear any stored resume payload.

    Returns the resume payload or ``None``.
    """
    resume = getattr(session, "resume_payload", None)
    if resume is not None:
        session.resume_payload = None
    return resume


def build_graph(
    agent_definition: dict[str, Any],
    checkpointer: Any,
    tool_registry: ToolRegistry,
    workspace_id: str,
    session_id: str,
    backend: Any,
    composite_backend: Any,
    tools_ctx: Any = None,
) -> Any:
    """Build a compiled LangGraph from the agent definition.

    All session-level dependencies are passed explicitly so callers
    do not need to reach into ``Session`` internals.
    """
    return build_agent_from_definition(
        agent_definition,
        checkpointer=checkpointer,
        tool_registry=tool_registry,
        workspace_id=workspace_id,
        session_id=session_id,
        backend=backend,
        composite_backend=composite_backend,
        tools_ctx=tools_ctx,
    )


def initialize_tool_registry(agent_definition: dict[str, Any]) -> ToolRegistry:
    """Load tool definitions from the agent definition into a fresh ToolRegistry."""
    from core.embedded_tool_loader import ToolLoadingError, load_tool_implementations

    registry = ToolRegistry()
    tool_definitions = agent_definition.get("tool_definitions", [])
    if tool_definitions:
        try:
            load_tool_implementations(tool_definitions, registry)
        except ToolLoadingError as e:
            import structlog

            structlog.get_logger(__name__).error("tool_loading_failed", error=str(e))
            raise
    return registry
