"""Retrieval of this session's agent pack.

A pack is the agent content one product supplies: its ``definition.json``, the
per-agent ``instructions/``, ``tools/`` and ``skills/`` directories, and the
``bin/`` executables those tools invoke. Waypoint ADR-041 defines the layout and
makes delivery the platform's job rather than each product's.

The pack is fetched from the serve runtime that resolved this session's agent
definition. That runtime already holds the tree — it reads the same one to
hydrate prompts — so nothing is built per product to deliver it and nothing is
mounted into this container.

Fetched once, before the agent loop starts, because :mod:`core.session.skills`
and :mod:`core.session.tools` resolve their directories at startup and a pack
arriving later is a session with no skills and no tools.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Where the pack is fetched from. Set per session by the SDK, derived from the
# definition URL so prompts and tools can never come from two runtimes.
ENV_PACK_URL = "HARNESS_PACK_URL"

# Where it is unpacked. Overridable for tests and for running outside the
# container.
#
# Under /tmp because the sandbox runs with a read-only root filesystem: /app is
# not writable, and of the three volumes that are, /shared is the credential
# channel and /workspace is the session's own tree — which for a durable
# workspace is a PVC checkpointed to object storage, so a pack unpacked there
# would both bury the agent's files and be uploaded with them.
ENV_PACK_DIR = "HARNESS_PACK_DIR"
DEFAULT_PACK_DIR = "/tmp/pack"

_FETCH_TIMEOUT_SECONDS = 60


class PackError(RuntimeError):
    """Raised when the pack cannot be retrieved or does not hold a pack.

    There is no fallback. A session that continued without its pack would start,
    answer its health probe, and run every agent with no instructions — which is
    indistinguishable from a bad prompt until someone reads the transcript.
    """


def pack_dir() -> Path:
    return Path(os.environ.get(ENV_PACK_DIR, DEFAULT_PACK_DIR))


def _safe_members(archive: tarfile.TarFile, dest: Path):
    """Members that land inside ``dest``, and nothing else.

    The archive comes from the platform's own serve runtime, so this is not a
    trust boundary — but an absolute path or a ``..`` traversal in a tar writes
    outside the directory it was meant to fill, and that is worth refusing by
    construction rather than by assuming the producer.
    """
    for member in archive.getmembers():
        if member.issym() or member.islnk():
            raise PackError(f"pack archive contains a link ({member.name}); packs are plain files")
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest.resolve()) + os.sep):
            raise PackError(f"pack archive entry {member.name!r} would be written outside {dest}")
        yield member


def fetch_pack(url: str, dest: Path) -> Path:
    """Fetch the pack at ``url`` and unpack it into ``dest``.

    Returns ``dest``. Raises :class:`PackError` with the URL in the message for
    every failure — an unreachable runtime, a non-200, a body that is not a tar,
    or an archive with no ``agents/`` in it.
    """
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
        try:
            with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise PackError(f"pack fetch from {url} returned HTTP {response.status}")
                while chunk := response.read(1 << 16):
                    tmp.write(chunk)
        except urllib.error.URLError as exc:
            raise PackError(f"pack fetch from {url} failed: {exc}") from exc
        tmp.flush()

        try:
            with tarfile.open(tmp.name) as archive:
                archive.extractall(dest, members=_safe_members(archive, dest))
        except tarfile.TarError as exc:
            raise PackError(f"pack from {url} is not a readable tar archive: {exc}") from exc

    agents = dest / "agents"
    if not agents.is_dir():
        raise PackError(
            f"pack from {url} has no agents/ directory — unpacked: "
            f"[{', '.join(sorted(p.name for p in dest.iterdir())) or 'nothing'}]"
        )

    logger.info(
        "pack.fetched", url=url, dest=str(dest), entries=sorted(p.name for p in dest.iterdir())
    )
    return dest


def ensure_pack() -> Path:
    """Fetch the pack this session was pointed at.

    Raises :class:`PackError` when no pack URL is set. There is nothing to fall
    back to: a sandbox image carries no agent content, so a session that started
    without its pack would run every agent with no instructions while answering
    its health probe perfectly.
    """
    url = os.environ.get(ENV_PACK_URL)
    if not url:
        raise PackError(
            f"{ENV_PACK_URL} is not set, so this session has no agent pack to read. "
            "The SDK sets it from the runtime that served the definition; a sandbox "
            "image carries no prompts, tools or skills of its own."
        )
    return fetch_pack(url, pack_dir())
