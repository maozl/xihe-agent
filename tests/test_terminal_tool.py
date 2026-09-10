"""L1 — terminal tool handler with the shell mocked out (no real subprocess).

Demonstrates the L1 pattern: mock the external IO boundary (``subprocess.Popen``)
and assert the handler's output shaping — truncation, exit-code interpretation —
via the JSON result string. No model, no network.
"""
import io
import json

import tools.terminal as term
from tools import _local_tap


class _FakeProc:
    """Minimal Popen stand-in exposing what _execute_terminal touches: bytes
    pipes drained via ``read1`` by the reader threads, ``wait(timeout)``."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = io.BytesIO(stdout.encode("utf-8"))
        self.stderr = io.BytesIO(stderr.encode("utf-8"))
        self.returncode = returncode

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def test_long_output_is_truncated(monkeypatch):
    monkeypatch.setattr(
        term.subprocess, "Popen",
        lambda *a, **k: _FakeProc(stdout="x" * 40000),
    )
    data = json.loads(term._execute_terminal(command="grep foo bar", timeout=10))

    assert data["exit_code"] == 0
    assert "OUTPUT TRUNCATED" in data["output"]


def test_grep_exit1_interpreted_as_no_matches(monkeypatch):
    # grep exits 1 when nothing matches — that's not an error. The handler
    # annotates it so the agent doesn't treat it as a failure.
    monkeypatch.setattr(
        term.subprocess, "Popen",
        lambda *a, **k: _FakeProc(returncode=1),
    )
    data = json.loads(term._execute_terminal(command="grep foo bar", timeout=10))

    assert data["exit_code"] == 1
    assert data["exit_code_meaning"] == "No matches found (not an error)"


def test_run_mirrors_to_conv_channel(monkeypatch):
    # The live view contract: the conversation's channel ring carries the
    # command header, the streamed chunks, and an exit line; `running` is
    # false again afterwards.
    monkeypatch.setattr(
        term.subprocess, "Popen",
        lambda *a, **k: _FakeProc(stdout="hello\n", returncode=0),
    )
    ch = _local_tap.conv_channel("t-term")
    before = ch.ring.w
    data = json.loads(term._execute_terminal(
        command="echo hi", timeout=10, context={"session_key": "t-term"}))

    assert data["exit_code"] == 0
    streamed, _ = ch.read_from(before)
    assert "agent $ echo hi" in streamed
    assert "hello" in streamed
    assert "exit 0" in streamed
    snap = ch.snapshot()
    assert snap["running"] is False
    assert snap["command"] == "echo hi"


def test_runs_are_scoped_per_conversation(monkeypatch):
    # Two conversations → two channels; a run in one never lands in the other.
    monkeypatch.setattr(
        term.subprocess, "Popen",
        lambda *a, **k: _FakeProc(stdout="mine\n", returncode=0),
    )
    other = _local_tap.conv_channel("t-other")
    before_other = other.ring.w
    term._execute_terminal(
        command="echo mine", timeout=10, context={"session_key": "t-main"})
    streamed, _ = other.read_from(before_other)
    assert "echo mine" not in streamed
