"""
Preview streaming — ADR-036 §7 (enterprise-hardening plan).

All routes behind the same auth check (§2 of the hardening plan — reuses
``WAYPOINT_INTERNAL_TOKEN``, the SDK's own ``/internal/*`` convention, rather
than inventing a new one):

- HTTP proxy to Metro's own server (``localhost:8081``) — allowlisted (§3),
  not a true catch-all, since Metro's dev server exposes more than bundle
  serving and was never designed to be reachable outside a developer's own
  machine.
- ``POST /wake`` / ``GET /wake/status`` — start this sandbox's Metro and
  report where that got to.
- ``/hot`` / ``/message`` WebSocket relay to Metro's own sockets — the
  actual HMR channel. Distinct from ``core/metro/watcher.py``'s outbound-
  only connection (that one exists purely to trigger checkpoints; this one
  is what the browser's preview actually uses).

There is deliberately no route serving the app's flow. Nothing in the
workspace describes it: the canvas reads the agent's own source through
``/workspace/files`` and extracts the graph itself (waypoint ADR-039). The
``GET /routes`` endpoint that served a compiled ``preview.json`` is gone with
the compile step that wrote it — an endpoint whose file no longer has a
producer answers "empty graph" forever, which reads as a broken app rather
than as a removed feature.
"""

import json
import os
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from core.metro.config import METRO_URL
from core.metro.supervisor import (
    ensure_metro_running,
    metro_is_running,
    node_modules_install_in_progress,
    wait_for_metro_ready,
)
from core.workspace_context import get_active_app_id

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["preview"])


# ── Auth (§2: reuse WAYPOINT_INTERNAL_TOKEN, the SDK's own convention) ─────


def _is_authorized(token: Optional[str]) -> bool:
    if os.environ.get("WAYPOINT_ENV") == "local":
        return True
    expected = os.environ.get("WAYPOINT_INTERNAL_TOKEN")
    if not expected:
        # Matches the SDK's own isInternalAuthorized: unset token is only
        # tolerated outside production.
        return os.environ.get("WAYPOINT_ENV") != "production"
    return token == expected


def _require_auth_http(request: Request) -> None:
    if not _is_authorized(request.headers.get("x-waypoint-internal-token")):
        raise HTTPException(status_code=401, detail="Missing or invalid x-waypoint-internal-token")


async def _require_auth_ws(websocket: WebSocket) -> bool:
    if not _is_authorized(websocket.headers.get("x-waypoint-internal-token")):
        await websocket.close(code=4401)
        return False
    return True


# ── HTTP proxy to Metro (§3: allowlisted, not a catch-all) ─────────────────

_ALLOWED_SUFFIXES = (
    ".bundle",
    ".map",
    ".js",
    ".css",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
)
_ALLOWED_PREFIXES = ("/assets/", "/_expo/")
_ALLOWED_EXACT = ("/", "/symbolicate")


def _path_allowed(path: str) -> bool:
    if path in _ALLOWED_EXACT:
        return True
    if path.startswith(_ALLOWED_PREFIXES):
        return True
    return path.endswith(_ALLOWED_SUFFIXES)


