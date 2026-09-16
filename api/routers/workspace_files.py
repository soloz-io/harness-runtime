"""The agent's working workspace: a read-only view, and a save trigger.

The Builder's text editor used to show rows from ``agent_output_files``, a
projection of the graph's ``last_files`` state key. That projection only exists
for the DB-backed ``DBBackend``: deepagents' ``FilesystemBackend``
writes to real disk and never calls ``send([("files", ...)])``, so the moment a
specialist opts into ``persistent_workspace: "s3"`` the projection went
permanently empty and the editor had nothing to show.

Rather than reinstate the copy, this serves the tree itself. The editor shows
what the agent is actually working on — including files written by ``npm
install``, a shell command, or anything else that never went through a tool the
graph observes, none of which a state projection could ever have captured.

The file endpoints are READ-ONLY, deliberately. The editor displays the
workspace; it does not own it. An edit endpoint here would let the UI race the
agent for the same files with no locking and no checkpoint boundary.

``POST /workspace/checkpoint`` is the exception, and it does not write the
workspace either — it asks the platform's workspace-sync sidecar to snapshot it.
That sidecar binds ``127.0.0.1``, so a request from outside the pod cannot reach
it (zero-ops ADR-052 §14.1); this process shares the pod's network namespace and
is therefore the only thing that can. The same reason ``preview.py`` proxies
Metro on localhost.

This is a PROXY, not a policy. The harness does not decide when to checkpoint,
does not know what a checkpoint contains, and holds no object-store credential
(ADR-037 §1). It forwards a request a human made.


Auth reuses ``WAYPOINT_INTERNAL_TOKEN``, the same convention as ``preview.py``.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspace"])

ROOT_DIR = os.environ.get("WORKSPACE_ROOT", "/workspace")

# Never walked into. `.git` is the big one — an Expo app's object store is tens
# of thousands of files and none of them mean anything in an editor — but each
# of these is both large and uninteresting to a human reading their own code.
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".expo",
    ".next",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    ".ruff_cache",
    ".turbo",
    ".cache",
}

# A listing is for browsing, not for transferring a repository. A workspace with
# a stray generated directory would otherwise return a response big enough to
# stall the browser, and the cap makes that a truncated list instead.
_MAX_ENTRIES = 5000

# Files above this are listed but never inlined. The editor asks for content
# per file, so nothing is lost except the ability to open something no one can
# usefully read.
_MAX_READ_BYTES = 2 * 1024 * 1024

# A checkpoint walks the whole tree, uploads what is new, and builds a squashfs
# image. On a fresh workspace with node_modules that is tens of seconds; the
# ceiling is generous so a slow first save reports success rather than a
# timeout the user reads as failure.
_CHECKPOINT_TIMEOUT_SECONDS = 180.0

# Memory protection bound for ZIP creation in memory. Prevents arbitrarily
# large workspaces from exhausting container memory during archive buffering.
_MAX_DOWNLOAD_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MiB uncompressed limit


def _is_authorized(token: Optional[str]) -> bool:
    if os.environ.get("WAYPOINT_ENV") == "local":
        return True
    expected = os.environ.get("WAYPOINT_INTERNAL_TOKEN")
    if not expected:
        return os.environ.get("WAYPOINT_ENV") != "production"
    return token == expected


def _require_auth(request: Request) -> None:
    if not _is_authorized(request.headers.get("x-waypoint-internal-token")):
        raise HTTPException(status_code=401, detail="Missing or invalid x-waypoint-internal-token")


def _resolve_within_root(rel_path: str) -> Path:
    """Resolve a caller-supplied path, or refuse it.

    Two separate jobs, and both have bitten this codebase before.

    First, normalisation. Callers legitimately hold BOTH shapes: the listing
    returns paths relative to the root, while the agent, the LLM transcript and
    every tool speak absolute ``/workspace/...``. Joining the absolute form onto
    the root without stripping it produces ``/workspace/workspace/...`` — the
    exact doubling documented in ``core/agent_backend.py``, where it surfaced as
    ``path_not_found`` on a live pod rather than as anything about paths.

    Second, containment. The value arrives in a query parameter, so
    ``../../etc/passwd`` is a thing this will be asked for. Resolving first and
    comparing afterwards is what makes that check correct: a prefix test on the
    raw string passes for ``/workspace/../etc``, and symlinks defeat any purely
    lexical check regardless.
    """
    root = Path(ROOT_DIR).resolve()

    cleaned = rel_path.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="path is required")
    # Accept the absolute form the rest of the system uses.
    if cleaned == ROOT_DIR:
        cleaned = ""
    elif cleaned.startswith(ROOT_DIR + "/"):
        cleaned = cleaned[len(ROOT_DIR) + 1 :]
    cleaned = cleaned.lstrip("/")

    candidate = (root / cleaned).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=400, detail="path escapes the workspace")
    return candidate


def _walk(root: Path) -> tuple[list[dict[str, Any]], bool]:
    files: list[dict[str, Any]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        # Pruned in place so os.walk does not descend — filtering the output
        # instead would still pay the cost of walking node_modules.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name == ".DS_Store":
                continue
            if len(files) >= _MAX_ENTRIES:
                truncated = True
                return files, truncated
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                # Raced against the agent writing the tree. A file that
                # vanished between listing and stat is not an error worth
                # failing the whole request for.
                continue
            files.append(
                {
                    "path": str(full.relative_to(root)),
                    "size": st.st_size,
                    "modified_at": st.st_mtime,
                }
            )
    return files, truncated


@router.get("/files")
async def list_files(request: Request) -> dict[str, Any]:
    """The workspace tree, as paths and metadata. No content."""
    _require_auth(request)
    root = Path(ROOT_DIR)
    if not root.is_dir():
        # Not an error: a session whose sandbox has not scaffolded anything yet
        # has an empty workspace, and an empty editor is the honest rendering.
        return {"root": ROOT_DIR, "files": [], "truncated": False}

    files, truncated = _walk(root.resolve())
    files.sort(key=lambda f: f["path"])
    if truncated:
        logger.warning("workspace_listing_truncated", limit=_MAX_ENTRIES, root=ROOT_DIR)
    return {"root": ROOT_DIR, "files": files, "truncated": truncated}


@router.get("/file")
async def read_file(request: Request, path: str = Query(...)) -> dict[str, Any]:
    """One file's content, as text."""
    _require_auth(request)
    full = _resolve_within_root(path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail=f"no such file: {path}")

    st = full.stat()
    if st.st_size > _MAX_READ_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file is {st.st_size} bytes, above the {_MAX_READ_BYTES} limit",
        )

    try:
        content = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Binary. Reported rather than returned as mojibake or as a 500, so the
        # editor can say "binary file" instead of rendering garbage.
        raise HTTPException(status_code=415, detail="file is not valid UTF-8 text") from None

    return {
        "path": str(full.relative_to(Path(ROOT_DIR).resolve())),
        "content": content,
        "size": st.st_size,
        "modified_at": st.st_mtime,
    }


