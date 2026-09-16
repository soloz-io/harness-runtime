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
image/audio/video/file reads, so a vision-capable model sees the fetched
image exactly as it would see one read from disk.

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


def _block_type_for(url: str, content_type: str) -> str:
    primary = content_type.split(";")[0].strip().lower()
    for prefix, block_type in _CONTENT_TYPE_TO_BLOCK_TYPE.items():
        if primary == prefix or primary.startswith(f"{prefix}/"):
            return block_type
    from pathlib import PurePosixPath
    from urllib.parse import urlparse

    suffix = PurePosixPath(urlparse(url).path).suffix.lower()
    return _EXTENSION_TO_BLOCK_TYPE.get(suffix, "file")


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

    block_type = _block_type_for(url, content_type)
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

Use this to view an image, video, audio clip, or PDF that was referenced to
you by URL — for example, an attachment a user included in their message,
forwarded to you as a plain URL in your task description. The fetched
content is returned as the same kind of image/video/audio/file block
``read_file`` returns for a local file, so you can see it directly in this
turn.
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
