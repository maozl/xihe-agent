"""serve ssh-live endpoints (gateway/serve/terminal.py): REST handlers as
plain functions (FakeRequest), the WS stream through aiohttp's TestClient.

The stream test pins the panel contract: meta first, cursor-addressed data
frames (backlog → live), input frames reaching the tap's sender, close
notification, and the no-such-session 404 (plain HTTP, not a WS upgrade).
"""
import asyncio
import json

import pytest

from gateway.serve import terminal as sr
from tools import _ssh_tap as tap_mod
from tools.ssh_tool import SSHSession, tool_result


class FakeRequest:
    def __init__(self, query=None, body=None, body_raises=False):
        self.query = query or {}
        self._body = body
        self._body_raises = body_raises

    async def json(self):
        if self._body_raises:
            raise ValueError("no json")
        return self._body


def _payload(resp):
    return json.loads(resp.text)


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    with tap_mod._taps_lock:
        for n in list(tap_mod._taps):
            t = tap_mod._taps.pop(n)
            t.close("test-end")


class _FakeChannel:
    """Idle channel: no bytes to recv (pump just polls), records resize_pty."""

    closed = False
    eof_received = False

    def __init__(self):
        self.resized = []

    def recv_ready(self):
        return False

    def recv(self, n):
        return b""

    def send(self, b):
        pass

    def resize_pty(self, width=0, height=0):
        self.resized.append((width, height))


def _tap(name="t1", prefill="", channel=None):
    sent = []
    # registry key = session_key|alias, like ssh_tool._skey produces
    key = f"agent:main:serve:dm:c1|{name}"
    tap = tap_mod.attach(key, channel=channel, meta={
        "host": "h", "user": "u", "mode": "shell", "origin": "agent",
        "alias": name, "session_key": "agent:main:serve:dm:c1",
        "cols": 100, "rows": 30,
    })
    tap.bind_sender(sent.append)
    if prefill:
        tap.publish(prefill)
    return tap, sent


# ---------------------------------------------------------------------------
# GET /ssh/live

def test_ssh_live_lists_sessions():
    _tap("live1", prefill="hello\n[user@h ~]$ ")
    p = _payload(asyncio.run(sr.ssh_live(FakeRequest())))
    row = next(r for r in p["sessions"] if r["name"] == "live1")
    assert row["mode"] == "shell" and row["origin"] == "agent"
    assert row["alive"] is True and row["closed"] is False


# ---------------------------------------------------------------------------
# POST /ssh/live/connect

def test_connect_requires_name_and_secret():
    r = asyncio.run(sr.ssh_live_connect(FakeRequest(body={"host": "h"})))
    assert r.status == 400
    r = asyncio.run(sr.ssh_live_connect(
        FakeRequest(body={"name": "n", "host": "h", "user": "u"})))
    assert r.status == 400


def test_connect_requires_int_port():
    r = asyncio.run(sr.ssh_live_connect(FakeRequest(
        body={"name": "n", "host": "h", "user": "u", "token": "x",
              "port": "abc"})))
    assert r.status == 400


def test_connect_routes_through_ssh_tool_with_desktop_origin(monkeypatch):
    from tools import ssh_tool
    captured = {}

    def fake_connect(args, **kw):
        captured.update(args=args, kw=kw)
        return tool_result(success=True, session=args["name"], mode="shell",
                           message="Connected")

    monkeypatch.setattr(ssh_tool, "_ssh_connect", fake_connect)
    body = {"name": "bastion", "host": "h", "user": "u", "token": "secret-tok",
            "session_key": "agent:main:serve:dm:c1"}
    resp = asyncio.run(sr.ssh_live_connect(FakeRequest(body=body)))
    p = _payload(resp)
    assert p["ok"] is True and p["session"] == "bastion"
    assert captured["kw"]["origin"] == "desktop"
    assert captured["kw"]["context"]["session_key"] == body["session_key"]
    assert captured["args"]["token"] == "secret-tok"
    assert "secret-tok" not in resp.text      # secret never echoes


def test_connect_maps_tool_error_to_ok_false(monkeypatch):
    from tools import ssh_tool
    monkeypatch.setattr(ssh_tool, "_ssh_connect",
                        lambda args, **kw: '{"error": "auth failed"}')
    p = _payload(asyncio.run(sr.ssh_live_connect(FakeRequest(
        body={"name": "n", "host": "h", "user": "u", "token": "x"}))))
    assert p["ok"] is False and p["error"] == "auth failed"


