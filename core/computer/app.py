"""
The session's computer app (waypoint ADR-050 §1-2, §5).

Two declarations decide whether this sandbox has one, and both must hold:

- The workflow definition declares that its sessions have a computer. The SDK
  reads that when it provisions the sandbox and sets ``HARNESS_COMPUTER=1`` in
  the pod's environment -- so a sandbox started by a wake, before any message,
  serves it too.
- The image carries the app at ``/opt/computer`` with its manifest,
  ``computer.json``: ``start`` (the command), ``port``, and optionally
  ``ready`` (default ``/``). Every pack places it the same way, from its
  ``sandbox/computer/`` (the agent-pack contract).

One image can serve several workflows (oranger's channel and video run the same
one); only a definition that declares a computer turns its app on.

The supervisor starts the app when the harness starts, from the session's
workspace, with ``COMPUTER_APP_PORT`` set to the manifest's port, and restarts it
when it exits, backing off so a crashing app cannot spin. The proxy
(api/routers/computer.py) waits on ``wait_ready`` before forwarding.
"""

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

ENV_ENABLED = "HARNESS_COMPUTER"
# Where an image places its computer, overridable for tests.
ENV_DIR = "HARNESS_COMPUTER_DIR"
DEFAULT_DIR = "/opt/computer"
MANIFEST = "computer.json"
WORKSPACE_DIR = "/workspace"

RESTART_BACKOFF_START_S = 1.0
RESTART_BACKOFF_MAX_S = 30.0
# A run this long is healthy: the next exit starts the backoff from the start.
HEALTHY_RUN_S = 60.0


@dataclass(frozen=True)
class ComputerApp:
    command: list[str]
    port: int
    ready_path: str
    cwd: str

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_base_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


def computer_app(env: Optional[dict[str, str]] = None) -> Optional[ComputerApp]:
    """This sandbox's computer app, or None when it has none.

    None unless the definition turned it on (``HARNESS_COMPUTER=1``) and the
    image carries a manifest with a command and a port. A definition that asks
    for a computer on an image with none is logged: the session will show none.
    """
    e = os.environ if env is None else env
    if e.get(ENV_ENABLED) != "1":
        return None
    path = os.path.join(e.get(ENV_DIR) or DEFAULT_DIR, MANIFEST)
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as err:
        logger.warning("computer_declared_but_image_has_no_manifest", path=path, error=str(err))
        return None
    command = shlex.split(str(manifest.get("start") or ""))
    port = manifest.get("port")
    if not command or not isinstance(port, int) or port <= 0:
        logger.warning(
            "computer_manifest_incomplete", path=path, has_start=bool(command), port=port
        )
        return None
    ready_path = str(manifest.get("ready") or "/")
    if not ready_path.startswith("/"):
        ready_path = f"/{ready_path}"
    return ComputerApp(command=command, port=port, ready_path=ready_path, cwd=WORKSPACE_DIR)


class ComputerSupervisor:
    """Keeps the computer app running for the life of the harness."""

    def __init__(self) -> None:
        self._app: Optional[ComputerApp] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stopping = False

    @property
    def app(self) -> Optional[ComputerApp]:
        return self._app

    def start(self, app: Optional[ComputerApp]) -> None:
        """Begin supervising ``app``; a no-op without one."""
        if app is None or self._task is not None:
            return
        self._app = app
        self._stopping = False
        self._task = asyncio.create_task(self._run(app))
        logger.info("computer_app_supervised", command=app.command, port=app.port)

    async def _run(self, app: ComputerApp) -> None:
        backoff = RESTART_BACKOFF_START_S
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *app.command,
                    cwd=app.cwd if os.path.isdir(app.cwd) else None,
                    env={**os.environ, "COMPUTER_APP_PORT": str(app.port)},
                )
                code = await self._process.wait()
                logger.warning("computer_app_exited", code=code)
            except Exception:
                logger.exception("computer_app_failed_to_start", command=app.command)
            if self._stopping:
                return
            if asyncio.get_running_loop().time() - started >= HEALTHY_RUN_S:
                backoff = RESTART_BACKOFF_START_S
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX_S)

    async def wait_ready(self, timeout_s: float = 20.0) -> bool:
        """Whether the app answers its readiness path within ``timeout_s``."""
        app = self._app
        if app is None:
            return False
        import httpx

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    res = await client.get(f"{app.base_url}{app.ready_path}", timeout=2.0)
                    if res.status_code < 500:
                        return True
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    return False
                await asyncio.sleep(0.25)

    async def stop(self) -> None:
        self._stopping = True
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._task:
            self._task.cancel()
        self._task = None


supervisor = ComputerSupervisor()
