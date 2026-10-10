"""The session's computer: declared, supervised and proxied (waypoint ADR-050 §1-2)."""

import json
import socket
import sys

import httpx
from fastapi import FastAPI

from api.routers import computer
from core.computer.app import ComputerApp, ComputerSupervisor, computer_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _image(tmp_path, manifest):
    """An image's /opt/computer, with this manifest (a str is written as is)."""
    (tmp_path / "computer.json").write_text(
        manifest if isinstance(manifest, str) else json.dumps(manifest)
    )
    return str(tmp_path)


MANIFEST = {"start": "node '/opt/computer/server.mjs' --quiet", "port": 4100, "ready": "healthz"}


def test_no_computer_unless_the_definition_turned_it_on(tmp_path):
    image = {"HARNESS_COMPUTER_DIR": _image(tmp_path, MANIFEST)}
    assert computer_app(image) is None
    assert computer_app({**image, "HARNESS_COMPUTER": "0"}) is None


def test_no_computer_when_the_image_carries_no_manifest(tmp_path):
    assert computer_app({"HARNESS_COMPUTER": "1", "HARNESS_COMPUTER_DIR": str(tmp_path)}) is None
    for broken in ("{not json", {"port": 4100}, {"start": "node s.js", "port": "4100"}):
        d = tmp_path / str(abs(hash(str(broken))))
        d.mkdir()
        assert (
            computer_app({"HARNESS_COMPUTER": "1", "HARNESS_COMPUTER_DIR": _image(d, broken)})
            is None
        )


def test_the_image_manifest_is_read(tmp_path):
    app = computer_app(
        {"HARNESS_COMPUTER": "1", "HARNESS_COMPUTER_DIR": _image(tmp_path, MANIFEST)}
    )
    assert app == ComputerApp(
        command=["node", "/opt/computer/server.mjs", "--quiet"],
        port=4100,
        ready_path="/healthz",
        cwd="/workspace",
    )


async def test_the_app_is_supervised_and_served_under_computer(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<p>scenes</p>")
    port = _free_port()
    sup = ComputerSupervisor()
    monkeypatch.setattr(computer, "supervisor", sup)
    sup.start(
        ComputerApp(
            command=[
                sys.executable,
                "-m",
                "http.server",
                str(port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(tmp_path),
            ],
            port=port,
            ready_path="/",
            cwd=str(tmp_path),
        )
    )
    app = FastAPI()
    app.include_router(computer.router)
    try:
        assert await sup.wait_ready(timeout_s=15)
        # An in-pod caller (loopback) needs no token, as for the preview routes.
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as client:
            page = await client.get("/computer/index.html")
            assert page.status_code == 200
            assert page.text == "<p>scenes</p>"
            root = await client.get("/computer")
            assert root.status_code == 307
            assert root.headers["location"] == "computer/"
    finally:
        await sup.stop()


async def test_an_outside_caller_needs_the_internal_token(monkeypatch):
    import hashlib

    monkeypatch.setenv("WAYPOINT_ENV", "production")
    monkeypatch.setenv("WAYPOINT_INTERNAL_TOKEN_SHA256", hashlib.sha256(b"sdk-token").hexdigest())
    monkeypatch.setattr(computer, "supervisor", ComputerSupervisor())
    app = FastAPI()
    app.include_router(computer.router)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as client:
        assert (await client.get("/computer/index.html")).status_code == 401
        # With the SDK's token it gets through (to a session with no computer here).
        res = await client.get(
            "/computer/index.html", headers={"x-waypoint-internal-token": "sdk-token"}
        )
        assert res.status_code == 404


async def test_a_session_without_a_computer_answers_404(monkeypatch):
    monkeypatch.setattr(computer, "supervisor", ComputerSupervisor())
    app = FastAPI()
    app.include_router(computer.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as client:
        assert (await client.get("/computer/")).status_code == 404
