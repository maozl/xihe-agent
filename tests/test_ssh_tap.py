"""L0/L1 for the shared-terminal tap (_ssh_tap) and the ssh_tool rewrite.

L0: ring offsets/trim, incremental UTF-8 decode across chunk splits, prompt
extraction, fan-out, input attribution. L1: _shell_mode reading from the ring
(prompt field + user_input_observed), _exec_mode's chunked drain (stdout/stderr
separation, idle timeout, tap publishing). No paramiko, no network — the
channel is a fake and the echo is pre-published into the ring.
"""
import json
import threading
import time

import pytest

import tools._ssh_tap as tap_mod
from tools._ssh_tap import (
    Ring,
    SessionTap,
    last_prompt_line,
    read_until_prompt,
    strip_ansi,
)
import tools.ssh_tool as ssh_tool
from tools.ssh_tool import SSHSession, _exec_mode, _shell_mode


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    with tap_mod._taps_lock:
        for n in list(tap_mod._taps):
            t = tap_mod._taps.pop(n)
            t.close("test-end")


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# L0 — ring
# ---------------------------------------------------------------------------

def test_ring_read_offsets_and_clamp():
    r = Ring(max_chars=1000)
    r.append("hello ")
    r.append("world")
    s, end = r.read(0)
    assert s == "hello world"
    assert end == 11
    assert r.read(6) == ("world", 11)
    assert r.read(11) == ("", 11)
    assert r.read(99) == ("", 11)


def test_ring_trim_clamps_reads_to_base():
    r = Ring(max_chars=10)
    r.append("0123456789")
    r.append("ABCDEF")            # keeps the last 10 chars: "6789ABCDEF"
    assert r.base == 6
    s, end = r.read(0)            # offset below base clamps up
    assert s == "6789ABCDEF"
    assert end == 16
    r2 = Ring(max_chars=5)
    r2.append("XYZ")
    r2.append("0123456789")
    assert r2.read(0)[0] == "56789"


def test_tap_feed_bytes_survives_multibyte_split():
    tap = SessionTap("t")
    raw = "中文测试".encode("utf-8")
    tap.feed_bytes(raw[:4])       # splits a 3-byte char across chunks
    tap.feed_bytes(raw[4:])
    assert tap.read_from(0)[0] == "中文测试"


# ---------------------------------------------------------------------------
# L0 — prompt extraction + read loop
# ---------------------------------------------------------------------------

def test_last_prompt_line_takes_final_and_strips_ansi():
    raw = "[user@bastion ~]$ ssh 10.0.0.1\r\n[app@web01 ~]$ tail done\r\n"
    assert last_prompt_line(raw) == "[app@web01 ~]$ tail done"
    colored = "\x1b[01;32m[app@web01 ~]$\x1b[0m "
    assert last_prompt_line(colored) == "[app@web01 ~]$"
    assert last_prompt_line("no prompt here") is None
    assert strip_ansi("\x1b[1;32mok\x1b[0m") == "ok"


def test_read_until_prompt_stops_and_drains():
    tap = SessionTap("t")
    tap.publish("Welcome\r\n[user@h ~]$ ")
    raw, cursor, interrupted = read_until_prompt(tap, 0, timeout=2, check_interrupt=False)
    assert "[user@h ~]$" in raw
    assert cursor == tap.ring.w
    assert not interrupted


def test_read_until_prompt_reads_only_new_bytes_from_cursor():
    tap = SessionTap("t")
    tap.publish("old prompt]$ done\n")     # consumed by a previous call
    cursor = tap.ring.w
    tap.publish("new output\n[app@x ~]$ ")
    raw, cursor2, _ = read_until_prompt(tap, cursor, timeout=2, check_interrupt=False)
    assert "old prompt" not in raw
    assert "new output" in raw


def test_read_until_prompt_timeout_on_silence():
    tap = SessionTap("t")
    raw, cursor, _ = read_until_prompt(tap, 0, timeout=0.3, check_interrupt=False)
    assert raw == ""
    assert cursor == 0


