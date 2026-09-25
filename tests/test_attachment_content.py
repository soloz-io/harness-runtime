"""Unit tests for core.session.execution attachment translation.

The SDK forwards *every* attachment (images and voice notes alike) — this
module is the single translator between the attachment list and what the
provider receives. Two failure modes are pinned here:

1. An audio attachment turned into an image block makes the provider
   answer ``400`` and kills the whole turn.
2. An audio-only send (no typed words) that produces empty content leaves
   the model with nothing to answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.session.execution import (
    _build_message_content,
    inline_object_attachments,
    prepare_turn_input,
)

S3_AUDIO = "https://hel1.your-objectstorage.com/bucket/note.webm"
S3_IMAGE = "https://hel1.your-objectstorage.com/bucket/shot.png"


def audio_object(url: str = S3_AUDIO) -> dict[str, Any]:
    return {
        "kind": "audio",
        "mime": "audio/webm",
        "filename": "note.webm",
        "source": {"type": "object", "value": url},
    }


def image_object(url: str = S3_IMAGE) -> dict[str, Any]:
    return {"kind": "image", "mime": "image/png", "source": {"type": "object", "value": url}}


def blocks_of(content: Any) -> list[dict[str, Any]]:
    assert isinstance(content, list), f"expected content blocks, got {content!r}"
    return content


def test_audio_only_with_no_words_is_still_non_empty():
    content = blocks_of(_build_message_content("", [audio_object()]))
    assert content == [{"type": "text", "text": f"[Attached audio: {S3_AUDIO}]"}]


def test_audio_never_becomes_an_image_block():
    content = blocks_of(_build_message_content("narrate this", [audio_object()]))
    assert [b["type"] for b in content] == ["text", "text"]
    assert content[0]["text"] == "narrate this"
    assert content[1]["text"] == f"[Attached audio: {S3_AUDIO}]"
    assert all("data" not in b for b in content)


def test_audio_data_source_falls_back_to_filename_never_base64():
    attachment = audio_object()
    attachment["source"] = {"type": "data", "value": "data:audio/webm;base64,AAAA"}
    content = blocks_of(_build_message_content("", [attachment]))
    assert content == [{"type": "text", "text": "[Attached audio: note.webm]"}]


def test_audio_data_source_without_filename_falls_back_to_mime():
    attachment = audio_object()
    attachment.pop("filename")
    attachment["mime"] = "audio/wav"
    attachment["source"] = {"type": "data", "value": "data:audio/wav;base64,AAAA"}
    content = blocks_of(_build_message_content("", [attachment]))
    assert content == [{"type": "text", "text": "[Attached audio: audio/wav]"}]


def test_image_object_still_becomes_text_plus_image_block():
    content = blocks_of(_build_message_content("", [image_object()]))
    assert content == [
        {"type": "text", "text": f"[Attached image: {S3_IMAGE}]"},
        {
            "type": "image",
            "source_type": "url",
            "url": S3_IMAGE,
            "mime_type": "image/png",
        },
    ]


def test_mixed_image_and_audio():
    content = blocks_of(_build_message_content("compare these", [image_object(), audio_object()]))
    # user text, image's URL line, image block, audio's URL line
    assert [b["type"] for b in content] == ["text", "text", "image", "text"]
    assert content[1]["text"] == f"[Attached image: {S3_IMAGE}]"
    assert content[3]["text"] == f"[Attached audio: {S3_AUDIO}]"


def test_no_attachments_returns_plain_text():
    assert _build_message_content("hello", None) == "hello"
    assert _build_message_content("hello", []) == "hello"


def test_prepare_turn_input_appends_content_for_audio_only_turn():
    payload = prepare_turn_input({"messages": []}, "", "user", [audio_object()])
    messages = payload["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = blocks_of(messages[0]["content"])
    assert content == [{"type": "text", "text": f"[Attached audio: {S3_AUDIO}]"}]


class _SpyClient:
    """AsyncClient stand-in that records fetches instead of doing I/O."""

    fetched: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_SpyClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str) -> Any:
        _SpyClient.fetched.append(url)

        class _Response:
            content = b"\x89PNG\r\n\x1a\n"

            def raise_for_status(self) -> None:
                return None

        return _Response()


@pytest.fixture()
def spy_client(monkeypatch: pytest.MonkeyPatch) -> _SpyClient:
    _SpyClient.fetched = []
    monkeypatch.setattr("core.session.execution.httpx.AsyncClient", _SpyClient)
    return _SpyClient()


async def test_inline_skips_audio_object_attachments(spy_client: _SpyClient) -> None:
    result = await inline_object_attachments([audio_object()])
    assert result == [audio_object()]
    assert spy_client.fetched == []


async def test_inline_still_fetches_image_object_attachments(spy_client: _SpyClient) -> None:
    result = await inline_object_attachments([image_object()])
    assert spy_client.fetched == [S3_IMAGE]
    assert result is not None
    assert result[0]["source"]["type"] == "data"
    assert result[0]["source_url"] == S3_IMAGE
