"""The terminal business: the desktop terminal panel over the agent's ssh
sessions — stateless module functions, no ServeApp state.

Sessions live in the SERVE process (ssh_tool's registry, tapped by _ssh_tap);
the panel is just a viewer + second keyboard on the same channels.

POST /ssh/live/connect routes through the same _ssh_connect the agent uses,
so the session lands in the shared registry and the agent can drive it
afterwards. The token/password stays in this process's memory: it is passed
to _ssh_connect and never echoed back or logged."""

import asyncio
import json
import logging

from aiohttp import web

from gateway.serve._common import err, run

logger = logging.getLogger(__name__)

# Backlog chars sent on a first attach (no `from` param): enough scrollback
# to see what the agent has been doing, not the whole ring.
_ATTACH_BACKLOG = 8_192
# Max chars per WS frame — the ring read for a burst can return ~1MB; one
# frame of that stalls the renderer's JSON parse.
_FRAME_CHARS = 8_192
# Live tick: matches the pump's poll rate; consecutive output within a tick
# coalesces into one frame (same philosophy as chat's Emitter).
_STREAM_TICK = 0.05


async def ssh_live(request):
    """Session list for the terminal panel's tabs (tap registry only —
    never takes ssh_tool._ssh_lock)."""
    from tools import _ssh_tap

    return web.json_response({"sessions": await run(_ssh_tap.live_snapshot)})


async def ssh_live_connect(request):
    from tools import ssh_tool

    try:
        body = await request.json()
    except Exception:
        return err(400, "invalid json")
    if not isinstance(body, dict):
        return err(400, "body must be a mapping")

    try:
        port = int(body.get("port") or 22)
        timeout = int(body.get("timeout") or 15)
    except (TypeError, ValueError):
        return err(400, "port/timeout must be integers")

    args = {
        "name": str(body.get("name") or "").strip(),
        "host": str(body.get("host") or "").strip(),
        "port": port,
        "user": str(body.get("user") or "").strip(),
        "mode": str(body.get("mode") or "shell"),
        "timeout": timeout,
    }
    secret = str(body.get("token") or body.get("password") or "")
    if secret:
        args["token"] = secret
    if not args["name"]:
        return err(400, "name is required")
    if not secret:
        # clarify is an agent-turn interaction; over REST there is nobody to
        # ask — demand the token upfront
        return err(400, "token/password is required")

    ctx = {"session_key": str(body.get("session_key") or "")}
    raw = await run(lambda: ssh_tool._ssh_connect(args, context=ctx,
                                                  origin="desktop"))
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        logger.warning("ssh live connect: unparseable result for %s", args["name"])
        return err(500, "connect returned an unexpected result")
    payload["ok"] = bool(payload.get("success"))
    return web.json_response(payload)


async def _ssh_stream_sender(ws, tap, q, cursor: int):
    """Writer half of /ssh/live/stream: backlog from ``cursor``, then live
    data by polling the ring at the sender's own cursor. The subscriber
    queue contributes only input-attribution and close events — its "d"
    events are discarded, because those same chars are also in the ring and
    cursor addressing makes the ring authoritative (no double-send window
    between backlog read and subscribe)."""
    loop = asyncio.get_running_loop()
    try:
        await ws.send_json({
            "t": "meta", "key": tap.name, "name": tap.meta.get("alias", tap.name),
            "host": tap.meta.get("host", ""), "user": tap.meta.get("user", ""),
            "mode": tap.meta.get("mode", ""), "origin": tap.meta.get("origin", ""),
            "session_key": tap.meta.get("session_key", ""),
            "cols": tap.meta.get("cols"), "rows": tap.meta.get("rows"),
            "offset": cursor, "base": tap.ring.base,
        })
        while True:
            text, cursor = tap.read_from(cursor)
            while text:
                piece, text = text[:_FRAME_CHARS], text[_FRAME_CHARS:]
                cursor_off = cursor - len(text)
                await ws.send_json({"t": "d", "o": cursor_off, "s": piece})
            # attribution/close events; data events are dropped unread
            while True:
                try:
                    ev = q.get_nowait()
                except Exception:
                    break
                if ev[0] == "i":
                    await ws.send_json({"t": "i", "who": ev[1], "s": ev[2]})
                elif ev[0] == "r":
                    await ws.send_json({"t": "resize", "cols": ev[1], "rows": ev[2]})
                elif ev[0] == "x":
                    await ws.send_json({"t": "x", "reason": ev[1]})
                    return
            if tap.closed:
                await ws.send_json({"t": "x", "reason": tap.close_reason or "closed"})
                return
            if not tap.alive():
                await ws.send_json({"t": "x", "reason": "dead"})
                return
            await asyncio.sleep(_STREAM_TICK)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # client gone — the reader half notices via its own receive() error
        logger.debug("ssh live stream sender ended: %s", e)


