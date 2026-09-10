"""L1 tests for run_sandbox_code — behavior of the RestrictedPython sandbox,
the loop/deadline guard, and its base-toolset placement (main + subagents)."""
import json

import pytest

import tools.sandbox_tool as st
from core.toolsets import SUBAGENT_BLOCKED_TOOLS, TOOLSETS
from tools import registry


def run(code):
    return json.loads(st._run_sandbox_code({"code": code}))


@pytest.mark.skipif(st.compile_restricted is None,
                    reason="RestrictedPython not installed")
class TestCompute:
    def test_math(self):
        assert run("result = math.factorial(10)")["result"] == 3628800

    def test_base64_hash_chain(self):
        out = run("result = base64.b64encode("
                  "hashlib.md5(b'abc').hexdigest().encode()).decode()")
        import base64, hashlib
        assert out["result"] == base64.b64encode(
            hashlib.md5(b"abc").hexdigest().encode()).decode()

    def test_datetime_arithmetic(self):
        out = run("d1 = datetime.datetime(2026, 1, 1)\n"
                  "d2 = datetime.datetime(2026, 3, 1)\n"
                  "result = (d2 - d1).days")
        assert out["result"] == 59

    def test_json_reshape(self):
        out = run("rows = json.loads('[{\"n\": 1}, {\"n\": 2}, {\"n\": 3}]')\n"
                  "result = sum(r['n'] for r in rows if r['n'] > 1)")
        assert out["result"] == 5

    def test_dict_write_allowed(self):
        out = run("d = {'a': [1, 2, 3]}\nd['sum'] = sum(d['a'])\nresult = d")
        assert out["result"] == {"a": [1, 2, 3], "sum": 6}

    def test_bytes_result_decoded(self):
        assert run("result = b'abc'")["result"] == "abc"

    def test_print_captured(self):
        out = run("x = 41\nprint('value', x + 1)\nresult = x")
        assert out["result"] == 41
        assert "value 42" in out["printed"]


@pytest.mark.skipif(st.compile_restricted is None,
                    reason="RestrictedPython not installed")
class TestContractErrors:
    def test_missing_result(self):
        assert "result" in run("x = 1")["error"]

    def test_empty_code(self):
        assert "error" in run("")

    def test_code_too_long(self):
        assert "too long" in run("result = 1  # " + "x" * 5000)["error"]

    def test_import_rejected_with_hint(self):
        out = run("import os\nresult = 1")
        assert "error" in out
        assert "pre-imported" in out["error"]

    def test_open_rejected(self):
        assert "error" in run("result = open('x')")

    def test_dunder_attr_rejected(self):
        assert "error" in run("result = ().__class__")

    def test_runtime_error_surfaced(self):
        out = run("result = 1 / 0")
        assert "ZeroDivisionError" in out["error"]


@pytest.mark.skipif(st.compile_restricted is None,
                    reason="RestrictedPython not installed")
class TestLoopGuard:
    def test_infinite_loop_aborts(self, monkeypatch):
        monkeypatch.setattr(st, "LINE_BUDGET", 20000)
        out = run("while True:\n    pass")
        assert "aborted" in out["error"]

    def test_deadline_aborts(self, monkeypatch):
        monkeypatch.setattr(st, "LINE_BUDGET", 10**9)
        monkeypatch.setattr(st, "EXEC_TIMEOUT_S", 0.05)
        out = run("while True:\n    pass")
        assert "error" in out

    def test_abort_not_swallowed_by_except_exception(self, monkeypatch):
        # SandboxAbort is BaseException so sandboxed error handling cannot
        # rescue an unbounded loop and keep the thread spinning.
        monkeypatch.setattr(st, "LINE_BUDGET", 20000)
        out = run("try:\n    while True:\n        pass\n"
                  "except Exception:\n    pass\nresult = 1")
        assert "aborted" in out["error"]


class TestRegistration:
    def test_in_base_toolset_table(self):
        assert "run_sandbox_code" in TOOLSETS["base"]["tools"]

    def test_registered_side_effect_free(self):
        assert registry.get_toolset_for_tool("run_sandbox_code") == "base"
        assert registry.is_read_only("run_sandbox_code")

    def test_not_subagent_blocked(self):
        assert "run_sandbox_code" not in SUBAGENT_BLOCKED_TOOLS

    def test_visible_to_subagents(self):
        names = {s["function"]["name"]
                 for s in registry.get_schemas(toolsets={"base"}, subagent=True)}
        assert "run_sandbox_code" in names