@router.post("/wake")
async def wake(request: Request) -> dict:
    """Make this sandbox's preview serveable, and say whether it is.

    THE single entry point for "start the preview". Everything that wants a
    running preview calls this — the Wake control in the UI goes SDK ->
    ensureSandbox -> here, and the in-pod triggers below call the same
    ``ensure_metro_running()`` this does.

    It exists because provisioning a pod and having a preview are not the same
    thing, and until now only the pod half had a caller. Metro was started
    exclusively as a side effect of four unrelated events — a session being
    created, an agent turn starting, a preview websocket connecting, a preview
    HTTP request arriving — so a user who provisioned a sandbox and then simply
    waited got a pod that never served anything. Waking is now something that
    can be *asked for* rather than only stumbled into.

    ``ensure_metro_running()`` is idempotent and repairs as well as starts: if
    ``node_modules/.bin/expo`` is missing (a restored workspace never gets its
    symlinks back) it kicks off the reinstall that recreates it. So calling
    this on a healthy sandbox costs a discovery check, and calling it on a
    broken one is the fix.

    Reports rather than raises. "Metro did not come up in time" is an ordinary
    outcome on a cold start — the install alone can outlast any sane request
    timeout — and the caller needs to distinguish it from "the sandbox is
    unreachable", which a 5xx here would collapse into.
    """
    _require_auth_http(request)

    try:
        await ensure_metro_running()
    except Exception:
        logger.warning("preview_wake_supervision_failed", exc_info=True)
        return {"metroReady": False, "reason": "supervision-failed"}

    ready = await wait_for_metro_ready()
    logger.info("preview_wake", metro_ready=ready)
    # `installing` distinguishes the two ways `ready` is false: a workspace
    # whose dependencies are still being regenerated WILL come up on its own,
    # and telling the user to retry would be wrong. Anything else will not.
    return {
        "metroReady": ready,
        "reason": None
        if ready
        else ("installing" if node_modules_install_in_progress() else "not-ready"),
    }


@router.get("/wake/status")
async def wake_status(request: Request) -> dict:
    """Poll target for "is the preview serving yet".

    Separate from POST /wake on purpose, and cheap on purpose: it starts
    nothing and waits for nothing, so it is safe to call on a short interval.
    POST /wake blocks for up to the Metro-ready timeout and can kick off a
    multi-minute dependency install; polling THAT would stack supervision
    passes and hold a connection open for the whole install.

    So the contract is: POST /wake once to ask, then GET /wake/status until
    ``metroReady``. The reasons are the same vocabulary both endpoints use, so
    a caller does not have to translate between them.
    """
    _require_auth_http(request)
    ready = metro_is_running()
    return {
        "metroReady": ready,
        "reason": None
        if ready
        else ("installing" if node_modules_install_in_progress() else "not-ready"),
    }


# ── /hot and /message WebSocket relay to local Metro ───────────────────────


