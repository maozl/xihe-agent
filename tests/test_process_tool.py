"""L1 — process tool (resident processes) with the shell mocked out.

Covers the contracts that matter for the desktop terminal panel and the agent:
startup-window fast-fail vs handoff, per-conversation naming (derived names
never silently replace; explicit names do), and log reads off the channel ring.
"""
import io
import json
import threading

import tools.process_tool as pt
from tools import _local_tap


class _FakeProc:
    """Popen stand-in: read1-able bytes pipes, controllable liveness."""

    def __init__(self, stdout="", stderr="", exit_code=0, stay_alive=False):
        self.stdout = io.BytesIO(stdout.encode("utf-8"))
        self.stderr = io.BytesIO(stderr.encode("utf-8"))
        self.pid = 4242
        self.returncode = None
        self._stay_alive = stay_alive
        self._code = exit_code
        self._lock = threading.Lock()
        self._killed = threading.Event()

    def poll(self):
        with self._lock:
            if self._stay_alive and not self._killed.is_set():
                return None
            return self._code

    def wait(self, timeout=None):
        if self.poll() is None:
            self._killed.wait(timeout)  # resident: block like a real server
            if self._killed.is_set():
                self._stay_alive = False
        return self.poll()

    def kill(self):
        self._killed.set()


def _ctx(sk):
    return {"context": {"session_key": sk}}


def test_start_fast_fail_returns_output(monkeypatch):
    monkeypatch.setattr(
        pt.subprocess, "Popen",
        lambda *a, **k: _FakeProc(stderr="port already in use\n", exit_code=1),
    )
    data = json.loads(pt._process(
        {"action": "start", "command": "python -m http.server 8080",
         "startup_timeout": 1}, **_ctx("t-p1")))

    assert data["started"] is False
    assert data["exit_code"] == 1
    assert "port already in use" in data["output"]
    assert "startup" in data["error"]


def test_start_alive_hands_off(monkeypatch):
    procs = []

    def fake_pop(*a, **k):
        p = _FakeProc(stdout="listening on 8000\n", stay_alive=True)
        procs.append(p)
        return p

    monkeypatch.setattr(pt.subprocess, "Popen", fake_pop)
    data = json.loads(pt._process(
        {"action": "start", "command": "python app.py", "name": "web",
         "startup_timeout": 1}, **_ctx("t-p2")))

    assert data["started"] is True
    assert data["name"] == "web"
    assert data["pid"] == 4242
    assert data["channel"] == "proc:t-p2:web"
    assert "listening" in data["output"]
    # the channel is the live-view source: header + streamed chunk are in it
    text = _local_tap.get("proc:t-p2:web").tail_text(10_000)
    assert "agent $ python app.py" in text
    assert "listening on 8000" in text

    for p in procs:
        p.kill()  # release the fakes' blocked wait/reaper threads


def test_derived_name_does_not_replace_live_process(monkeypatch):
    procs = []

    def fake_pop(*a, **k):
        p = _FakeProc(stay_alive=True)
        procs.append(p)
        return p

    monkeypatch.setattr(pt.subprocess, "Popen", fake_pop)
    first = json.loads(pt._process(
        {"action": "start", "command": "python a.py", "startup_timeout": 1},
        **_ctx("t-p3")))
    second = json.loads(pt._process(
        {"action": "start", "command": "python b.py", "startup_timeout": 1},
        **_ctx("t-p3")))

    assert first["name"] == "python"
    assert second["name"] == "python-2"
    assert json.loads(pt._process({"action": "check", "name": "python"},
                                  **_ctx("t-p3")))["running"] is True
    for p in procs:
        p.kill()


def test_explicit_name_replaces_live_process(monkeypatch):
    procs = []

    def fake_pop(*a, **k):
        p = _FakeProc(stay_alive=True)
        procs.append(p)
        return p

    monkeypatch.setattr(pt.subprocess, "Popen", fake_pop)
    pt._process({"action": "start", "command": "python a.py", "name": "web",
                 "startup_timeout": 1}, **_ctx("t-p4"))

    killed = []

    def fake_kill(pid):
        killed.append(pid)
        procs[0].kill()

    monkeypatch.setattr(pt, "kill_tree", fake_kill)
    data = json.loads(pt._process(
        {"action": "start", "command": "python b.py", "name": "web",
         "startup_timeout": 1}, **_ctx("t-p4")))

    assert killed == [4242]
    assert data["started"] is True
    assert data["name"] == "web"
    for p in procs:
        p.kill()


def test_stop_kills_and_scopes_to_conversation(monkeypatch):
    procs = []

    def fake_pop(*a, **k):
        p = _FakeProc(stay_alive=True)
        procs.append(p)
        return p

    monkeypatch.setattr(pt.subprocess, "Popen", fake_pop)
    pt._process({"action": "start", "command": "python a.py", "name": "web",
                 "startup_timeout": 1}, **_ctx("t-p5"))

    # another conversation cannot see (let alone stop) this conversation's proc
    other = json.loads(pt._process({"action": "stop", "name": "web"},
                                   **_ctx("t-other")))
    assert "error" in other

    killed = []

    def fake_kill(pid):
        killed.append(pid)
        procs[0].kill()

    monkeypatch.setattr(pt, "kill_tree", fake_kill)
    stopped = json.loads(pt._process({"action": "stop", "name": "web"},
                                     **_ctx("t-p5")))
    assert stopped["status"] == "stopped"
    assert killed == [4242]
    for p in procs:
        p.kill()


def test_output_reads_channel_tail(monkeypatch):
    monkeypatch.setattr(
        pt.subprocess, "Popen",
        lambda *a, **k: _FakeProc(stdout="line1\nline2\n", exit_code=0),
    )
    pt._process({"action": "start", "command": "echo x", "name": "logs",
                 "startup_timeout": 1}, **_ctx("t-p6"))
    data = json.loads(pt._process({"action": "output", "name": "logs"},
                                  **_ctx("t-p6")))
    assert "line2" in data["output"]