def test_read_until_prompt_progress_extends_wait_past_idle():
    tap = SessionTap("t")

    def feed():
        for i in range(10):
            time.sleep(0.05)
            tap.publish(f"tick{i}\n")

    threading.Thread(target=feed, daemon=True).start()
    start = time.time()
    raw, cursor, _ = read_until_prompt(
        tap, 0, timeout=5, check_interrupt=False, idle_timeout=0.2)
    # each tick resets the idle clock: the read outlives the idle window
    assert time.time() - start >= 0.4
    assert "tick" in raw
    assert cursor == tap.ring.w


def test_read_until_prompt_idle_returns_before_ceiling():
    tap = SessionTap("t")           # silent from the start
    start = time.time()
    raw, cursor, _ = read_until_prompt(
        tap, 0, timeout=5, check_interrupt=False, idle_timeout=0.2)
    assert time.time() - start < 2  # returned at the idle mark, not the 5s ceiling
    assert raw == "" and cursor == 0


def test_read_until_prompt_breaks_when_tap_closed():
    tap = SessionTap("t")
    closing = threading.Timer(0.1, lambda: tap.close("test"))
    closing.start()
    start = time.time()
    raw, _, _ = read_until_prompt(tap, 0, timeout=5, check_interrupt=False)
    closing.join()
    assert time.time() - start < 2      # returned at close, not at timeout


# ---------------------------------------------------------------------------
# L0 — fan-out, input attribution, pump
# ---------------------------------------------------------------------------

def test_subscribe_receives_data_input_close_events():
    tap = SessionTap("t")
    with tap_mod._taps_lock:
        tap_mod._taps["t"] = tap
    got = tap_mod.subscribe("t")
    assert got is not None
    _, q = got
    tap.publish("data")
    tap.send_input("ls\n", who="agent")
    tap.close("done")
    events = [q.get(timeout=1) for _ in range(3)]
    assert ("d", "data") in events
    assert ("i", "agent", "ls\n") in events
    assert ("x", "done") in events
    tap_mod.unsubscribe(tap, q)
    assert not tap.subs


def test_send_input_records_offset_and_who_for_user_detection():
    sent = []
    tap = SessionTap("t")
    tap.bind_sender(sent.append)
    tap.publish("banner\n")
    off0 = tap.ring.w
    tap.send_input("whoami\n", who="user")
    tap.publish("root\n[app@h ~]$ ")
    assert sent == ["whoami\n"]
    assert tap.user_input_since(off0) is True
    assert tap.user_input_since(tap.ring.w) is False
    tap.close("x")
    with pytest.raises(tap_mod.TapClosedError):
        tap.send_input("more\n", who="user")


class _FakeChannel:
    def __init__(self, chunks=()):
        self._q = list(chunks)
        self.closed = False
        self.eof_received = False
        self.sent = []
        self.resized = []

    def recv_ready(self):
        return bool(self._q) and not self.closed

    def recv(self, n):
        return self._q.pop(0)

    def send(self, b):
        self.sent.append(b)

    def resize_pty(self, width=0, height=0):
        self.resized.append((width, height))


def test_pump_moves_channel_bytes_into_ring_and_exits_on_close():
    ch = _FakeChannel([b"hello ", "世".encode(), b" [user@h ~]$ "])
    tap = tap_mod.attach("pump-test", channel=ch)
    assert _wait_for(lambda: "user@h" in tap.read_from(0)[0])
    ch.closed = True
    assert _wait_for(lambda: tap.closed)
    tap.pump.join(timeout=2)
    assert tap.close_reason == "channel-closed"
    assert tap_mod.get("pump-test") is None      # pump unregistered itself


def test_attach_replaces_old_tap():
    tap_mod.attach("dup", channel=None)
    old = tap_mod.get("dup")
    tap_mod.attach("dup", channel=None)
    assert old.closed and old.close_reason == "replaced"
    assert tap_mod.get("dup") is not old