async def ssh_live_stream(request):
    """WS viewer + input path for one tapped session.

    GET /ssh/live/stream?key=<session_key|alias>&from=<offset>
      server → client  {t:"meta"} then {t:"d",o,s} data frames, {t:"i"} input
                      attribution, {t:"resize",cols,rows} pty resize, {t:"x"} on
                      close, {t:"err"} on rejected input
      client → server {t:"input", s:"..."} → channel.send via the tap
                      {t:"resize", cols, rows} → channel.resize_pty

    ``from`` omitted → attach with the last _ATTACH_BACKLOG chars; explicit →
    resume at that offset (client reconnect cursor). Data always carries its
    ending offset, so the client's cursor follows the server's, never the
    other way round."""
    from tools import _ssh_tap

    key = request.query.get("key", "").strip()
    if not key:
        return err(400, "key is required")
    raw_from = request.query.get("from")
    try:
        frm = int(raw_from) if raw_from is not None else None
    except ValueError:
        return err(400, "from must be an integer offset")

    got = await run(lambda: _ssh_tap.subscribe(key))
    if got is None:
        return err(404, f"no such session: {key}")
    tap, q = got

    if frm is None:
        cursor = max(tap.ring.base, tap.ring.w - _ATTACH_BACKLOG)
    else:
        # clamp both ways: below base the ring no longer holds it, above w
        # means the ring restarted (serve restart) — live from now
        cursor = max(tap.ring.base, min(frm, tap.ring.w))

    ws = web.WebSocketResponse(heartbeat=60)
    await ws.prepare(request)
    sender = asyncio.create_task(_ssh_stream_sender(ws, tap, q, cursor))
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                if msg.type == web.WSMsgType.ERROR:
                    break
                continue
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("t") == "input":
                text = str(frame.get("s") or "")
                reason = await run(lambda: _ssh_tap.send_user_input(key, text))
                if reason:
                    await ws.send_json({"t": "err", "reason": reason})
            elif frame.get("t") == "resize":
                cols, rows = frame.get("cols"), frame.get("rows")
                reason = await run(
                    lambda: _ssh_tap.resize(key, cols, rows))
                if reason:
                    await ws.send_json({"t": "err", "reason": reason})
    finally:
        sender.cancel()
        _ssh_tap.unsubscribe(tap, q)
    return ws


async def local_live(request):
    """Channel list for the terminal panel's agent tabs: per-conversation
    `terminal` consoles plus one channel per resident `process` run."""
    from tools import _local_tap

    return web.json_response({"channels": await run(_local_tap.list_channels)})


async def _local_stream_sender(ws, ch, cursor: int):
    """Writer half of /local/live/stream: backlog from ``cursor``, then live
    data by polling the channel ring. Read-only — the owning tool (terminal /
    process) controls the process; there is no input path (unlike ssh)."""
    snap = await run(ch.snapshot)
    snap.update({"t": "meta", "offset": cursor, "base": ch.ring.base})
    await ws.send_json(snap)
    while True:
        text, cursor = await run(lambda: ch.read_from(cursor))
        while text:
            piece, text = text[:_FRAME_CHARS], text[_FRAME_CHARS:]
            cursor_off = cursor - len(text)
            await ws.send_json({"t": "d", "o": cursor_off, "s": piece})
        await asyncio.sleep(_STREAM_TICK)


async def local_live_stream(request):
    """WS viewer for one agent local channel.

    GET /local/live/stream?key=<channel>&from=<offset>
      server → client  {t:"meta"} then {t:"d",o,s} frames; data carries its
                      ending offset so a reconnect resumes at the cursor.
      client → server  nothing (read-only stream)

    ``from`` omitted → attach with the last _ATTACH_BACKLOG chars; explicit →
    resume at that offset (clamped to the ring, same as ssh streams)."""
    from tools import _local_tap

    key = request.query.get("key", "").strip()
    if not key:
        return err(400, "key is required")

    def _resolve():
        ch = _local_tap.get(key)
        if ch is None and key.startswith("conv:"):
            # A viewer may open a conversation's console before any terminal
            # run created it — create it empty and wait.
            ch = _local_tap.conv_channel(key[len("conv:"):])
        return ch

    ch = await run(_resolve)
    if ch is None:
        return err(404, f"no such channel: {key}")

    raw_from = request.query.get("from")
    try:
        frm = int(raw_from) if raw_from is not None else None
    except ValueError:
        return err(400, "from must be an integer offset")

    if frm is None:
        cursor = max(ch.ring.base, ch.ring.w - _ATTACH_BACKLOG)
    else:
        cursor = max(ch.ring.base, min(frm, ch.ring.w))

    ws = web.WebSocketResponse(heartbeat=60)
    await ws.prepare(request)
    sender = asyncio.create_task(_local_stream_sender(ws, ch, cursor))
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.ERROR:
                break
    finally:
        sender.cancel()
    return ws


async def local_live_stop(request):
    """Kill a channel's live resident process (panel stop button)."""
    from tools import _local_tap

    try:
        body = await request.json()
    except Exception:
        body = {}
    key = str((body or {}).get("key") or "")
    if not key:
        return err(400, "key is required")
    result = await run(lambda: _local_tap.stop_channel(key))
    if not result.get("ok"):
        return err(404, result.get("error", "channel not found"))
    return web.json_response(result)


def add_routes(router):
    router.add_get("/ssh/live", ssh_live)
    router.add_get("/ssh/live/stream", ssh_live_stream)
    router.add_post("/ssh/live/connect", ssh_live_connect)
    router.add_get("/local/live", local_live)
    router.add_get("/local/live/stream", local_live_stream)
    router.add_post("/local/live/stop", local_live_stop)