def _build_workspace_zip(root: Path) -> bytes:
    """Build the complete workspace ZIP in memory and return the bytes.

    ZipFile records the byte offsets of each local file header for use in the
    central directory.  Truncating the underlying buffer between yielded chunks
    changes the archive's byte layout, so when the central directory is finally
    written on close(), its recorded offsets no longer point to the corresponding
    local headers (surfacing as macOS Archive Utility 'Error 94 — Bad message').

    Building into a single BytesIO buffer and finalizing the archive before
    transmission produces a valid, compliant ZIP.  To prevent unbounded memory
    consumption on arbitrarily large workspaces, we verify total uncompressed
    file size before compression and raise HTTP 413 if the workspace exceeds
    _MAX_DOWNLOAD_UNCOMPRESSED_BYTES.

    Intentional exclusions:
    Directories pruned during walk (_SKIP_DIRS: node_modules, .git, dist, build,
    __pycache__, .venv, .expo, .next, .turbo, .cache) are intentionally omitted
    from project export.  A project download is meant for offline source inspection
    and development, not repository snapshotting (which uses object storage).

    Binary files (images, compiled assets, .sqlite, …) are included verbatim:
    unlike /workspace/file which rejects non-text for editor display, a zip
    download faithfully bundles all static assets.
    """
    files, _ = _walk(root)
    files.sort(key=lambda f: f["path"])

    total_size = sum(f.get("size", 0) for f in files)
    if total_size > _MAX_DOWNLOAD_UNCOMPRESSED_BYTES:
        limit_mb = _MAX_DOWNLOAD_UNCOMPRESSED_BYTES // (1024 * 1024)
        actual_mb = total_size // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Workspace size ({actual_mb}MB) exceeds maximum download limit ({limit_mb}MB)",
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for entry in files:
            full = root / entry["path"]
            try:
                data = full.read_bytes()
            except OSError:
                # File disappeared between _walk and now — skip silently.
                continue
            zf.writestr(entry["path"], data)

    # ZipFile.close() has now written the central directory; the archive is valid.
    return buf.getvalue()


