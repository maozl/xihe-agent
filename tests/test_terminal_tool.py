"""L1 — terminal tool handler with the shell mocked out (no real subprocess).

Demonstrates the L1 pattern: mock the external IO boundary (``subprocess.Popen``)
and assert the handler's output shaping — truncation, exit-code interpretation —
via the JSON result string. No model, no network.
"""
import json

import tools.terminal as term


class _FakeProc:
    """Minimal Popen stand-in exposing what _execute_terminal touches."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout=None):
        return (self._stdout, self._stderr)

    def kill(self):
        pass

    def wait(self):
        pass


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
