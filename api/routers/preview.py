"""
Preview streaming — ADR-036 §7 (enterprise-hardening plan).

Four routes, all behind the same auth check (§2 of the hardening plan —
reuses ``WAYPOINT_INTERNAL_TOKEN``, the SDK's own ``/internal/*`` convention,
rather than inventing a new one):

- HTTP proxy to Metro's own server (``localhost:8081``) — allowlisted (§3),
  not a true catch-all, since Metro's dev server exposes more than bundle
  serving and was never designed to be reachable outside a developer's own
  machine.
- ``GET /routes`` — the route manifest, read from the compiled ``preview.json``. Ported
  near-verbatim from opencode's own
  ``opencode_event_bridge/preview_manifest.py`` (same scan logic, same
  runtime-report-takes-precedence behavior), adapted to this session's
  ``workspace_context``/``/workspace`` conventions instead of opencode's
  ``OPENCODE_DIRECTORY``/``APP_ID`` globals.
- ``/hot`` / ``/message`` WebSocket relay to Metro's own sockets — the
  actual HMR channel. Distinct from ``core/metro/watcher.py``'s outbound-
  only connection (that one exists purely to trigger checkpoints; this one
  is what the browser's preview actually uses).
"""

import json
import os
import re
from typing import Any, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from core.metro.config import METRO_URL
from core.metro.supervisor import ensure_metro_running, wait_for_metro_ready
from core.workspace_context import get_active_app_id

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["preview"])

ROOT_DIR = "/workspace"


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


# ── Route manifest (ported from opencode's preview_manifest.py) ───────────

_NAV_CALL_RE = re.compile(
    r"(?:router\.(?:push|navigate|replace)|(?:navigation\.)?(?:navigate|push|replace))\s*\(\s*['\"`]([^'\"`]+)['\"`]"
)
_LINK_HREF_RE = re.compile(r"(?:<Link|href=|Link\s+to=|to=)\s*['\"`]([^'\"`]+)['\"`]")
_ROUTER_NON_ROUTE_RE = re.compile(r"^(?:_|\+)")