def test_resize_updates_meta_fanout_and_channel():
    ch = _FakeChannel()
    tap_mod.attach("rs", channel=ch, meta={"cols": 100, "rows": 30})
    _, q = tap_mod.subscribe("rs")
    assert tap_mod.resize("rs", 160, 44) == ""
    assert ch.resized == [(160, 44)]
    tap = tap_mod.get("rs")
    assert tap.meta["cols"] == 160 and tap.meta["rows"] == 44
    assert q.get(timeout=1) == ("r", 160, 44)
    # out-of-range and garbage values never reach the channel
    assert tap_mod.resize("rs", 5, 30) == "bad-input"
    assert tap_mod.resize("rs", "wide", 30) == "bad-input"
    assert ch.resized == [(160, 44)]
    assert tap_mod.resize("nope", 120, 40) == "no-session"


def test_resize_rejects_passive_tap():
    tap_mod.attach("passive", channel=None)
    assert tap_mod.resize("passive", 120, 40) == "not-shell"


# ---------------------------------------------------------------------------
# L1 — ssh_tool through the tap
# ---------------------------------------------------------------------------

class _FakeTransport:
    def is_active(self):
        return True


class _FakeClient:
    def get_transport(self):
        return _FakeTransport()


def _shell_session(name="bastion", prefill=""):
    sent = []
    session_key = "agent:main:desktop:dm:c1"
    tap = tap_mod.attach(name, channel=None, meta={
        "host": "h", "user": "u", "mode": "shell", "origin": "agent",
        "session_key": session_key, "cols": 100, "rows": 30,
    })
    tap.bind_sender(sent.append)
    if prefill:
        tap.publish(prefill)
    session = SSHSession(name=name, client=_FakeClient(), host="h", port=22,
                         user="u", mode="shell", channel=None,
                         session_key=session_key)
    session.tap = tap
    tap.session = session
    return session, tap, sent


def test_shell_mode_returns_prompt_and_clean_output():
    echo = ("sudo su - app\r\n" + "banner line\r\n" + "[app@web01 ~]$ ")
    session, tap, sent = _shell_session(prefill=echo)
    result = _shell_mode(session, "sudo su - app", "", timeout=5)
    assert sent == ["sudo su - app\n"]
    assert result["mode"] == "shell"
    assert result["prompt"] == "[app@web01 ~]$"
    # prompt lines and the echoed command are cleaned from agent output
    assert "[app@web01 ~]$" not in result["output"]
    assert "sudo su - app" not in result["output"]
    # agent cursor advanced: a following read starts after this window
    assert tap.agent_cursor == tap.ring.w
    assert "user_input_observed" not in result


def test_shell_mode_flags_user_input_in_window():
    session, tap, sent = _shell_session(prefill="out\r\n[user@h ~]$ ")
    tap.send_input("exit\n", who="user")     # desktop typed between agent calls
    result = _shell_mode(session, "whoami", "", timeout=5)
    assert result["user_input_observed"] is True


def test_shell_mode_wraps_target_ip_with_ssh_tt():
    session, tap, sent = _shell_session(prefill="nested\r\n[root@t ~]# ")
    _shell_mode(session, 'tail -1 log', "10.0.0.9", timeout=5)
    assert sent == ['ssh -tt 10.0.0.9 "tail -1 log"\n']


def test_shell_mode_reports_still_running_when_no_prompt():
    # output but no prompt, then silence → idle return flags still_running
    session, tap, sent = _shell_session(prefill="starting hive...\n")
    start = time.time()
    result = _shell_mode(session, "hive -e 'select 1'", "", timeout=0.4)
    assert time.time() - start < 2
    assert result["still_running"] is True
    assert "queue behind" in result["note"]
    assert result["exit_code"] is None
    assert "starting hive" in result["output"]
    assert "prompt" not in result


class _FakeExecChannel:
    """Stand-in for exec_command's stdout.channel with scripted streams."""

    def __init__(self, out_chunks=(), err_chunks=(), exit_code=0, never_exit=False):
        self._out = list(out_chunks)
        self._err = list(err_chunks)
        self._exit = exit_code
        self._never = never_exit
        self.exit_status_ready_flag = False

    def recv_ready(self):
        return bool(self._out)

    def recv(self, n):
        return self._out.pop(0)

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv_stderr(self, n):
        return self._err.pop(0)

    def exit_status_ready(self):
        if self._never:
            return False
        return not self._out and not self._err

    def recv_exit_status(self):
        return self._exit


