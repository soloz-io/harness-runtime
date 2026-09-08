"""
Metro process supervision — ported near-verbatim from opencode's
``opencode_event_bridge/metro_supervisor.py``.

``ensure_metro_running()`` is idempotent and safe to call repeatedly (from
session creation and from ``after_agent`` on every turn — see
``core.topology.composite_topology``): it only spawns Metro when nothing
is running yet, discovered fresh each call rather than trusting stale
state, since a crashed process needs to be noticed and respawned.

No Dockerfile change is needed: Expo/Metro isn't baked into the sandbox
image (confirmed — opencode's own working image doesn't either). It
resolves from whatever ``node_modules`` the agent's own shell commands
install per the ``expo-app`` skill's instructions
(``npx create-expo-app@latest ...``).
"""

import asyncio
import glob
import os

import structlog

from core.metro.config import METRO_PORT

# How long a cold `npx expo start --web` takes to open its listening socket
# after being spawned — bounded generously since it's just a TCP accept
# check, not the full bundler warmup. Verified in a live pod: the HMR
# watcher's own reconnect loop went from "connection refused" to connected
# within ~3s of the process actually being spawned; this is a much wider
# margin for slower cold starts.
METRO_READY_TIMEOUT_SECONDS = 45.0
METRO_READY_POLL_INTERVAL_SECONDS = 0.25

logger = structlog.get_logger(__name__)

_metro_process: asyncio.subprocess.Process | None = None
_metro_project_root: str | None = None
_lock = asyncio.Lock()


def _discover_project_root(workspace_dir: str) -> str | None:
    """Bounded, non-recursive glob for a project's node_modules/.bin/expo —
    checks the workspace root and immediate subdirectories only, matching
    opencode's own discovery scope."""
    root_candidate = os.path.join(workspace_dir, "node_modules", ".bin", "expo")
    if os.path.isfile(root_candidate):
        return workspace_dir
    for match in sorted(
        glob.glob(os.path.join(workspace_dir, "*", "node_modules", ".bin", "expo"))
    ):
        return match.rsplit(os.sep + "node_modules", 1)[0]
    return None


def _metro_env() -> dict[str, str]:
    """Environment for the Metro child process.

    HOME=/ in the sandbox image and / is not writable by the runtime user, so
    anything that writes under $HOME dies on startup. Two separate failures
    come from this, both verified in a live pod:

      npm/npx cache ($HOME/.npm) -> `npm ERR! enoent`
      Expo's state dir (/.expo)  -> `ENOENT: mkdir '/.expo'`, Metro exits

    Pointing HOME at a writable location fixes both; npm_config_cache is set
    explicitly as well so the cache location stays stable regardless of HOME.

    CI is deliberately NOT set: `CI=1` puts Metro in no-watch mode
    ("reloads are disabled"), which would silently kill hot reload — the whole
    point of the live preview.
    """
    return {
        **os.environ,
        "HOME": "/tmp",
        "METRO_ENABLED": "1",
        "npm_config_cache": "/tmp/npm-cache",
    }


async def _watch_for_exit(proc: asyncio.subprocess.Process, project_root: str) -> None:
    stderr_tail = ""
    if proc.stderr is not None:
        try:
            stderr_tail = (await proc.stderr.read()).decode("utf-8", "replace")[-2000:]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the exit itself
            stderr_tail = "(unreadable)"
    await proc.wait()
    global _metro_process, _metro_project_root
    # Metro's own stderr was previously sent to DEVNULL, so a Metro that
    # crashed on startup left no trace at all and the preview just returned
    # 500s (httpx.ConnectError) with nothing explaining why.
    log = logger.error if proc.returncode else logger.info
    log(
        "metro_process_exited",
        project_root=project_root,
        returncode=proc.returncode,
        stderr=stderr_tail,
    )
    if _metro_process is proc:
        _metro_process = None
        _metro_project_root = None


