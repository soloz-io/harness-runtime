"""
URL Fetch Middleware — provides the ``fetch_url`` capability to agents.

Attachments (e.g. images the user attaches in chat) reach an agent as a plain
HTTPS URL embedded in text — the platform resolves them to an object-store
URL before any agent ever sees them (see waypoint-sdk's chat-attachments.ts),
and neither ``read_file`` (deepagents' own FilesystemMiddleware tool, which
only reads the virtual workspace filesystem — confirmed by tracing
``validate_path``/``backend.read`` in the installed deepagents package; it
has no HTTP client and cannot fetch a URL) nor any other built-in tool can
turn that URL into something the model can actually see. This middleware
closes that gap: ``fetch_url`` downloads the bytes and returns them as the
same multimodal content-block shape ``read_file`` already returns for local
image/file reads, so a vision-capable model sees the fetched image exactly as
it would see one read from disk.

Audio and video are the exception: they are classified but never inlined (see
``_NON_INLINE_BLOCK_TYPES``), because no model this runtime targets can read
them and their base64 is large enough to end a turn on its own.

General-purpose, not tied to any one agent or domain — any specialist that
needs to view an attachment forwarded to it in text (per the platform's
"forward attachment URLs in routing context" convention) can use this tool.
"""

from typing import cast

import httpx
import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.messages.content import ContentBlock
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# Mirrors deepagents.backends.utils._EXTENSION_TO_FILE_TYPE — the multimodal
# content-block type read_file() already uses for these extensions, kept
# in sync deliberately so a fetched attachment and a locally-read file of
# the same type produce identical block shapes for the model.
_EXTENSION_TO_BLOCK_TYPE: dict[str, str] = {
    ".png": "image",
    ".jpeg": "image",
    ".jpg": "image",
    ".webp": "image",
    ".gif": "image",
    ".heic": "image",
    ".heif": "image",
    ".mp4": "video",
    ".mpeg": "video",
    ".mov": "video",
    ".avi": "video",
    ".webm": "video",
    ".wmv": "video",
    ".wav": "audio",
    ".mp3": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    ".pdf": "file",
}

_CONTENT_TYPE_TO_BLOCK_TYPE: dict[str, str] = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "application/pdf": "file",
}

MAX_FETCH_BYTES = (
    10 * 1024 * 1024
)  # 10MB — generous for a single attachment, bounded against a runaway tool call

# Block types whose bytes never enter the conversation.
#
# The extension/content-type maps above mirror deepagents', which is written
# against Gemini (its own comments cite ai.google.dev's audio docs) — a model
# that genuinely ingests audio and video. Reached by a model that does not,
# those bytes are not a media the model perceives: they are base64 filler it
# cannot decode, at roughly 1.33 characters per byte. A 1.9MB narration track
# became 2.5 million characters — ~636k tokens — in a single tool message, the
# newest one, which compaction cannot evict. The turn died with
# ContextOverflowError and the agent never learned anything it could have used.
#
# These stay IN the maps above on purpose. Classification is what arms this
# guard; deleting the entries would send a .wav down the "file" path and
# base64 it anyway.
#
# Nothing is lost by withholding them. Audio and video are processed by tools
# that take the URL and fetch the bytes themselves (seg_cli's whisper flow,
# the render pipeline). What an agent legitimately needs to know about such a
# URL is whether it resolves — and that is what it is told.
_NON_INLINE_BLOCK_TYPES = frozenset({"audio", "video"})


def _block_type_for(url: str, content_type: str) -> str:
    primary = content_type.split(";")[0].strip().lower()
    for prefix, block_type in _CONTENT_TYPE_TO_BLOCK_TYPE.items():
        if primary == prefix or primary.startswith(f"{prefix}/"):
            return block_type
    from pathlib import PurePosixPath
    from urllib.parse import urlparse

    suffix = PurePosixPath(urlparse(url).path).suffix.lower()
    return _EXTENSION_TO_BLOCK_TYPE.get(suffix, "file")


def _human_bytes(content_length: str | None) -> str:
    """A size for the agent to read, or an honest admission there wasn't one."""
    if not content_length:
        return "size not reported by the server"
    try:
        n = int(content_length)
    except ValueError:
        return "size not reported by the server"
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _accessible_not_inlined(
    url: str,
    *,
    block_type: str,
    content_type: str,
    content_length: str | None,
    status_code: int,
    tool_call_id: str | None,
) -> ToolMessage:
    """Report that an audio/video URL resolves, without returning its bytes.

    ``status="success"`` deliberately: the fetch did what the agent needed. An
    error status would push the agent to retry or to route around a problem
    that does not exist.

    Claims only what was actually tested. A GET was issued and did not return
    4xx/5xx, so the address resolves and the server is serving something of
    this type and size — that is all. The bytes were never read, so this says
    nothing about whether they decode, are complete, or are the right take.
    Saying "valid" here would answer the agent's literal question ("confirm
    the audio is valid") with a check that never ran, and a truncated track
    would sail through to the render.

    The wording says what to do next, because an agent that is only refused
    tends to try again by another route — which is how a 2.5-million-character
    base64 blob reached a model that cannot decode audio.
    """
    mime_type = content_type.split(";")[0].strip() or "application/octet-stream"
    size = _human_bytes(content_length)

    logger.info(
        "fetch_url_not_inlined",
        url=url,
        block_type=block_type,
        mime_type=mime_type,
        content_length=content_length,
        status_code=status_code,
    )

    return ToolMessage(
        content=(
            f"The URL resolves: HTTP {status_code}, {mime_type}, {size}. "
            f"The address is reachable and serving {block_type}.\n\n"
            f"Not checked: whether the bytes decode, are complete, or are the "
            f"take you expect — the body was deliberately not read.\n\n"
            f"Its bytes are not returned here, by design. You cannot read {block_type} "
            f"content, and placing it in the conversation would consume the context "
            f"window without telling you anything.\n\n"
            f"Nothing is blocked. The tools that process {block_type} take this URL and "
            f"fetch the bytes themselves — they are what will detect a bad track. "
            f"Proceed."
        ),
        name="fetch_url",
        tool_call_id=tool_call_id,
        additional_kwargs={
            "fetch_url_source": url,
            "fetch_url_media_type": mime_type,
            "fetch_url_inlined": False,
        },
        status="success",
    )


