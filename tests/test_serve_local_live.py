"""serve local-live endpoints (gateway/serve/terminal.py): the agent channel
registry's list + read-only per-channel WS stream, through aiohttp's
TestClient.

Pins the panel contract: channels envelope, meta first, cursor-addressed data
frames (backlog → live), resume-from-cursor, unknown-key 404. The registry is
a module singleton — tests use fresh session keys instead of assuming empty.
"""
import asyncio
import json

from gateway.serve import terminal as sr
from tools import _local_tap


async def _recv_json(ws, timeout=3.0):
    msg = await asyncio.wait_for(ws.receive_json(), timeout)
    assert msg["t"], msg
    return msg


async def _collect_until(ws, want_t, timeout=3.0):
    while True:
        msg = await _recv_json(ws, timeout)
        if msg["t"] == want_t:
            return msg


def _client():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    sr.add_routes(app.router)
    return TestClient(TestServer(app))


def test_local_live_lists_channels():
    ch = _local_tap.conv_channel("t-live-a")
    ch.begin("pytest-demo", None)
    ch.publish("chunk")
    ch.end(0, 0.1)
    p = json.loads(asyncio.run(sr.local_live(None)).text)["channels"]
    row = next(r for r in p if r["key"] == "conv:t-live-a")
    assert row["running"] is False
    assert row["command"] == "pytest-demo"
    assert row["buffered"] > 0


def test_stream_contract_backlog_then_live():
    async def drive():
        client = _client()
        await client.start_server()
        try:
            ch = _local_tap.conv_channel("t-live-b")
            ch.publish("backlog-data\r\n")
            ws = await client.ws_connect("/local/live/stream?key=conv:t-live-b")
            meta = await _recv_json(ws)
            assert meta["t"] == "meta" and meta["running"] is False
            assert meta["kind"] == "conv"

            data = await _collect_until(ws, "d")
            assert "backlog-data" in data["s"]
            assert data["o"] == ch.ring.w

            ch.publish("live-data\r\n")
            more = await _collect_until(ws, "d")
            assert "live-data" in more["s"]
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_stream_resumes_from_cursor():
    async def drive():
        client = _client()
        await client.start_server()
        try:
            ch = _local_tap.conv_channel("t-live-c")
            ch.publish("line1\nline2\n")
            frm = ch.ring.w - len("line2\n")
            ws = await client.ws_connect(
                f"/local/live/stream?key=conv:t-live-c&from={frm}")
            await _recv_json(ws)                       # meta
            data = await _collect_until(ws, "d")
            assert data["s"] == "line2\n"
            assert data["o"] == ch.ring.w
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_unknown_proc_key_is_404():
    async def drive():
        client = _client()
        await client.start_server()
        try:
            # a plain GET (no upgrade) still runs the resolver before
            # ws.prepare — the 404 escapes as a normal response
            r = await client.get("/local/live/stream?key=proc:nope:zzz")
            assert r.status == 404
            # a conv key may not exist yet — the viewer creates it empty
            ws = await client.ws_connect("/local/live/stream?key=conv:t-live-d")
            meta = await _recv_json(ws)
            assert meta["t"] == "meta"
            await ws.close()
        finally:
            await client.close()

    asyncio.run(drive())


def test_add_routes_registers_local_live():
    from aiohttp import web

    app = web.Application()
    sr.add_routes(app.router)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert paths >= {"/local/live", "/local/live/stream", "/local/live/stop"}