# ---------------------------------------------------------------------------
# WS /ssh/live/stream

async def _recv_json(ws, timeout=3.0):
    msg = await asyncio.wait_for(ws.receive_json(), timeout)
    assert msg["t"], msg
    return msg


async def _collect_until(ws, want_t, timeout=3.0):
    """Receive frames until one of type ``want_t`` arrives; returns it."""
    while True:
        msg = await _recv_json(ws, timeout)
        if msg["t"] == want_t:
            return msg


def test_stream_contract():
    async def drive():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        sr.add_routes(app.router)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            tap, sent = _tap("ws1", prefill="banner\r\n[user@h ~]$ ")
            ws = await client.ws_connect("/ssh/live/stream?key=agent:main:serve:dm:c1|ws1")

            meta = await _recv_json(ws)
            assert meta["t"] == "meta" and meta["name"] == "ws1"
            assert meta["cols"] == 100 and meta["rows"] == 30
            assert meta["offset"] == 0

            data = await _collect_until(ws, "d")
            assert "banner" in data["s"]
            assert data["o"] == len("banner\r\n[user@h ~]$ ")

            await ws.send_json({"t": "input", "s": "whoami\n"})
            ev = await _collect_until(ws, "i")
            assert ev["who"] == "user" and ev["s"] == "whoami\n"
            assert sent == ["whoami\n"]

            tap.publish("root\r\n[user@h ~]$ ")
            more = await _collect_until(ws, "d")
            assert "root" in more["s"]

            tap.close("test-done")
            end = await _collect_until(ws, "x")
            assert end["reason"] == "test-done"
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_stream_resumes_from_cursor():
    async def drive():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        sr.add_routes(app.router)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            full = "line1\nline2\n[user@h ~]$ "
            tap, _ = _tap("ws2", prefill=full)
            ws = await client.ws_connect(
                f"/ssh/live/stream?key=agent:main:serve:dm:c1|ws2&from={len('line1\n')}")
            await _recv_json(ws)                       # meta
            data = await _collect_until(ws, "d")
            assert data["s"] == full[len("line1\n"):]
            assert data["o"] == len(full)
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_stream_resize_frame_reaches_channel_and_echoes():
    async def drive():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        sr.add_routes(app.router)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ch = _FakeChannel()
            _tap("ws3", channel=ch)
            ws = await client.ws_connect("/ssh/live/stream?key=agent:main:serve:dm:c1|ws3")
            await _recv_json(ws)                       # meta
            await ws.send_json({"t": "resize", "cols": 160, "rows": 44})
            # the resize fan-out echoes back to this same viewer
            ev = await _collect_until(ws, "resize")
            assert ev["cols"] == 160 and ev["rows"] == 44
            assert ch.resized == [(160, 44)]
            rows = await client.get("/ssh/live")
            live = (await rows.json())["sessions"]
            row = next(r for r in live if r["name"] == "ws3")
            assert row["cols"] == 160 and row["rows"] == 44
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_stream_resize_rejected_on_passive_tap():
    async def drive():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        sr.add_routes(app.router)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            _tap("ws4")                               # passive: no channel
            ws = await client.ws_connect("/ssh/live/stream?key=agent:main:serve:dm:c1|ws4")
            await _recv_json(ws)                       # meta
            await ws.send_json({"t": "resize", "cols": 160, "rows": 44})
            ev = await _collect_until(ws, "err")
            assert ev["reason"] == "not-shell"
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_stream_unknown_session_is_plain_404():
    async def drive():
        from aiohttp import web
        from aiohttp.client_exceptions import WSServerHandshakeError
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        sr.add_routes(app.router)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with pytest.raises(WSServerHandshakeError) as e:
                await client.ws_connect("/ssh/live/stream?key=nope")
            assert e.value.status == 404
        finally:
            await client.close()

    asyncio.run(drive())


def test_stream_requires_session_param():
    async def drive():
        from aiohttp import web
        from aiohttp.client_exceptions import WSServerHandshakeError
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        sr.add_routes(app.router)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with pytest.raises(WSServerHandshakeError) as e:
                await client.ws_connect("/ssh/live/stream")
            assert e.value.status == 400
        finally:
            await client.close()

    asyncio.run(drive())


def test_add_routes_registers_ssh_live():
    from aiohttp import web
    app = web.Application()
    sr.add_routes(app.router)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert paths >= {"/ssh/live", "/ssh/live/stream", "/ssh/live/connect"}
