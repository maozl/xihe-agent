"""Shared-terminal tap for ssh_tool sessions — pump + ring + subscribers.

Every shell-mode channel gets one pump thread that is the SOLE owner of
``channel.recv()``; all consumers (the agent's ssh_exec reads, the desktop
panel's WS viewers) read from that session's ring at their own cursor.
This is what lets the desktop user type into the same channel: output can
arrive with no agent turn running, which an on-demand reader would drop or
mis-attribute. Exec-mode sessions get a passive tap (no pump — the channel
is consumed synchronously inside _exec_mode, which publishes chunks itself).

Locking rule: nothing here may take ssh_tool._ssh_lock. ssh_exec holds it
for a whole command (up to 120s) while waiting on this ring — the pump and
the desktop input path must stay live during that. The tap registry and
each ring carry their own locks.
"""

import codecs
import logging
import queue
import re
import threading
import time

logger = logging.getLogger(__name__)

RING_MAX_CHARS = 1_000_000
INPUT_LOG_MAX = 200
PUMP_POLL_SEC = 0.05
PUMP_JOIN_SEC = 2.0
MAX_INPUT_FRAME = 16_000

# Legacy stop heuristic, preserved verbatim from the old channel-based
# _read_until_prompt — the ssh-access skill's stateful chains are tuned to it.
PROMPT_STOP_RE = re.compile(r"\][\$#]")
PROMPT_LINE_RE = re.compile(r"^\[.*\][\$#] ?.*$", re.M)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def last_prompt_line(raw: str) -> str | None:
    """Final ``[user@host ~]$``-style line, ANSI-stripped. Returned by
    ssh_exec as the chain-position signal — _clean_shell_output strips these
    lines from the agent-facing output, so without this the model cannot see
    where the stateful chain currently sits (or that the desktop user moved
    it)."""
    best = None
    for m in PROMPT_LINE_RE.finditer(strip_ansi(raw)):
        best = m.group(0).strip()
    return best


class Ring:
    """Bounded str buffer addressed by a monotonic char offset. Reads walk a
    chunk list (no per-append 1MB copy); append coalesces when fragmentation
    grows past the guard."""

    _COALESCE_AT = 4_096  # chunks

    __slots__ = ("_chunks", "_total", "base", "w", "_max")

    def __init__(self, max_chars: int = RING_MAX_CHARS):
        self._chunks: list[str] = []
        self._total = 0
        self.base = 0    # stream offset of _chunks[0]
        self.w = 0       # write offset (total chars ever appended)
        self._max = max_chars

    def append(self, s: str) -> None:
        if not s:
            return
        self._chunks.append(s)
        self._total += len(s)
        self.w += len(s)
        over = self._total - self._max
        if over >= self._total:
            # single append larger than the ring — keep its tail
            self._chunks = [s[-self._max:]] if self._max > 0 else []
            self._total = len(self._chunks[0]) if self._chunks else 0
            self.base = self.w - self._total
            return
        if over > 0:
            i = 0
            while i < len(self._chunks) and over >= len(self._chunks[i]):
                over -= len(self._chunks[i])
                i += 1
            kept = self._chunks[i:]
            if over:
                kept = [kept[0][over:]] + kept[1:]
            self._chunks = kept
            self._total = self._max
            self.base = self.w - self._total
        elif len(self._chunks) > self._COALESCE_AT:
            joined = "".join(self._chunks)
            self._chunks = [joined]

    def read(self, start: int) -> tuple[str, int]:
        """(text, end_offset) for everything retained from ``start`` (clamped
        to ``base``) up to ``w``."""
        if start >= self.w:
            return "", self.w
        if start < self.base:
            start = self.base
        out: list[str] = []
        pos = self.base
        for c in self._chunks:
            end = pos + len(c)
            if end > start:
                out.append(c if pos >= start else c[start - pos:])
            pos = end
        return "".join(out), self.w


class TapClosedError(RuntimeError):
    pass