@router.get("/download")
async def download_workspace(request: Request) -> StreamingResponse:
    """Return the full workspace as a ZIP archive.

    Excluded dirs: node_modules, .git, dist, build, __pycache__, .venv,
    .expo, .next, .turbo (same as ``/workspace/files``).

    Binary files are included verbatim (unlike ``/workspace/file`` which
    refuses them for the editor).
    """
    _require_auth(request)
    root = Path(ROOT_DIR).resolve()

    # Build the complete archive before responding — required for a valid ZIP
    # (see _build_workspace_zip for the reason).
    zip_bytes = _build_workspace_zip(root) if root.is_dir() else _empty_zip()

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="workspace.zip"'},
    )


def _empty_zip() -> bytes:
    """Return a valid empty ZIP archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    return buf.getvalue()


@router.post("/checkpoint")
async def checkpoint(request: Request) -> dict[str, Any]:
    """Ask the workspace-sync sidecar for a snapshot, now.

    Forwarded verbatim, including the sidecar's own status codes, because each
    one means something the caller needs to distinguish:

    - ``200`` with an empty ``checkpointId`` — object storage is not configured
      on this deployment. A legitimate no-op (§14), not a failure.
    - ``409`` — the workspace is read-only (a build job, §14.3).
    - ``503`` — no sidecar in this pod, i.e. the job asked for no workspace
      persistence. Collapsing that into a 500 would make "this sandbox is not
      persistent" look like "saving is broken".
    """
    _require_auth(request)

    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        # An empty body is the ordinary case — a Save button sends no name.
        body = {}
    payload = {
        "name": (body or {}).get("name") or "",
        "description": (body or {}).get("description") or "",
    }

    port = os.environ.get("WORKSPACE_SYNC_PORT", "7070")
    url = f"http://127.0.0.1:{port}/checkpoint"

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_CHECKPOINT_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
    except httpx.ConnectError:
        # Nothing listening on the sidecar port. The pod was created without
        # workspacePersistence, so there is nothing to save to — a real answer,
        # not an error to retry.
        raise HTTPException(
            status_code=503,
            detail="This sandbox has no workspace persistence; there is nothing to save to.",
        ) from None
    except httpx.TimeoutException:
        # A first snapshot of a large workspace is genuinely slow (it walks the
        # tree, uploads new objects, and builds a squashfs image). Say so,
        # rather than reporting a generic failure for something still running.
        raise HTTPException(
            status_code=504,
            detail=f"Checkpoint did not complete within {_CHECKPOINT_TIMEOUT_SECONDS}s; it may still be running.",
        ) from None

    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text}

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=data)
    return data
