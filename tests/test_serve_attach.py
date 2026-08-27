"""serve's `attach` contract: the reconnect-resync handshake.

A reconnecting desktop sends `attach` to resume a detached turn's stream.
The ack (`attached{running}`) is what lets the desktop distinguish the two
outcomes: running=true → keep the local pending bubble (deltas resume onto
it); running=false → the turn settled while we were away, no complete is
coming, so refetch to replace the stale bubble. Without the ack the desktop
had to refetch unconditionally, which wiped live bubbles — the missing
正在思考… indicator bug.
"""
import asyncio

from gateway.serve import ServeApp

from tests.test_serve_first_run import FakeCtx, FakeWs


def _make_app():
    return ServeApp(FakeCtx({"api_key": "sk-x", "model": "m"}), version="test")


def _ack(ws):
    return [e for e in ws.sent if e.get("type") == "attached"]


def test_attach_idle_conv_acks_not_running():
    app = _make_app()
    ws = FakeWs()
    adopted = asyncio.run(app._handle_attach(ws, {"conv_id": "c1", "type": "attach"}))
    assert adopted is False
    assert app._conv_sockets.get("c1") is None
    assert _ack(ws) == [{"type": "attached", "conv_id": "c1", "running": False}]


def test_attach_running_conv_adopts_socket_and_acks_running():
    app = _make_app()
    app._active["c1"] = object()
    ws = FakeWs()
    adopted = asyncio.run(app._handle_attach(ws, {"conv_id": "c1", "type": "attach"}))
    assert adopted is True
    assert app._conv_sockets["c1"] is ws
    assert _ack(ws) == [{"type": "attached", "conv_id": "c1", "running": True}]


def test_attach_empty_conv_id_is_silent_noop():
    app = _make_app()
    ws = FakeWs()
    adopted = asyncio.run(app._handle_attach(ws, {"conv_id": "", "type": "attach"}))
    assert adopted is False
    assert ws.sent == []