class SessionTap:
    """One watched ssh session: ring + subscriber fan-out + input attribution.

    ``agent_cursor`` persists across ssh_exec calls, so each command reads
    only bytes produced since the previous one — desktop-typed commands land
    in the next agent read (echoed by the pty) instead of being swallowed."""

    def __init__(self, name: str, meta: dict | None = None, session=None):
        self.name = name
        self.meta = dict(meta or {})
        self.session = session          # ssh_tool.SSHSession (liveness probe)
        self.ring = Ring()
        self.lock = threading.Lock()
        self.data_avail = threading.Condition(self.lock)
        self.subs: dict[int, queue.Queue] = {}
        self.inputs: list[dict] = []    # {offset, who, text, ts}
        self.closed = False
        self.close_reason: str | None = None
        self.agent_cursor = 0
        self.pump: threading.Thread | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._send = None               # callable(str) — channel.send; None = passive
        self._channel = None            # pumped shell channel (liveness probe)

    # -- producer -------------------------------------------------------------

    def feed_bytes(self, data: bytes) -> str:
        """Decode a recv chunk and publish it. One incremental decoder per
        tap, so a multibyte char split across two recv chunks survives (the
        legacy per-chunk decode dropped it)."""
        s = self._decoder.decode(data)
        self.publish(s)
        return s

    def publish(self, s: str) -> None:
        """Emit an already-decoded chunk (exec taps publish read results)."""
        if not s:
            return
        with self.lock:
            self.ring.append(s)
            self._fanout(("d", s))
            self.data_avail.notify_all()

    def _fanout(self, ev) -> None:
        """Caller holds self.lock. A full queue means a stuck viewer — drop
        it (the panel reconnects) rather than block the pump."""
        dead = []
        for qid, q in list(self.subs.items()):
            try:
                q.put_nowait(ev)
            except queue.Full:
                dead.append(qid)
        for qid in dead:
            self.subs.pop(qid, None)
            logger.warning("ssh tap '%s': slow viewer dropped", self.name)

    # -- input ----------------------------------------------------------------

    def bind_sender(self, send) -> None:
        """Attach the channel's send (pumped shell taps). Must be called
        before the pump starts so no input can race an unbound tap."""
        self._send = send

    def send_input(self, text: str, who: str) -> int:
        """Write to the channel and log attribution. Raises TapClosedError on
        a dead session so callers (agent _shell_mode, desktop WS) surface it.
        The pty echoes the text into the stream — the ring stays output-only,
        attribution lives in ``inputs``."""
        with self.lock:
            if self.closed:
                raise TapClosedError(f"session '{self.name}' is closed")
            entry = {"offset": self.ring.w, "who": who, "text": text, "ts": time.time()}
            self.inputs.append(entry)
            if len(self.inputs) > INPUT_LOG_MAX:
                del self.inputs[: len(self.inputs) // 2]
            self._fanout(("i", who, text))
        if self._send is not None:
            self._send(text)            # channel.send — outside the tap lock
        return entry["offset"]

    def user_input_since(self, offset: int) -> bool:
        with self.lock:
            return any(e["who"] == "user" and e["offset"] >= offset for e in self.inputs)

    def last_input(self) -> dict | None:
        with self.lock:
            return dict(self.inputs[-1]) if self.inputs else None

    # -- consumers ------------------------------------------------------------

    def read_from(self, cursor: int) -> tuple[str, int]:
        with self.lock:
            return self.ring.read(cursor)

    def wait(self, timeout: float) -> None:
        with self.data_avail:
            self.data_avail.wait(timeout)

    def close(self, reason: str) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.close_reason = reason
            self._fanout(("x", reason))
            self.data_avail.notify_all()

    def alive(self) -> bool:
        if self.closed:
            return False
        if self._send is not None:      # pumped shell tap: channel liveness
            try:
                ch = getattr(self, "_channel", None)
                return ch is None or not getattr(ch, "closed", False)
            except Exception:
                return False
        if self.session is not None:    # passive exec tap: transport liveness
            try:
                from tools.ssh_tool import _is_alive
                return _is_alive(self.session)
            except Exception:
                return False
        return True


# -- agent-side read loop (ring equivalent of the legacy _read_until_prompt) --

def read_until_prompt(tap: SessionTap, cursor: int, timeout: float = 10,
                      check_interrupt: bool = True, idle_timeout: float = 0):
    """Same contract as the old channel-based loop: stop at the first chunk
    matching the legacy prompt regex, drain 0.2s more, mark interrupts.
    Returns (raw, new_cursor, interrupted).

    ``idle_timeout`` > 0 enables progress-aware waiting: new output resets the
    idle clock, and only ``timeout`` (a total ceiling) bounds the wait. A
    healthy long job keeps producing output and the read follows it through;
    a silent one returns at the idle mark instead of blocking the caller for
    the whole ceiling."""
    output = ""
    start = time.time()
    last_data = start
    while True:
        now = time.time()
        if now - start >= timeout:
            break
        if idle_timeout > 0 and now - last_data >= idle_timeout:
            break
        if check_interrupt:
            try:
                from core.support.interrupt import is_interrupted
                if is_interrupted():
                    output += "\n[interrupted]"
                    break
            except Exception:
                pass
        chunk, cursor = tap.read_from(cursor)
        if chunk:
            last_data = now
            output += chunk
            if PROMPT_STOP_RE.search(chunk):
                time.sleep(0.2)
                rest, cursor = tap.read_from(cursor)
                output += rest
                break
        else:
            if tap.closed:
                break
            tap.wait(0.1)
    return output, cursor, output.endswith("\n[interrupted]")


# -- registry ------------------------------------------------------------------

_taps: dict[str, SessionTap] = {}
_taps_lock = threading.Lock()
_sub_seq = 0


def attach(key: str, channel=None, meta: dict | None = None, session=None) -> SessionTap:
    """Create (or replace) the tap for ``key`` (``session_key|alias`` — one
    tap per conversation+alias); a live shell ``channel`` starts the pump.
    Replacing closes the previous tap — a reconnect under the same key must
    never leave two pumps reading old references. ``meta["alias"]`` carries
    the display name."""
    with _taps_lock:
        old = _taps.get(key)
        if old is not None:
            old.close("replaced")
        tap = SessionTap(key, meta, session=session)
        _taps[key] = tap
    if channel is not None:
        tap._channel = channel
        tap.bind_sender(lambda text, ch=channel: ch.send(text.encode("utf-8")))
        t = threading.Thread(target=_pump_loop, args=(tap, channel),
                             daemon=True, name=f"ssh-tap:{key}")
        tap.pump = t
        t.start()
    return tap


def get(name: str) -> SessionTap | None:
    with _taps_lock:
        return _taps.get(name)


def close_tap(name: str) -> None:
    """From _cleanup_dead (runs under ssh_tool._ssh_lock): mark closed and
    drop from the registry; the pump thread notices and exits by itself, so
    this never blocks on a join."""
    with _taps_lock:
        tap = _taps.pop(name, None)
    if tap is not None:
        tap.close("disconnected")


def _pump_loop(tap: SessionTap, channel) -> None:
    try:
        while not tap.closed:
            if channel.recv_ready():
                data = channel.recv(4096)
                if not data:
                    break               # EOF
                tap.feed_bytes(data)
                continue
            if channel.closed or channel.eof_received:
                while channel.recv_ready():
                    tap.feed_bytes(channel.recv(4096))
                break
            time.sleep(PUMP_POLL_SEC)
    except Exception as e:
        logger.info("ssh tap '%s' pump ended: %s: %s", tap.name, type(e).__name__, e)
    finally:
        tap.close("channel-closed")
        with _taps_lock:
            if _taps.get(tap.name) is tap:
                del _taps[tap.name]


def subscribe(name: str) -> tuple[SessionTap, "queue.Queue"] | None:
    """Register a viewer queue; it receives ("d", str) data / ("i", who, text)
    input / ("x", reason) close events. Returns None when no such session."""
    global _sub_seq
    with _taps_lock:
        tap = _taps.get(name)
        if tap is None:
            return None
        q: queue.Queue = queue.Queue(maxsize=2_000)
        with tap.lock:
            _sub_seq += 1
            tap.subs[_sub_seq] = q
        return tap, q


def unsubscribe(tap: SessionTap, q: "queue.Queue") -> None:
    with tap.lock:
        for qid, cur in list(tap.subs.items()):
            if cur is q:
                del tap.subs[qid]
                break


def send_user_input(name: str, text: str) -> str:
    """Desktop-typed input. Never takes ssh_tool._ssh_lock (an in-flight
    agent command may hold it for minutes). Returns "" or an error reason."""
    if not text or len(text) > MAX_INPUT_FRAME:
        return "bad-input"
    tap = get(name)
    if tap is None:
        return "no-session"
    try:
        tap.send_input(text, who="user")
        return ""
    except TapClosedError:
        return "session-closed"


# Viewer pty bounds — a desktop panel can be huge; the remote side still has
# to allocate line buffers for it.
RESIZE_MIN_COLS, RESIZE_MAX_COLS = 20, 400
RESIZE_MIN_ROWS, RESIZE_MAX_ROWS = 5, 200


def resize(name: str, cols: int, rows: int) -> str:
    """Viewer-driven pty resize (panel width changed). Updates the tap's meta
    (what new viewers are told) and fans out an ("r", cols, rows) event so
    every OTHER viewer's xterm adopts the same size — one pty, many viewers,
    they must agree. The channel request itself runs outside the tap lock,
    same rule as send_input. Passive exec taps have no pty."""
    try:
        cols = int(cols)
        rows = int(rows)
    except (TypeError, ValueError):
        return "bad-input"
    if not (RESIZE_MIN_COLS <= cols <= RESIZE_MAX_COLS
            and RESIZE_MIN_ROWS <= rows <= RESIZE_MAX_ROWS):
        return "bad-input"
    tap = get(name)
    if tap is None:
        return "no-session"
    with tap.lock:
        if tap.closed:
            return "session-closed"
        if tap._channel is None:
            return "not-shell"
        tap.meta["cols"], tap.meta["rows"] = cols, rows
        tap._fanout(("r", cols, rows))
    try:
        tap._channel.resize_pty(width=cols, height=rows)
    except Exception as e:
        # A dead channel is the pump's business (it closes the tap); only a
        # LIVE failure to resize is worth surfacing.
        if not tap.closed:
            logger.warning("ssh tap '%s' resize_pty failed: %s", name, e)
            return "resize-failed"
    return ""


def live_snapshot() -> list[dict]:
    """Session rows for GET /ssh/live — tap registry only; liveness comes from
    the tap itself, no ssh_tool lock involved. ``key`` is the registry key
    (``session_key|alias`` — the stream route's address); ``name`` is the
    display alias."""
    with _taps_lock:
        taps = list(_taps.values())
    rows = []
    for t in taps:
        with t.lock:
            rows.append({
                "key": t.name,
                "name": t.meta.get("alias", t.name),
                "host": t.meta.get("host", ""),
                "user": t.meta.get("user", ""),
                "mode": t.meta.get("mode", ""),
                "origin": t.meta.get("origin", "agent"),
                "session_key": t.meta.get("session_key", ""),
                "cols": t.meta.get("cols"),
                "rows": t.meta.get("rows"),
                "alive": t.alive(),
                "offset": t.ring.w,
                "buffered": t.ring.w - t.ring.base,
                "last_input": t.inputs[-1]["who"] if t.inputs else None,
                "last_activity": t.inputs[-1]["ts"] if t.inputs else None,
                "closed": t.closed,
            })
    return rows