async def _relay(websocket: WebSocket, metro_path: str) -> None:
    import websockets

    if not await _require_auth_ws(websocket):
        return
    await websocket.accept()

    # Supervision is otherwise only triggered at the start of a turn
    # (Session.__init__, _build_deep_agent_spec) — and the turn that runs
    # `npm install` (via run_tool) necessarily checks for the Expo binary
    # *before* that install completes, so it always no-ops. Nothing
    # re-checks afterward unless another turn happens to start. Observed
    # directly: a session finished installing deps and compiling with Metro
    # never once started, and the preview failed with a bare ConnectError
    # until manually triggered. ensure_metro_running() is idempotent and
    # safe to call unconditionally on every connection.
    try:
        await ensure_metro_running()
    except Exception:
        logger.warning("preview_ws_metro_supervision_failed", metro_path=metro_path, exc_info=True)

    # ensure_metro_running() returns the instant the subprocess is spawned,
    # not once it's actually accepting connections (see
    # wait_for_metro_ready's docstring — verified live, a cold-started
    # Metro loses this race every time). Wait for the real socket instead
    # of immediately handing the first connection to a closed port.
    if not await wait_for_metro_ready():
        logger.warning("preview_ws_metro_not_ready", metro_path=metro_path)
        await websocket.close(code=1013, reason="Metro is still starting — retry shortly")
        return

    try:
        async with websockets.connect(f"{METRO_URL}{metro_path}") as upstream:
            import asyncio

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await upstream.send(msg)
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client() -> None:
                async for msg in upstream:
                    if isinstance(msg, str):
                        await websocket.send_text(msg)
                    else:
                        await websocket.send_bytes(msg)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception:
        logger.warning("preview_ws_relay_failed", metro_path=metro_path, exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/hot")
async def hot(websocket: WebSocket) -> None:
    await _relay(websocket, "/hot")


@router.websocket("/message")
async def message(websocket: WebSocket) -> None:
    await _relay(websocket, "/message")


# ── HTTP proxy to Metro (§3: allowlisted, not a catch-all) ─────────────────
#
# Registered LAST, deliberately: FastAPI matches routes in registration
# order, and "/{path:path}" matches literally any path — declared any earlier
# in this file, it would shadow /wake and the WebSocket relays above before
# their own handlers ever ran.


def _websocket_appid_patch_script(app_id: str) -> str:
    """A script injected into the preview's own HTML, before Metro's bundle
    <script> tag — so before Metro's own HMR client ever opens its /hot or
    /message socket.

    Why this exists: BFF's WS upgrade handler (bff/src/index.ts) needs to
    know which app's sandbox to relay a given /hot connection to, since one
    BFF instance sits in front of many sandbox pods. It originally relied
    on the browser sending `Referer` on the WS handshake — verified live
    that this does not hold (confirmed via nginx's own access log showing
    no Referer on that request, even with a same-origin
    strict-origin-when-cross-origin policy explicitly set). WebSocket
    handshake referrer transmission is evidently not reliable enough to
    route on.

    Metro's own web client computes its socket URL as `${origin}/hot`
    (origin only, no path or query — verified against its actual source)
    and we can't edit that bundled code. So instead: monkey-patch
    `window.WebSocket` here, before that client module ever runs, to add
    `?appId=...` to exactly those two paths — appId comes from this
    server's own session state (`get_active_app_id()`), never anything the
    browser could fail to send.

    The same patch also REPORTS Metro's HMR lifecycle to the canvas.

    Metro's /hot socket carries an explicit rebuild lifecycle —
    ``update-start`` / ``update`` / ``update-done`` / ``error`` (see
    metro-runtime's HMRClient, which switches on exactly these). The canvas
    needs it because the coding agent writes a feature one file at a time:
    between the first write and the last, the app genuinely does not compile
    (App.tsx referencing a screen that does not exist yet), and a preview
    node that re-renders into that half-state looks like a crash rather than
    like work in progress. Knowing a build is in flight lets the canvas hold
    the last good frame and show a "building" indicator instead.

    It is done HERE, in the injected script, rather than in the app's own
    previewBridge for two reasons: this file already owns the /hot socket, so
    there is one interception point instead of two; and it works for an app
    whose sources predate the feature, which matters because an existing
    workspace is only regenerated when the agent next rewrites it.

    Deliberately observation-only — it forwards what Metro says and never
    suppresses, delays, or synthesises a message. The socket behaves exactly
    as it did unpatched; `onmessage` assignment and `addEventListener` both
    still reach Metro's own client untouched.
    """
    return (
        "<script>(function(){"
        f"var appId={json.dumps(app_id)};"
        "if(!appId)return;"
        "var Native=window.WebSocket;"
        # Report to the canvas. Same-origin (the canvas frames this page), so
        # targetOrigin is the page's own origin rather than '*'.
        "function report(kind,body){try{if(window.parent&&window.parent!==window){"
        "window.parent.postMessage({type:'expo-app:bundle',phase:kind,body:body,appId:appId},"
        "window.location.origin);}}catch(e){}}"
        "function watchHmr(ws){"
        "ws.addEventListener('message',function(ev){"
        "var t;try{t=JSON.parse(String(ev.data)).type;}catch(e){return;}"
        # heartbeat/bundle-registered are noise for this purpose; the four
        # below are the whole lifecycle the canvas gates on.
        "if(t==='update-start'||t==='update-done'||t==='error'){report(t);}"
        "});"
        "ws.addEventListener('close',function(){report('disconnected');});"
        "}"
        "function Patched(url,protocols){"
        "var isHot=false;"
        "try{"
        "var u=new URL(url,window.location.href);"
        "isHot=(u.pathname==='/hot');"
        "if((isHot||u.pathname==='/message')&&!u.searchParams.has('appId')){"
        "u.searchParams.set('appId',appId);url=u.toString();"
        "}"
        "}catch(e){}"
        "var ws=Reflect.construct(Native,protocols===undefined?[url]:[url,protocols]);"
        "if(isHot){try{watchHmr(ws);}catch(e){}}"
        "return ws;"
        "}"
        "Patched.prototype=Native.prototype;"
        "Patched.CONNECTING=Native.CONNECTING;Patched.OPEN=Native.OPEN;"
        "Patched.CLOSING=Native.CLOSING;Patched.CLOSED=Native.CLOSED;"
        "window.WebSocket=Patched;"
        "})();</script>"
    )


def _inject_websocket_appid_patch(html: bytes, app_id: Optional[str]) -> bytes:
    if not app_id:
        return html
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        return html
    script = _websocket_appid_patch_script(app_id)
    if "<head>" in text:
        return text.replace("<head>", f"<head>{script}", 1).encode("utf-8")
    if "<script" in text:
        return text.replace("<script", f"{script}<script", 1).encode("utf-8")
    # Neither anchor found — an HTML shape we don't recognize. Return
    # unmodified rather than guessing at a splice point; the preview still
    # loads, just without live-reload routing, which is the same degraded
    # state as before this patch existed.
    return html


@router.api_route("/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def proxy_to_metro(path: str, request: Request) -> Response:
    """Catch-all — but only for paths on the allowlist. Metro's dev server
    exposes debug/introspection endpoints beyond bundle serving; anything
    not explicitly needed 404s here rather than being forwarded."""
    url_path = f"/{path}" if not path.startswith("/") else path
    if not _path_allowed(url_path):
        raise HTTPException(status_code=404, detail="Not found")
    _require_auth_http(request)

    # See _relay's matching comment: supervision only otherwise runs at a
    # turn's start, one step ahead of that same turn's own `npm install`
    # (via run_tool) — so it never sees a freshly-installed project until an
    # unrelated later turn happens to run. Idempotent; safe on every request.
    try:
        await ensure_metro_running()
    except Exception:
        logger.warning("preview_http_metro_supervision_failed", path=url_path, exc_info=True)

    # See wait_for_metro_ready's docstring: ensure_metro_running() only
    # guarantees the process was spawned, not that it's listening yet.
    # Verified live — the very first preview request after a cold bootstrap
    # raced this and hit an unhandled httpx.ConnectError (raw 500) in the
    # same second metro_started logged. Wait for the real socket and fail
    # with a clear, retryable status instead.
    if not await wait_for_metro_ready():
        # Blank for the DOCUMENT request, JSON for everything else.
        #
        # This route is what the preview <iframe> navigates to, so whatever the
        # top-level request returns is painted inside the device screen. An
        # HTTPException renders as `{"detail":"Metro is still starting..."}`,
        # which showed through DevicePreviewNode's own empty-state overlay —
        # that overlay is `bg-background/80`, i.e. 20% transparent — so the user
        # saw "No preview available" with raw JSON bleeding through behind it.
        #
        # The overlay owns what the user is told. The iframe underneath must
        # render nothing. Subresource requests (bundles, assets) are not painted
        # anywhere, so they keep the machine-readable body, which is what a
        # fetch error surfaces in the console.
        if url_path == "/":
            return Response(
                content=(
                    '<!doctype html><meta charset="utf-8">'
                    "<title>Starting…</title>"
                    "<style>html,body{margin:0;height:100%;background:transparent}</style>"
                ),
                status_code=503,
                media_type="text/html",
            )
        raise HTTPException(
            status_code=503,
            detail="Metro is still starting for this workspace — retry shortly.",
        )

    import httpx

    query = request.url.query
    target = f"{METRO_URL.replace('ws://', 'http://').replace('wss://', 'https://')}{url_path}"
    if query:
        target = f"{target}?{query}"

    async with httpx.AsyncClient() as client:
        upstream = await client.request(
            request.method,
            target,
            headers={
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length")
            },
            content=await request.body(),
            timeout=30.0,
        )

    content = upstream.content
    content_type = upstream.headers.get("content-type", "")
    if url_path == "/" and "html" in content_type.lower():
        content = _inject_websocket_appid_patch(content, get_active_app_id())

    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
