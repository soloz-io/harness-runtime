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

# How long `npm install` gets before this gives up waiting on it. Generous —
# matches compile_check_cli's own npm_install budget — because a first
# install of a real Expo project (React Native's dependency tree is large)
# genuinely takes minutes, and this is a background task with nothing else
# waiting on it; a short timeout would just retry into the same slow install
# repeatedly instead of once.
NPM_INSTALL_TIMEOUT_SECONDS = 540.0

_node_modules_install_task: asyncio.Task | None = None

# Shared, by literal path, with the workflow-preview skill's
# compile_check_cli.py — the two do not import from each other (one runs in
# this process, the other is a standalone script dispatched as a run_tool in
# a separate process), so there is no Python-level way to share this
# constant; the path itself is the contract. Whichever side starts an
# `npm install` first creates it and the other waits, so an agent turn that
# calls compile_check_cli moments after a restore does not race THIS
# function's own background install against the same node_modules — observed
# as a real risk, not a theoretical one: `_ensure_node_modules_installing`
# fires from `Session.__init__`, and an agent's first tool call can follow
# within seconds, well inside a real Expo install's runtime.
NPM_INSTALL_LOCK_PATH = "/workspace/.npm-install.lock"


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
    env = {
        **os.environ,
        "HOME": "/tmp",
        "METRO_ENABLED": "1",
        "npm_config_cache": "/tmp/npm-cache",
    }

    # No EXPO_BASE_URL. The app is embedded under `/api/v1/preview/<appId>/`
    # (ADR-033), but nothing in the app needs to be told that: Expo reads a
    # base URL only from `experiments.baseUrl` in the app config, and the
    # scaffolded template has no app.config.js to put one in (waypoint
    # ADR-039 keeps the template untouched). Setting the variable here reached
    # nothing — babel-preset-expo inlines `process.env.EXPO_BASE_URL` from the
    # bundle request's own option, not from this process's environment.
    #
    # The prefix is handled where the URLs actually are: the BFF rewrites
    # `src="/..."` to `src="./..."` in the served HTML, and nginx recovers the
    # appId from the Referer for the root-absolute `/assets/` requests React
    # Native's asset resolver makes. Both are independent of what the app is
    # built from.
    return env


# Hosts npm must reach directly rather than through the agent-vault proxy.
# Verified live: npm cannot authenticate to that proxy (`407 Proxy
# Authentication Required` even with fully-credentialed --https-proxy), and
# agent-vault exists to inject credentials for allowlisted upstreams the
# public npm registry doesn't need any of. Direct egress to it is already
# permitted from the pod. Mirrors compile_check_cli.py's own NPM_DIRECT_HOSTS
# / _npm_env() — duplicated rather than imported, same reason as
# NPM_INSTALL_LOCK_PATH above: that script runs in a separate process this
# module has no import path to.
_NPM_DIRECT_HOSTS = "registry.npmjs.org,.npmjs.org"


def _npm_install_env() -> dict[str, str]:
    """Environment for the background `npm install` specifically — not
    Metro's own env, though it starts from the same HOME/cache fix.

    Without this, this function's first live run hit exactly the proxy
    failure compile_check_cli.py's own docstring already documents in
    detail: `npm ERR! code E407` fetching a package tarball, because the
    sandbox's proxy.env points HTTPS_PROXY at agent-vault and npm cannot
    authenticate to it. Skipped here in a live pod before this fix existed.
    """
    env = dict(_metro_env())
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    combined = f"{existing},{_NPM_DIRECT_HOSTS}" if existing else _NPM_DIRECT_HOSTS
    env["NO_PROXY"] = combined
    env["no_proxy"] = combined
    env["npm_config_proxy"] = "false"
    env["npm_config_https_proxy"] = "false"
    return env


