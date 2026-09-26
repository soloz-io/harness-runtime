"""The pack a session runs on arrives from the serve runtime (waypoint ADR-041).

What these cover is the failure that is otherwise silent: a session that starts
without its content and runs every agent with no instructions.
"""

import io
import os
import tarfile
from pathlib import Path

import pytest

from core.session.pack import ENV_PACK_DIR, ENV_PACK_URL, PackError, ensure_pack, fetch_pack


def _tar(entries: dict[str, str], *, link: str | None = None) -> bytes:
    """A tar in the shape the serve runtime streams."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content.encode()))
        if link:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    return buf.getvalue()


@pytest.fixture
def serve(monkeypatch):
    """Stands in for the serve runtime's /packs/<pack>/content route."""

    def _serve(body: bytes, status: int = 200):
        class _Response:
            def __init__(self):
                self.status = status
                self._body = io.BytesIO(body)

            def read(self, n=-1):
                return self._body.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _Response())

    return _serve


def test_pack_is_unpacked_where_the_harness_reads_it(serve, tmp_path):
    serve(
        _tar(
            {
                "definition.json": "{}",
                "agents/orchestrator/instructions/00.md": "# hi",
                "bin/cli.cjs": "x",
            }
        )
    )

    dest = fetch_pack("http://serve/packs/oranger/content", tmp_path / "pack")

    assert (dest / "agents" / "orchestrator" / "instructions" / "00.md").read_text() == "# hi"
    assert (dest / "bin" / "cli.cjs").exists()
    assert (dest / "definition.json").exists()


def test_a_pack_without_agents_is_refused(serve, tmp_path):
    # An archive that unpacks cleanly but holds no agents leaves every node
    # without instructions. Failing here names the URL; failing later does not.
    serve(_tar({"definition.json": "{}"}))

    with pytest.raises(PackError, match="no agents/"):
        fetch_pack("http://serve/packs/oranger/content", tmp_path / "pack")


def test_an_unreachable_runtime_names_the_url(serve, tmp_path, monkeypatch):
    import urllib.error

    def _boom(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    with pytest.raises(PackError, match="http://serve/packs/oranger/content"):
        fetch_pack("http://serve/packs/oranger/content", tmp_path / "pack")


def test_a_body_that_is_not_a_tar_is_refused(serve, tmp_path):
    serve(b"<html>404</html>")

    with pytest.raises(PackError, match="not a readable tar"):
        fetch_pack("http://serve/packs/oranger/content", tmp_path / "pack")


def test_an_entry_outside_the_destination_is_refused(serve, tmp_path):
    serve(_tar({"../escaped.md": "x", "agents/orchestrator/instructions/00.md": "# hi"}))

    with pytest.raises(PackError, match="outside"):
        fetch_pack("http://serve/packs/oranger/content", tmp_path / "pack")
    assert not (tmp_path / "escaped.md").exists()


def test_a_link_in_the_archive_is_refused(serve, tmp_path):
    serve(_tar({"agents/orchestrator/instructions/00.md": "# hi"}, link="agents/secret"))

    with pytest.raises(PackError, match="link"):
        fetch_pack("http://serve/packs/oranger/content", tmp_path / "pack")


def test_no_pack_url_means_a_sandbox_carrying_its_own_content(monkeypatch):
    # What keeps products moving onto the contract one at a time: an image that
    # still bakes its agents in runs unchanged.
    monkeypatch.delenv(ENV_PACK_URL, raising=False)
    assert ensure_pack() is None


def test_the_pack_url_is_all_a_session_needs_to_be_pointed_at(serve, tmp_path, monkeypatch):
    serve(_tar({"definition.json": "{}", "agents/orchestrator/instructions/00.md": "# hi"}))
    monkeypatch.setenv(ENV_PACK_URL, "http://serve/packs/oranger/content")
    monkeypatch.setenv(ENV_PACK_DIR, str(tmp_path / "pack"))

    dest = ensure_pack()

    assert dest == Path(os.environ[ENV_PACK_DIR])
    assert (dest / "agents" / "orchestrator").is_dir()
