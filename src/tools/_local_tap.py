"""Channel registry for the agent's local runs — the desktop terminal panel's
live view, same ring/cursor idea as _ssh_tap.

A channel is one ring-backed console addressable by key:
  conv:{session_key}          one-shot `terminal` commands of a conversation
  proc:{session_key}:{name}   one resident process (the `process` tool)

The session_key is the honest scope — core never knows workspaces; the desktop
labels channels by conversation on its side. Concurrent turns in one
conversation interleave in its conv channel like a shared console (the active
counter keeps `running` truthful); a resident process owns its channel
exclusively. Producers are the tool reader threads; viewers (serve's WS
senders) poll the ring at their own cursor under the channel lock.
"""

import atexit
import threading
import time

from core.support.proc_utils import kill_tree
from tools._ssh_tap import Ring

RING_MAX_CHARS = 200_000
# Soft cap on retained channels; idle ones (no live run, no live process) are
# evicted oldest-first. A live process's channel is never evicted — its ring
# is the only copy of the logs.
CHANNEL_CAP = 16
IDLE_EVICT_AFTER_S = 30 * 60


class Channel:
    def __init__(self, key: str, kind: str, name: str, session_key: str):
        self.key = key
        self.kind = kind  # "conv" | "proc"
        self.name = name
        self.session_key = session_key
        self.ring = Ring(max_chars=RING_MAX_CHARS)
        self.lock = threading.Lock()
        # conv: overlapping one-shot invocations; a proc channel is 0/1 by
        # construction (one process owns it).
        self.active = 0
        self.command: str | None = None
        self.cwd: str | None = None
        self.last_touch = time.time()
        # proc channels:
        self.proc = None
        self.started_at: float | None = None
        self.exit_code = None

    # ---- conv channels (terminal tool) ----

    def begin(self, command: str, cwd: str | None) -> None:
        header = f"\r\n\x1b[90m—— agent $ {command}\x1b[0m\r\n"
        if cwd:
            header += f"\x1b[90m   ({cwd})\x1b[0m\r\n"
        with self.lock:
            self.active += 1
            self.command = command
            self.cwd = cwd
            self.last_touch = time.time()
            self.ring.append(header)

    def end(self, exit_code, elapsed_s: float, note: str | None = None) -> None:
        if note:
            line = f"\x1b[90m—— {note} ——\x1b[0m\r\n"
        elif exit_code is None:
            line = "\x1b[90m—— ended ——\x1b[0m\r\n"
        else:
            tail = f"exit {exit_code} · {elapsed_s:.1f}s"
            color = "92" if exit_code == 0 else "91"
            line = f"\x1b[{color}m—— {tail} ——\x1b[0m\r\n"
        with self.lock:
            self.active = max(0, self.active - 1)
            self.last_touch = time.time()
            self.ring.append(line)

    # ---- shared ----

    def publish(self, s: str) -> None:
        if not s:
            return
        with self.lock:
            self.last_touch = time.time()
            self.ring.append(s)

    def read_from(self, cursor: int) -> tuple[str, int]:
        """(text, end_offset) from ``cursor`` — locked: serve's WS sender
        reads from the event-loop executor while tool reader threads append."""
        with self.lock:
            return self.ring.read(cursor)

    def tail_text(self, max_chars: int) -> str:
        with self.lock:
            start = max(self.ring.base, self.ring.w - max_chars)
            text, _ = self.ring.read(start)
            return text

    # ---- proc channels (process tool) ----

    def attach_proc(self, proc, command: str, cwd: str | None) -> None:
        with self.lock:
            self.proc = proc
            self.command = command
            self.cwd = cwd
            self.started_at = time.time()
            self.exit_code = None
            self.last_touch = self.started_at

    def proc_exit(self, exit_code, uptime_s: float) -> None:
        tail = f"exit {exit_code} · {uptime_s:.0f}s"
        color = "92" if exit_code == 0 else "91"
        with self.lock:
            self.exit_code = exit_code
            self.last_touch = time.time()
            self.ring.append(f"\r\n\x1b[{color}m—— {tail} ——\x1b[0m\r\n")

    def live_proc(self):
        """The Popen while it is still alive, else None. Lock-free poll() is
        safe (Popen.poll is idempotent); returning the ref under no lock is
        fine because ownership outlives the registry."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            return proc
        return None

    def snapshot(self) -> dict:
        with self.lock:
            if self.kind == "conv":
                running = self.active > 0
            else:
                running = self.proc is not None and self.proc.poll() is None
            snap = {
                "key": self.key,
                "kind": self.kind,
                "name": self.name,
                "session_key": self.session_key,
                "running": running,
                "command": self.command,
                "cwd": self.cwd,
                "offset": self.ring.w,
                "buffered": self.ring.w - self.ring.base,
                "ts": time.time(),
            }
            if self.kind == "proc":
                snap.update({
                    "pid": self.proc.pid if self.proc else None,
                    "started_at": self.started_at,
                    "exit_code": self.exit_code,
                })
            return snap


_channels: dict[str, Channel] = {}
_reg_lock = threading.Lock()


def _evict_locked() -> None:
    if len(_channels) <= CHANNEL_CAP:
        return
    now = time.time()
    idle = [c for c in _channels.values()
            if now - c.last_touch > IDLE_EVICT_AFTER_S
            and not (c.kind == "proc" and c.live_proc())]
    idle.sort(key=lambda c: c.last_touch)
    for c in idle:
        if len(_channels) <= CHANNEL_CAP:
            break
        _channels.pop(c.key, None)


def conv_channel(session_key: str) -> Channel:
    key = f"conv:{session_key}"
    with _reg_lock:
        ch = _channels.get(key)
        if ch is None:
            ch = Channel(key, "conv", session_key, session_key)
            _channels[key] = ch
        _evict_locked()
        return ch


def proc_channel(session_key: str, name: str) -> Channel:
    key = f"proc:{session_key}:{name}"
    with _reg_lock:
        ch = _channels.get(key)
        if ch is None:
            ch = Channel(key, "proc", name, session_key)
            _channels[key] = ch
        _evict_locked()
        return ch


def get(key: str) -> Channel | None:
    with _reg_lock:
        return _channels.get(key)


def list_channels() -> list[dict]:
    with _reg_lock:
        return [c.snapshot() for c in _channels.values()]


def stop_channel(key: str) -> dict:
    """Kill one channel's live process (desktop stop button / REST)."""
    ch = get(key)
    if not ch:
        return {"ok": False, "error": "channel not found"}
    proc = ch.live_proc()
    if proc is None:
        return {"ok": True, "key": key, "stopped": False}
    kill_tree(proc.pid)
    return {"ok": True, "key": key, "stopped": True}


def stop_all() -> None:
    """Kill every live resident process. Called at serve shutdown and
    interpreter exit — without it, the supervisor dying orphans the web
    backends the user believes are managed. Best-effort (kill_tree never
    raises); idempotent."""
    with _reg_lock:
        procs = [c.proc for c in _channels.values() if c.live_proc()]
    for proc in procs:
        try:
            kill_tree(proc.pid)
        except Exception:
            pass


atexit.register(stop_all)