async def _run_npm_install(workspace_dir: str) -> None:
    """Regenerate `node_modules` after a restore, in the background.

    workspace-sync deliberately never persists `node_modules` in a
    checkpoint (zero-ops ADR-052 §14.2's skipDir excludes it — it's large
    and fully regenerable) — so EVERY restore of a workspace that already
    has a real Expo project comes back missing it, every time, not just on
    first bootstrap. A stale comment near the bottom of this module used to
    claim the platform now persists this directory "like any other", which
    is what justified deleting the node_modules cache this function
    replaces — that premise doesn't hold against the sidecar's actual
    skipDir list, and nothing was left to regenerate it automatically. That
    was the actual cause of `ensure_metro_running()` finding no Expo binary
    and permanently no-op'ing after a restore, not a bug in Metro discovery
    itself — discovery is correct; there was simply never anything to find.

    Deliberately `npm install`, not `npm ci`. `npm ci` deletes node_modules
    and reinstalls from the lockfile, which is wrong for the case this most
    often runs in: a restored tree whose packages are all present and whose
    only missing pieces are the `.bin` symlinks. `npm install` repairs those
    in place, and is a no-op when nothing is missing.

    This is now the ONLY thing that installs into a preview workspace. It
    used to share that duty with the agent's own compile_check_cli, which ran
    every turn; that script is gone (ADR-039), so nothing else will notice a
    workspace that cannot start.
    """
    lock_path = os.path.join(workspace_dir, os.path.basename(NPM_INSTALL_LOCK_PATH))
    try:
        # Atomic create-or-fail. A collision means something else is already
        # installing into this tree; back off rather than run a second
        # `npm install` against it concurrently. The lock file is kept (rather
        # than folded into the in-process task guard) because the racing party
        # need not be in this process.
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
    except FileExistsError:
        logger.info("node_modules_install_skipped_lock_held", workspace_dir=workspace_dir)
        return

    logger.info("node_modules_install_started", workspace_dir=workspace_dir)
    installed = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "install",
            cwd=workspace_dir,
            env=_npm_install_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=NPM_INSTALL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error(
                "node_modules_install_timed_out",
                workspace_dir=workspace_dir,
                timeout=NPM_INSTALL_TIMEOUT_SECONDS,
            )
            return
        if proc.returncode != 0:
            logger.error(
                "node_modules_install_failed",
                workspace_dir=workspace_dir,
                returncode=proc.returncode,
                stderr=(stderr or b"").decode("utf-8", "replace")[-2000:],
            )
            return
        logger.info("node_modules_install_completed", workspace_dir=workspace_dir)
        installed = True
    except Exception:  # noqa: BLE001 - a background install must never crash the caller
        logger.error("node_modules_install_errored", workspace_dir=workspace_dir, exc_info=True)
    finally:
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

    # Start Metro NOW, rather than waiting for something else to ask again.
    #
    # This install is the thing that was blocking it: whoever triggered the
    # install got here BECAUSE `node_modules/.bin/expo` was missing, so their
    # own supervision pass necessarily found nothing and returned. Without
    # this, a workspace repairs itself and then sits idle with a usable Expo
    # binary and no Metro — verified live: the install completed at 06:19:06
    # and Metro was still not running, because the only caller had already
    # been and gone at 06:18:24.
    #
    # The lock is released first (above), so this pass sees a clean workspace
    # and takes the normal start path rather than colliding with itself.
    if installed:
        try:
            await ensure_metro_running(workspace_dir)
        except Exception:  # noqa: BLE001 - the install itself succeeded; report and move on
            logger.error(
                "metro_start_after_install_failed", workspace_dir=workspace_dir, exc_info=True
            )


