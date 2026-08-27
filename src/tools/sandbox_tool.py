"""run_sandbox_code — RestrictedPython in-memory compute sandbox."""

import base64
import datetime
import hashlib
import json
import math
import sys
import threading
import time
import warnings

from tools import registry, tool_error

try:
    from RestrictedPython import PrintCollector, compile_restricted, safe_builtins
    from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter
    from RestrictedPython.Guards import (full_write_guard,
                                         guarded_iter_unpack_sequence,
                                         safer_getattr)
except ImportError:
    compile_restricted = None

MAX_CODE_CHARS = 4000
EXEC_TIMEOUT_S = 10.0
LINE_BUDGET = 1_000_000

# Pure helpers absent from safe_builtins; exposing them is free of side effects.
_EXTRA_BUILTINS = {"sum": sum, "enumerate": enumerate, "reversed": reversed,
                   "any": any, "all": all, "bool": bool, "divmod": divmod}

_PREIMPORTED = {"math": math, "datetime": datetime, "base64": base64,
                "hashlib": hashlib, "json": json}


class SandboxAbort(BaseException):
    # BaseException on purpose: a sandboxed ``except Exception`` must not be
    # able to swallow the loop-budget/deadline abort and keep spinning.
    pass


def _make_globals() -> dict:
    return {
        "__builtins__": {**safe_builtins, **_EXTRA_BUILTINS},
        "_getattr_": safer_getattr,
        "_getitem_": default_guarded_getitem,
        "_getiter_": default_guarded_getiter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_write_": full_write_guard,
        "_print_": PrintCollector,
        **_PREIMPORTED,
    }


def _execute(code: str) -> dict:
    """Run the snippet in a watched thread. Returns {result, printed, error}.

    The tracer enforces a line budget + wall-clock deadline so unbounded loops
    die deterministically inside the thread instead of leaking it; the outer
    join() is the backstop for a snippet wedged inside one non-Python call.
    """
    state = {"deadline": time.monotonic() + EXEC_TIMEOUT_S}

    def _tracer(frame, event, arg):
        if event == "line":
            state["lines"] = state.get("lines", 0) + 1
            if state["lines"] > LINE_BUDGET:
                raise SandboxAbort(f"line budget exceeded ({LINE_BUDGET} lines)")
            if time.monotonic() > state["deadline"]:
                raise SandboxAbort(f"deadline exceeded ({EXEC_TIMEOUT_S}s)")
        return _tracer

    def _worker():
        sys.settrace(_tracer)
        try:
            g = _make_globals()
            # RP warns ("Prints, but never reads 'printed'") for every print()
            # use — noise to the gateway log, not actionable for the caller.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                byte_code = compile_restricted(code, "<sandbox>", "exec")
            exec(byte_code, g)
            if "result" in g:
                state["result"] = g["result"]
            if "_print" in g:
                state["printed"] = g["_print"]()
        except SandboxAbort as e:
            state["error"] = f"execution aborted ({e}) — unbounded loop?"
        except BaseException as e:
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            sys.settrace(None)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(EXEC_TIMEOUT_S + 2.0)
    if t.is_alive():
        state["error"] = (f"execution timed out after {EXEC_TIMEOUT_S}s "
                          "(stuck inside a single non-interruptible call)")
    return state


def _json_fallback(o):
    if isinstance(o, (bytes, bytearray)):
        try:
            return o.decode("utf-8")
        except UnicodeDecodeError:
            return o.hex()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    return str(o)


def _run_sandbox_code(args: dict, **kw) -> str:
    if compile_restricted is None:
        return tool_error("RestrictedPython is not installed; sandbox unavailable")
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return tool_error("code is required: a short Python snippet assigning `result`")
    if len(code) > MAX_CODE_CHARS:
        return tool_error(f"code too long ({len(code)} > {MAX_CODE_CHARS} chars); split the work")

    state = _execute(code)
    if "error" in state:
        msg = state["error"]
        if "import" in msg.lower():
            msg += " — imports are not allowed; math/datetime/base64/hashlib/json are pre-imported"
        return tool_error(msg)
    if "result" not in state:
        return tool_error("snippet ran but never assigned `result` — "
                          "assign the output to the variable `result`")

    out = {"result": state["result"]}
    if state.get("printed"):
        out["printed"] = state["printed"]
    return json.dumps(out, ensure_ascii=False, default=_json_fallback)


def _check_sandbox() -> bool:
    return compile_restricted is not None


registry.register(
    name="run_sandbox_code",
    schema={
        "type": "function",
        "function": {
            "name": "run_sandbox_code",
            "description": (
                "Run a short Python snippet in a restricted in-memory sandbox "
                "(RestrictedPython — NOT a shell, no system commands). Use it for "
                "exact math, date/time arithmetic, base64/hex encode-decode, "
                "md5/sha hashing, JSON re-shaping, and small dict/list transforms "
                "— anything LLMs compute poorly by hand. Rules: pure computation "
                "only (no files, network, subprocess, or system access); math, "
                "datetime, base64, hashlib, json are pre-imported and `import` "
                "statements are rejected; the snippet MUST assign its output to "
                "the variable `result`; loops are bounded by a hard line budget "
                "and timeout — never write unbounded loops. Batch several related "
                "computations into one call instead of many round trips. The code "
                "argument must contain only Python code, no prose or explanations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python snippet; assign the final output to `result`. "
                            "Example: result = base64.b64encode('hello'.encode()).decode()"
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    },
    handler=lambda args, **kw: _run_sandbox_code(args, **kw),
    check_fn=_check_sandbox,
    toolset="base",
    read_only=True,
)
