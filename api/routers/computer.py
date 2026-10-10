"""
The session's computer, proxied (waypoint ADR-050 §2).

``/computer/<path>`` on this port, HTTP or WebSocket, is the computer app's
``/<path>``. A prefix of its own means no app path collides with the harness's
routes, and the app builder's Metro proxy (preview.py) is untouched.

The app is served under prefixes it does not know -- the consumer's, then this
one -- so it addresses its own resources relatively. ``/computer`` without the
slash redirects to ``computer/`` so that relative addresses resolve below it.

Same auth as the preview routes: the SDK's internal token, or a caller inside
the pod.
"""

import asyncio
from typing import Optional

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from api.internal_auth import is_in_pod_caller, is_internal_caller
from core.computer import supervisor

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["computer"])

# Not forwarded either way: they describe one hop, not the message.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _authorized(client_host: Optional[str], token: Optional[str]) -> bool:
    return is_in_pod_caller(client_host) or is_internal_caller(token)


@router.api_route("/computer", methods=_METHODS, include_in_schema=False)
async def computer_root() -> Response:
    # Relative, so it holds under any prefix in front of this one.
    return RedirectResponse(url="computer/", status_code=307)


@router.api_route("/computer/{path:path}", methods=_METHODS, include_in_schema=False)
async def proxy_http(path: str, request: Request) -> Response:
    if not _authorized(
        request.client.host if request.client else None,
        request.headers.get("x-waypoint-internal-token"),
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid x-waypoint-internal-token")
    app = supervisor.app
    if app is None:
        raise HTTPException(status_code=404, detail="This session has no computer")
    if not await supervisor.wait_ready():
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return Response(
                content='<!doctype html><meta charset="utf-8"><title>Starting…</title>'
                '<meta http-equiv="refresh" content="2">'
                "<style>html,body{margin:0;height:100%;background:transparent}</style>",
                status_code=503,
                media_type="text/html",
            )
        raise HTTPException(
            status_code=503, detail="The computer is still starting — retry shortly."
        )

    target = f"{app.base_url}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "x-waypoint-internal-token"
    }

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    upstream_req = client.build_request(
        request.method, target, headers=headers, content=await request.body()
    )
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        logger.warning("computer_proxy_upstream_failed", path=path, error=str(e))
        raise HTTPException(status_code=502, detail="The computer did not answer") from e

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP},
    )


@router.websocket("/computer/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str) -> None:
    import websockets

    if not _authorized(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("x-waypoint-internal-token"),
    ):
        await websocket.close(code=4401)
        return
    app = supervisor.app
    if app is None:
        await websocket.close(code=4404)
        return
    if not await supervisor.wait_ready():
        await websocket.close(code=1013, reason="The computer is still starting — retry shortly")
        return
    target = f"{app.ws_base_url}/{path}"
    if websocket.url.query:
        target = f"{target}?{websocket.url.query}"
    subprotocols = websocket.scope.get("subprotocols") or None
    try:
        async with websockets.connect(target, subprotocols=subprotocols) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def client_to_upstream() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                except WebSocketDisconnect:
                    return

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

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
        logger.warning("computer_ws_relay_failed", path=path, exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