def test_exec_mode_chunked_drain_and_tap_publish(monkeypatch):
    session, tap, _ = _shell_session(name="direct")
    session.mode = "exec"
    tap.meta["mode"] = "exec"

    chan = _FakeExecChannel(
        out_chunks=[b"line1\n", b"line2\n"], err_chunks=[b"warn\n"], exit_code=0)
    monkeypatch.setattr(
        ssh_tool, "_get_paramiko", lambda: object())   # not used without target_ip

    class _Client:
        def exec_command(self, cmd, timeout=None):
            return None, type("F", (), {"channel": chan})(), None

    session.client = _Client()
    result = _exec_mode(session, "cat file", "", timeout=5)

    assert result["exit_code"] == 0 and result["success"] is True
    assert result["output"] == "line1\nline2\n"
    assert result["stderr"] == "warn\n"
    # viewer stream: synthesized command line, both streams, exit marker
    view = tap.read_from(0)[0]
    assert "$ cat file" in view
    assert "line1" in view and "warn" in view
    assert "[exit 0]" in view


def test_exec_mode_idle_timeout(monkeypatch):
    session, tap, _ = _shell_session(name="direct2")
    session.mode = "exec"

    chan = _FakeExecChannel(out_chunks=[b"started\n"], never_exit=True)
    monkeypatch.setattr(ssh_tool, "_get_paramiko", lambda: object())

    class _Client:
        def exec_command(self, cmd, timeout=None):
            return None, type("F", (), {"channel": chan})(), None

    session.client = _Client()
    start = time.time()
    result = _exec_mode(session, "sleep 999", "", timeout=0.3)
    assert time.time() - start < 5
    assert result["timed_out"] is True
    assert result["exit_code"] == -1
    assert result["output"] == "started\n"


def test_ssh_exec_scopes_session_to_conversation(monkeypatch):
    monkeypatch.setattr(ssh_tool, "_get_paramiko", lambda: object())
    chan = _FakeExecChannel(out_chunks=[b"ok\n"], exit_code=0)

    class _Client:
        def get_transport(self):
            return _FakeTransport()

        def exec_command(self, cmd, timeout=None):
            return None, type("F", (), {"channel": chan})(), None

    sa, _, _ = _shell_session(name="bastion-a")
    sa.mode = "exec"
    sa.client = _Client()
    sb, _, _ = _shell_session(name="bastion-b")
    sb.mode = "exec"
    sb.client = _Client()
    # distinct tap keys above; the alias both conversations use is "bastion"
    sa.name = "bastion"
    sb.name = "bastion"

    # same alias "bastion", two conversations → two independent registrations
    ka = ssh_tool._skey("agent:main:desktop:dm:c1", "bastion")
    kb = ssh_tool._skey("agent:main:desktop:dm:c9", "bastion")
    old = dict(ssh_tool._sessions)
    ssh_tool._sessions[ka] = sa
    ssh_tool._sessions[kb] = sb
    try:
        r = json.loads(ssh_tool._ssh_exec(
            {"session": "bastion", "command": "echo hi"},
            context={"session_key": "agent:main:desktop:dm:c1"}))
        assert r["exit_code"] == 0

        # a third conversation cannot use another conversation's session
        r2 = json.loads(ssh_tool._ssh_exec(
            {"session": "bastion", "command": "echo hi"},
            context={"session_key": "agent:main:desktop:dm:cX"}))
        assert "another conversation" in r2["error"]
    finally:
        ssh_tool._sessions.clear()
        ssh_tool._sessions.update(old)


def test_live_snapshot_shape():
    _shell_session(name="snap")
    rows = tap_mod.live_snapshot()
    row = next(r for r in rows if r["name"] == "snap")
    assert row["origin"] == "agent"
    assert row["session_key"] == "agent:main:desktop:dm:c1"
    assert row["mode"] == "shell"
    assert row["alive"] is True
    assert set(row) >= {"name", "host", "user", "mode", "origin",
                        "session_key", "cols", "rows", "alive", "offset"}