def _load_compiled_route_manifest(app_id: Optional[str]) -> Optional[dict]:
    """Read the route manifest straight from ``/workspace/preview.json`` —
    the compiled, authoritative artifact ``compile_check_cli`` generates
    from the agent's own ``*.preview.md`` manifests, the same source
    ``App.tsx``'s ``ScreenRoute`` switch is written from.

    This is now the ONLY source. It replaced a filename scanner that could
    only *guess* a route id from a component filename — a screen named
    ``Screen1.tsx`` gives no way to know whether the manifest called the
    route ``screen1``, ``screen-1``, or anything else — and, later, a cache
    of what the app POSTed about itself on mount. See ``routes()`` for why
    that cache had to go rather than be repaired.

    Reading the compiled JSON sidesteps guessing entirely: a
    ``devicePreview`` node's own ``data.route`` **is** the exact path the
    app's ``ScreenRoute`` type derives its values from (both strip the
    leading ``/`` from the same string), so using it directly can't drift
    from what the app will actually match against — regardless of what
    convention the agent chose for its manifest ids.

    Returns ``None`` (not an empty manifest) when ``preview.json`` doesn't
    exist yet or fails to parse, so the caller knows to fall back rather
    than treating "no compiled preview yet" the same as "compiled with
    zero screens".
    """
    path = os.path.join(ROOT_DIR, "preview.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            compiled = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    nodes = compiled.get("nodes")
    if not isinstance(nodes, list):
        return None

    routes: list[dict] = []
    node_kind_by_id: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_kind_by_id[node.get("id", "")] = node.get("type", "")
        if node.get("type") != "devicePreview":
            continue
        data = node.get("data") or {}
        route_path = data.get("route")
        if not isinstance(route_path, str) or not route_path.startswith("/"):
            continue
        routes.append(
            {
                "id": route_path.lstrip("/") or "home",
                "path": route_path,
                "label": data.get("label") or route_path,
                "isInitial": bool(data.get("isInitial")),
                "_node_id": node.get("id"),
            }
        )
    if not routes:
        return None

    route_id_by_node_id = {r["_node_id"]: r["id"] for r in routes}
    for r in routes:
        r.pop("_node_id", None)

    # A link is a devicePreview -> transition -> devicePreview chain (the
    # PTP rule from the topology reference). A transition can have MORE
    # THAN ONE devicePreview source — topology.md §6.A documents this as
    # the supported "fan-in" pattern (several screens converging on one
    # shared transition, e.g. three sign-in paths all reaching Home) — and
    # more than one devicePreview target (§6.B "fan-out", conditional
    # branching). Collect every source per transition, not just one, or a
    # generated app using fan-in loses all but the last-processed source
    # edge here even though the compiled graph has all of them.
    edges = compiled.get("edges")
    links: list[dict] = []
    if isinstance(edges, list):
        transition_sources: dict[str, list[tuple[str, Optional[str]]]] = {}
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source, target = edge.get("source"), edge.get("target")
            if (
                node_kind_by_id.get(source) == "devicePreview"
                and node_kind_by_id.get(target) == "transition"
            ):
                transition_sources.setdefault(target, []).append((source, edge.get("sourceHandle")))
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source, target = edge.get("source"), edge.get("target")
            if (
                node_kind_by_id.get(source) != "transition"
                or node_kind_by_id.get(target) != "devicePreview"
            ):
                continue
            to_rid = route_id_by_node_id.get(target)
            if not to_rid:
                continue
            for from_node_id, source_handle in transition_sources.get(source, []):
                from_rid = route_id_by_node_id.get(from_node_id)
                if not from_rid:
                    continue
                link: dict[str, Any] = {"fromRouteId": from_rid, "toRouteId": to_rid}
                if source_handle:
                    link["sourceHandle"] = source_handle
                links.append(link)

    return {"appId": app_id, "routes": routes, "links": links}


@router.get("/routes")
async def routes(request: Request) -> dict:
    """The app's route manifest, from the compiled artifact and nowhere else.

    ONE SOURCE, deliberately. This used to consult three, ordered worst-first:

      1. ``_RUNTIME_MANIFESTS`` — what the app POSTed about itself on mount,
         built by hand in ``previewBridge.ts``'s ``buildRouteManifest()``.
      2. ``preview.json`` — generated by ``compile_check_cli`` from the agent's
         own ``*.preview.md`` manifests.
      3. a scan that guessed route ids from screen FILENAMES.

    Each was added to fix the one below it, and none was removed when its
    replacement landed. The least reliable — hand-written, unverified, cached
    in memory with no invalidation — outranked the generated one.

    That produced an unrecoverable state, observed live: the placeholder app
    reported ``home`` and it was cached; the agent then replaced the screens and
    recompiled, so ``preview.json`` said ``first-screen``; this endpoint kept
    serving ``home``; the canvas passed ``?route=/home`` to the app; the app
    threw on an unmatched route DURING RENDER — before the ``useEffect`` that
    would have re-reported the real routes could run. The stale value caused the
    crash, and the crash preserved the stale value.

    ``preview.json`` cannot drift in that way: it derives each route id from the
    ``devicePreview`` node's own ``data.route``, which is the same string the
    app's ``ScreenRoute`` union is written from.

    An absent or unparseable manifest returns an EMPTY one. "This app has not
    compiled" is a real state and the canvas should show it, not a filename
    guess that happens to render something.
    """
    _require_auth_http(request)
    app_id = get_active_app_id()
    compiled = _load_compiled_route_manifest(app_id)
    if compiled is not None:
        return compiled
    logger.info("preview_routes_not_compiled", app_id=app_id)
    return {"appId": app_id, "routes": [], "links": []}


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
# order, and "/{path:path}" matches literally any path — declared any
# earlier in this file, it would shadow /routes above
# before their own handlers ever ran.


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
    """
    return (
        "<script>(function(){"
        f"var appId={json.dumps(app_id)};"
        "if(!appId)return;"
        "var Native=window.WebSocket;"
        "function Patched(url,protocols){"
        "try{"
        "var u=new URL(url,window.location.href);"
        "if((u.pathname==='/hot'||u.pathname==='/message')&&!u.searchParams.has('appId')){"
        "u.searchParams.set('appId',appId);url=u.toString();"
        "}"
        "}catch(e){}"
        "return Reflect.construct(Native,protocols===undefined?[url]:[url,protocols]);"
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