async def _do_fetch(url: str, tool_call_id: str | None) -> ToolMessage:
    if not url.startswith(("http://", "https://")):
        return ToolMessage(
            content=f"Error: fetch_url only accepts http:// or https:// URLs, got: {url}",
            name="fetch_url",
            tool_call_id=tool_call_id,
            status="error",
        )

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                content_length = response.headers.get("content-length")

                # Decided from headers, so the body is never downloaded for a
                # resource whose bytes are not going to be returned.
                #
                # raise_for_status() above has already rejected 4xx/5xx, so
                # reaching here means the address really does resolve — an
                # expired presigned URL (403) never gets this far. That is a
                # genuine check, not an assumption, and it is the one thing
                # this branch is entitled to report.
                block_type = _block_type_for(url, content_type)
                if block_type in _NON_INLINE_BLOCK_TYPES:
                    # An empty resource is a broken one. Reporting it as
                    # reachable would send the agent onward to a tool that
                    # fetches nothing, and the failure would surface much later
                    # as a silent gap in the render.
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length) == 0
                    ):
                        return ToolMessage(
                            content=(
                                f"Error: {url} resolves but the server reports it is 0 bytes. "
                                f"The {block_type} has not been written yet, or was written empty."
                            ),
                            name="fetch_url",
                            tool_call_id=tool_call_id,
                            status="error",
                        )
                    return _accessible_not_inlined(
                        url,
                        block_type=block_type,
                        content_type=content_type,
                        content_length=content_length,
                        status_code=response.status_code,
                        tool_call_id=tool_call_id,
                    )

                if content_length and int(content_length) > MAX_FETCH_BYTES:
                    return ToolMessage(
                        content=f"Error: resource at {url} is {content_length} bytes, exceeds the {MAX_FETCH_BYTES} byte limit",
                        name="fetch_url",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_FETCH_BYTES:
                        return ToolMessage(
                            content=f"Error: resource at {url} exceeds the {MAX_FETCH_BYTES} byte limit",
                            name="fetch_url",
                            tool_call_id=tool_call_id,
                            status="error",
                        )
    except httpx.HTTPError as e:
        logger.warning("fetch_url_failed", url=url, error=str(e))
        return ToolMessage(
            content=f"Error: failed to fetch {url}: {e}",
            name="fetch_url",
            tool_call_id=tool_call_id,
            status="error",
        )

    if not chunks:
        return ToolMessage(
            content=f"Error: {url} returned no content",
            name="fetch_url",
            tool_call_id=tool_call_id,
            status="success",
        )

    import base64

    mime_type = content_type.split(";")[0].strip() or "application/octet-stream"
    b64_content = base64.b64encode(bytes(chunks)).decode("ascii")

    logger.info(
        "fetch_url_succeeded",
        url=url,
        block_type=block_type,
        mime_type=mime_type,
        bytes=len(chunks),
    )

    return ToolMessage(
        content_blocks=cast(
            list[ContentBlock],
            [{"type": block_type, "base64": b64_content, "mime_type": mime_type}],
        ),
        name="fetch_url",
        tool_call_id=tool_call_id,
        additional_kwargs={"fetch_url_source": url, "fetch_url_media_type": mime_type},
        status="success",
    )


FETCH_URL_TOOL_DESCRIPTION = """Fetch a resource at an http(s) URL and return it as multimodal content.

Use this to view an image or PDF that was referenced to you by URL — for
example, an attachment a user included in their message, forwarded to you as
a plain URL in your task description. The fetched content is returned as the
same kind of image/file block ``read_file`` returns for a local file, so you
can see it directly in this turn.

Audio and video are NOT returned. For those URLs this tool reports only
whether the address resolves. You cannot read audio or video content, and the
tools that process it fetch the bytes from the URL themselves — so there is
never a reason to pull a track or a clip into your context. Passing the URL
along to the tool or subagent that owns that work is the whole job.
"""


class FetchUrlSchema(BaseModel):
    """Args schema for fetch_url — deliberately excludes ``runtime``, which
    is injected by the framework, not supplied by the model."""

    url: str = Field(description="The http:// or https:// URL to fetch.")


def _create_fetch_url_tool() -> BaseTool:
    async def async_fetch_url(url: str, runtime: ToolRuntime) -> ToolMessage:
        return await _do_fetch(url, runtime.tool_call_id)

    def sync_fetch_url(url: str, runtime: ToolRuntime) -> ToolMessage:
        import asyncio

        return asyncio.run(_do_fetch(url, runtime.tool_call_id))

    return StructuredTool.from_function(
        name="fetch_url",
        description=FETCH_URL_TOOL_DESCRIPTION,
        func=sync_fetch_url,
        coroutine=async_fetch_url,
        infer_schema=False,
        args_schema=FetchUrlSchema,
    )


class UrlFetchMiddleware(AgentMiddleware):
    """Provides the ``fetch_url`` tool for viewing attachments referenced by URL."""

    tools = [_create_fetch_url_tool()]