async def ensure_metro_running(workspace_dir: str = "/workspace") -> None:
    """Start Metro if it isn't already running for a discovered project.
    No-ops (returns immediately) if no Expo project has been scaffolded
    into the workspace yet, or if Metro is already alive."""
    global _metro_process, _metro_project_root
    if _metro_process is not None and _metro_process.returncode is None:
        return
    async with _lock:
        if _metro_process is not None and _metro_process.returncode is None:
            return
        project_root = _discover_project_root(workspace_dir)
        if project_root is None:
            # Previously a bare `return`. That silence is expensive: with no
            # Expo project installed, Metro never starts, and the preview
            # surfaces only as an opaque 500 (httpx.ConnectError to :8081)
            # with nothing anywhere saying why. Say it plainly instead —
            # node_modules/.bin/expo missing means `npm install` has not
            # succeeded in this workspace yet.
            logger.info(
                "metro_not_started_no_expo_project",
                workspace_dir=workspace_dir,
                looked_for=os.path.join(workspace_dir, "node_modules", ".bin", "expo"),
                node_modules_present=os.path.isdir(os.path.join(workspace_dir, "node_modules")),
            )
            return
        _metro_project_root = project_root
        proc = await asyncio.create_subprocess_exec(
            "npx",
            "expo",
            "start",
            "--web",
            "--port",
            METRO_PORT,
            cwd=project_root,
            env=_metro_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _metro_process = proc
        asyncio.create_task(_watch_for_exit(proc, project_root))
        logger.info("metro_started", project_root=project_root, port=METRO_PORT)
        # A discoverable project (node_modules/.bin/expo present) means
        # whatever ran npm install — bootstrap_app_cli, compile_check_cli,
        # or a cache hit from restore_node_modules_cache — already
        # succeeded. Best-effort cache it now, keyed by package-lock.json's
        # hash (core.workspace.node_modules_cache), so the *next* restore of this
        # workspace, or any other with an identical lockfile, can skip
        # npm install entirely. Fire-and-forget: never blocks Metro
        # startup, and the function itself never raises.
        # node_modules used to be uploaded here as a separate
        # content-addressed archive, because the git-based sync ignored it.
        # node_modules used to be archived here as a separate cache keyed on
        # the lockfile hash, because the old git-based persistence ignored it.
        # Workspace durability is no longer this process's concern at all
        # (ADR-036 §10), and whatever the platform persists includes this
        # directory like any other, so the cache has no job left.


async def wait_for_metro_ready(
    timeout: float = METRO_READY_TIMEOUT_SECONDS,
    interval: float = METRO_READY_POLL_INTERVAL_SECONDS,
) -> bool:
    """Block until Metro is actually accepting TCP connections, or *timeout*
    elapses.

    ``ensure_metro_running()`` returns the instant the subprocess is
    spawned — it does not (and should not, to stay cheap and idempotent)
    wait for the child to actually open its listening socket. Every caller
    that proxies a request to Metro right after calling it was therefore
    racing a freshly-forked process: verified live, the very first preview
    request after a cold bootstrap hit ``httpx.ConnectError: All connection
    attempts failed`` in the same second ``metro_started`` was logged,
    because ``npx expo start`` hadn't bound port 8081 yet. This closes that
    race by polling a real TCP connect instead of assuming readiness.

    Returns ``True`` once a connection succeeds, ``False`` on timeout (the
    caller decides how to fail — a clear 503, not a raw ConnectError).
    """
    if _metro_process is None or _metro_process.returncode is not None:
        # Nothing was ever spawned (no Expo project yet) or it already
        # died — waiting would just burn the whole timeout for nothing.
        return False

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _metro_process.returncode is not None:
            # Died while we were waiting — no point continuing to poll.
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", int(METRO_PORT)), timeout=interval
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                pass
            return True
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(interval)
    logger.warning("metro_ready_wait_timed_out", port=METRO_PORT, timeout=timeout)
    return False