def _ensure_node_modules_installing(workspace_dir: str) -> None:
    """Fire-and-forget an `npm install` if this workspace needs one and
    isn't already getting one.

    Deliberately narrow: only when `package.json` exists (a real project was
    scaffolded — never install into an empty, not-yet-bootstrapped
    workspace) and the project cannot actually run (below). The
    module-level task guards against every one of `ensure_metro_running()`'s
    three callers — session creation, every preview connection, every agent
    turn — piling on a duplicate `npm install` for the same restore; only
    the first caller after a restore actually starts one. The lock file
    `_run_npm_install` acquires is the OTHER half of that guard, covering the
    one caller this in-process task can't see: an agent's own
    compile_check_cli, running as a separate process.
    """
    global _node_modules_install_task
    if _node_modules_install_task is not None and not _node_modules_install_task.done():
        return
    if not os.path.isfile(os.path.join(workspace_dir, "package.json")):
        return
    # The readiness test is `node_modules/.bin/expo`, NOT `node_modules`.
    #
    # A present-but-unusable node_modules is a real, reachable state, and
    # testing only for the directory cannot see it: a restore brings back
    # regular files but no symlinks (the snapshot walk skips anything that is
    # not a regular file), so `node_modules/` reappears fully populated while
    # `.bin/` — which is nothing but symlinks — does not. Metro is launched
    # via `.bin/expo`, so it never starts, and a directory-only check
    # concludes there is nothing to install. Observed live: 358 packages
    # present, zero symlinks, `metro_not_started_no_expo_project` repeating
    # every few seconds forever.
    #
    # This used to be masked rather than handled. `compile_check_cli` ran on
    # every agent turn and installed js-yaml when it was missing, and that
    # npm invocation rebuilt `.bin` as a side effect. That script is gone
    # (ADR-039 removed the compile step), so this is now the only thing that
    # can repair a workspace, and it has to test the condition that actually
    # matters: whether the project can be started, not whether a directory
    # exists.
    if _discover_project_root(workspace_dir) is not None:
        return
    _node_modules_install_task = asyncio.create_task(_run_npm_install(workspace_dir))


def metro_is_running() -> bool:
    """Is the Metro child process alive right now?

    Deliberately a process check and NOT a TCP probe: this backs a poll
    endpoint, and opening a socket on every poll would turn a status check
    into load. `wait_for_metro_ready()` remains the socket-level answer for
    the one place that has to block until Metro is truly accepting
    connections; the gap between "spawned" and "listening" is seconds, and a
    poller closes it by asking again.
    """
    return _metro_process is not None and _metro_process.returncode is None


def node_modules_install_in_progress(workspace_dir: str = "/workspace") -> bool:
    """Is a dependency reinstall running for this workspace right now?

    Checks both halves of the guard `_ensure_node_modules_installing` uses: the
    in-process task, and the lock file that covers an installer started by a
    different process. Either means "not ready YET" rather than "not ready" —
    a distinction the wake endpoint reports so a caller does not tell the user
    to retry something that is already on its way.
    """
    if _node_modules_install_task is not None and not _node_modules_install_task.done():
        return True
    return os.path.exists(os.path.join(workspace_dir, os.path.basename(NPM_INSTALL_LOCK_PATH)))


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
            # Missing purely because a restore doesn't bring it back (see
            # _run_npm_install's docstring) is worth fixing without waiting
            # for an agent turn to notice and run compile_check_cli. A
            # workspace with no package.json at all (never bootstrapped)
            # still no-ops here — nothing to install yet.
            _ensure_node_modules_installing(workspace_dir)
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
        # A discoverable project here means SOME npm install already
        # succeeded — an agent's own bootstrap_app_cli/compile_check_cli
        # call, or _ensure_node_modules_installing() above catching a
        # restore that came back without node_modules. Nothing further to
        # do: workspace-sync will snapshot whatever's on disk, minus
        # node_modules itself (zero-ops ADR-052 §14.2's skipDir), on the
        # next checkpoint — the next restore starts this same search over.


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
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        # Re-read the global every pass, and tolerate None.
        #
        # `_watch_for_exit` sets `_metro_process = None` the moment Metro
        # exits, and that happens on another task while this one is parked in
        # an await. Reading it once up front and dereferencing it later raised
        # `AttributeError: 'NoneType' object has no attribute 'returncode'`
        # inside the proxy — which surfaced to the user as a bare 500 with no
        # explanation, instead of the blank 503 this function exists to enable.
        # Verified live: Metro was dying on every spawn, and the crash hid why.
        proc = _metro_process
        if proc is None or proc.returncode is not None:
            # Never spawned (no Expo project yet), or spawned and already
            # gone — waiting would just burn the whole timeout for nothing.
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
