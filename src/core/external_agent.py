"""External-agent driver — drive a headless claude/codex CLI subprocess over
its native NDJSON protocol.

Ported from ``xihe-desktop/src/main/claude.ts`` (the hardened ClaudeRunner) into
the xihe-agent kernel so all three run modes (CLI / gateway / serve) share one
implementation. Triggered via the ``external_agent`` tool (delegate semantics:
xihe stays the orchestrator, invokes the engine as a reasoning "brain" for a
subtask).

One architecture, two engine lifecycle policies (both share spawn hardening,
PID-file orphan sweep, tree-kill interrupt registration, (engine, session_key)
resume-id storage, and the unified event shape):

  - claude — WARM: one long-lived child per session_key, fed across turns
    (each turn is one NDJSON line on stdin; claude blocks on stdin after each
    result). Cold-resume respawns with ``--resume <session_id>``. Hardening:
    write-turn-immediately-on-cold-start (stream-json needs the first stdin
    line before claude boots), 45s ready timeout, 10min idle reap.
  - codex — ONE-SHOT: ``codex exec`` runs to ``turn.completed`` and exits, so
    each turn spawns fresh; continuity rides on ``exec resume <thread_id>``
    (captured from ``thread.started``). The prompt IS stdin, closed after
    write — codex blocks reading stdin until EOF (opposite of claude's
    long-lived stdin). Requires ``--disable multi_agent``: litellm-family
    gateways reject the ``namespace`` tool type that feature adds (500).

Unified event shape (the reduction target — 6-variant subset of ``ServeEvent``,
no hello/turn_start: a tool call doesn't need them):
  {"type": "text_delta"|"thought_delta"|"tool_call"|"tool_result"|
           "complete"|"error", ...}

NDJSON→event mapping per engine: claude mirrors claude.ts (system/init→ready;
stream_event deltas→text/thought_delta; tool_use block→tool_call; user
tool_result→tool_result; result{success|error}→complete|error). codex maps
thread.started→thread_id capture; item command_execution/mcp_tool_call/
file_change→tool_call/result; item reasoning→thought_delta; item
agent_message→text; turn.completed/turn.failed→complete/error.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.proc_utils import kill_tree, list_process_command_line, process_alive
from core.safe_env import build_safe_env, sanitize_error

logger = logging.getLogger(__name__)

# Lifecycle constants — ported from claude.ts.
# Spawn but no system/init within this → claude stuck on init (version mismatch
# with stream-json, or internal gateway unreachable): emit an error instead of
# letting the caller block forever.
READY_TIMEOUT_S = 45
# A session idle this long after a turn → reap the process; next run_turn cold
# --resumes losslessly.
IDLE_TTL_S = 10 * 60
# Orphan-sweep fingerprints — our spawn always passes these, so a leftover pid
# whose command line lacks its engine's marker is a reused pid we must NOT
# kill. Per-engine because the two CLIs share no flags.
_FINGERPRINTS = {
    "claude": "--input-format stream-json",
    "codex": "--disable multi_agent",
}


@dataclass
class ExternalAgentResult:
    """Outcome of one external-agent turn."""
    final_text: str = ""
    exit_reason: str = "completed"  # completed|interrupted|failed
    tool_trace: list = field(default_factory=list)
    duration_seconds: float = 0.0
    error: Optional[str] = None
    session_id: Optional[str] = None


@dataclass
class TurnSpec:
    """Per-turn spawn parameters (baked into the session at spawn time)."""
    cwd: Optional[str]
    # {api_key?: str, base_url?: str, wire_api?: str, max_tokens?: int} —
    # base_url/wire_api are codex provider wiring (driver turns them into
    # `-c model_providers.xihe.*`); max_tokens maps to each engine's output cap.
    llm: dict
    # claude: bypassPermissions etc. (claude --permission-mode values).
    # codex: read-only | workspace-write | danger-full-access (→ -s), or
    # bypassPermissions → --dangerously-bypass-approvals-and-sandbox.
    permission_mode: str = "bypassPermissions"
    model: Optional[str] = None
    bin: str = "claude"
    debug: bool = False               # full prompt + NDJSON traffic → trace log
    # Raw CLI args appended verbatim before the engine's resume flags (claude:
    # --resume; codex: resume/"-") — from external_agents.<engine>.extra_args.
    extra_args: list = field(default_factory=list)


def _data_dir() -> Path:
    try:
        from core.config import AGENT_HOME
        if AGENT_HOME:
            return Path(AGENT_HOME)
    except Exception:
        pass
    return Path.home() / ".xihe-agent"


def _pids_path() -> Path:
    return _data_dir() / "external-agent-pids.json"


def _log_path() -> Path:
    return _data_dir() / "external-agent.log"


_log_lock = threading.Lock()


def _log_event(line: str) -> None:
    """Append a timestamped, secret-scrubbed event line to the dedicated log.

    The ``[ts]``-prefixed shape is deliberately distinct from claude's raw
    NDJSON stdout (which starts with ``{``) so the log stays greppable.
    """
    try:
        import datetime
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with _log_lock, open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {sanitize_error(line)}\n")
    except Exception:
        pass


def _trace_path() -> Path:
    return _data_dir() / "external-agent-trace.log"


_trace_lock = threading.Lock()


def _log_traffic(line: str) -> None:
    """Raw turn traffic (full prompts out, claude's NDJSON frames back).

    Debug-only, opt-in via external_agents.claude.debug. Separate file from
    the event log because a long turn is thousands of frames; a size guard
    truncates so a forgotten toggle can't fill the disk.
    """
    try:
        import datetime
        p = _trace_path()
        try:
            if p.exists() and p.stat().st_size > 50 * 1024 * 1024:
                p.unlink()
        except OSError:
            pass
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        with _trace_lock, open(p, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except Exception:
        pass


def _read_pids() -> List[dict]:
    try:
        with open(_pids_path(), encoding="utf-8") as f:
            arr = json.load(f)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def _write_pids(entries: List[dict]) -> None:
    # Best-effort: failure only affects next-startup sweep, not this process.
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(_pids_path(), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _add_pid(entry: dict) -> None:
    entries = [e for e in _read_pids() if e.get("pid") != entry.get("pid")]
    entries.append(entry)
    _write_pids(entries)


def _remove_pid(pid) -> None:
    if not pid:
        return
    _write_pids([e for e in _read_pids() if e.get("pid") != pid])


# (engine, session_key) → engine session/thread id. Survives process death
# WITHIN a xihe run so a respawned child resumes the same conversation. Keyed
# by engine: one xihe session may alternate claude/codex turns, and a bare
# session_key would let one engine resume the other's conversation id.
_resume_ids: Dict[tuple, str] = {}


class ExternalAgentDriver:
    """Engine-agnostic interface for a headless external agent."""

    def run_turn(self, session_key: str, prompt: str, spec: TurnSpec,
                 on_event: Callable[[dict], None]) -> ExternalAgentResult:
        raise NotImplementedError

    def interrupt(self, session_key: str) -> None:
        """Stop/kill the session (covers the boot window too). Default: no-op."""
        pass

    def dispose(self, session_key: str) -> None:
        """Tear down an idle/ended session. Default: no-op."""
        pass


class _SweptDriver(ExternalAgentDriver):
    """Shared once-per-driver orphan sweep (each engine singleton sweeps at
    most once; the second sweep finds an emptied PID file and no-ops)."""

    def __init__(self):
        self._swept = False

    def _maybe_sweep(self) -> None:
        if self._swept:
            return
        self._swept = True
        try:
            sweep_orphans()
        except Exception:
            logger.debug("orphan sweep failed", exc_info=True)


# Shared spawn machinery — identical hardening for both engines.

def _resolve_bin(bin_name: str) -> str:
    """Absolute existing path stays; bare name resolves via PATH. Left as-is
    when missing so Popen (not we) produces the spawn error."""
    if os.path.isabs(bin_name) and os.path.exists(bin_name):
        return bin_name
    return shutil.which(bin_name) or bin_name


def _routed_argv(bin_resolved: str, args: List[str]) -> List[str]:
    """npm-installed CLIs ship as .cmd/.bat batch wrappers on Windows;
    CreateProcess can't run those directly (WinError 193 "%1 is not a valid
    Win32 application"), so route through cmd.exe. stdio pipes inherit
    through the wrapper, and taskkill /T still reaps the whole node tree
    rooted at the cmd.exe pid."""
    argv = [bin_resolved] + args
    if os.name == "nt" and bin_resolved.lower().endswith((".cmd", ".bat")):
        argv = ["cmd.exe", "/c"] + argv
    return argv


def _spawn_cli(bin_resolved: str, args: List[str], cwd: str,
               env: dict) -> subprocess.Popen:
    """Spawn an engine CLI with piped stdio and a clean process group."""
    popen_kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # own pgid for killpg
    else:
        # New process group (harmless; taskkill /T walks the tree regardless).
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(_routed_argv(bin_resolved, args), **popen_kwargs)


def _start_stderr_drain(proc: subprocess.Popen, buf: List[str],
                        tag: str) -> threading.Thread:
    """Drain stderr on a daemon thread — an undrained pipe eventually blocks
    the child. Tail lands in *buf* for timeout/error messages."""
    def _drain():
        try:
            while True:
                raw = proc.stderr.readline()
                if not raw:
                    break
                chunk = raw.decode("utf-8", "replace")
                buf.append(chunk)
                _log_event("stderr: " + chunk.rstrip())
        except Exception as e:
            # a dead drain thread blocks the child on a full pipe — this must
            # be visible, not a silent hang later
            _log_event(f"stderr drain thread died: {e!r}")

    t = threading.Thread(target=_drain, name=f"ext-agent-stderr-{tag}",
                         daemon=True)
    t.start()
    return t


_CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


class CodexDriver(_SweptDriver):
    """Drives the local ``codex`` CLI — one process per turn (``codex exec``
    is one-shot), conversation continuity via ``exec resume <thread_id>``."""

    def __init__(self):
        super().__init__()

    def run_turn(self, session_key: str, prompt: str, spec: TurnSpec,
                 on_event: Callable[[dict], None]) -> ExternalAgentResult:
        self._maybe_sweep()
        # Lazy import keeps this module importable without the tools package
        # (and is_interrupted resolves the agent bound around tool dispatch).
        from tools.interrupt import is_interrupted, register_subprocess, unregister_subprocess

        t0 = time.monotonic()
        proc = self._spawn(session_key, spec)
        if proc is None:
            return ExternalAgentResult(
                exit_reason="failed",
                error="无法启动 codex 子进程（见 external-agent.log）",
                duration_seconds=round(time.monotonic() - t0, 2),
            )

        kill_handle = _TreeKillHandle(proc.pid)
        register_subprocess(kill_handle)
        stderr_buf: List[str] = []
        _start_stderr_drain(proc, stderr_buf, f"codex-{session_key}")

        scratch = {
            "thread_id": None,
            "texts": [],           # agent_message item texts, in order
            "done": False,
            "final_text": "",
            "exit_reason": None,
            "error": None,
            "tool_trace": [],
            "on_event": on_event,
        }

        try:
            # The prompt IS stdin and EOF gates codex's boot — write then
            # CLOSE. (claude keeps stdin open across turns; codex must see
            # EOF or it hangs at "Reading additional input from stdin...".)
            if spec.debug:
                _log_traffic(f">>> PROMPT (codex) session={session_key}\n{prompt}")
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.close()
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break  # EOF — proc died (interrupt / crash / early exit)
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if spec.debug:
                    _log_traffic(f"<<< {text}")
                self._handle_line(session_key, scratch, text)
                if scratch["done"]:
                    break
        except Exception as e:
            scratch["error"] = scratch["error"] or f"读取 codex stdout 失败：{e}"

        unregister_subprocess(kill_handle)
        result = self._build_result(scratch, t0, is_interrupted)
        # One-shot: nothing to keep warm. It exits on its own after the turn
        # verdict; kill_tree also covers stopped-mid-turn, then drop the PID.
        kill_tree(proc.pid)
        _remove_pid(proc.pid)
        return result

    def interrupt(self, session_key: str) -> None:
        pass  # no cross-turn process; in-turn stops ride tools.interrupt

    def dispose(self, session_key: str) -> None:
        pass  # see interrupt()

    def _spawn(self, session_key: str, spec: TurnSpec) -> Optional[subprocess.Popen]:
        bin_resolved = _resolve_bin(spec.bin or "codex")
        cwd = spec.cwd or str(Path.home())

        args = ["exec", "--json", "--skip-git-repo-check", "-C", cwd]
        # litellm-family gateways 500 on the `namespace` tool type that the
        # multi_agent feature sends — required flag, not an optimization.
        args += ["--disable", "multi_agent"]
        if spec.model:
            args += ["-m", str(spec.model)]
        pm = (spec.permission_mode or "workspace-write").strip()
        if pm in ("bypassPermissions", "bypass"):
            args.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            args += ["-s", pm if pm in _CODEX_SANDBOX_MODES else "workspace-write"]
        # Inline provider (external_agents.codex.base_url set): -c overrides
        # fully define provider "xihe" on the command line (verified 0.146.1),
        # overriding config.toml's model_provider for this run. The tool layer
        # gates this on the explicit key — no main-config base_url fallback —
        # so config.toml wiring stays authoritative by default.
        base_url = str(spec.llm.get("base_url") or "").strip().rstrip("/")
        if base_url:
            wire = str(spec.llm.get("wire_api") or "responses").strip().lower()
            if wire not in ("responses", "chat"):
                wire = "responses"
            args += [
                "-c", 'model_provider="xihe"',
                "-c", 'model_providers.xihe.name="xihe"',
                "-c", f'model_providers.xihe.base_url="{base_url}"',
                "-c", 'model_providers.xihe.env_key="CODEX_API_KEY"',
                "-c", f'model_providers.xihe.wire_api="{wire}"',
            ]
        # Output cap (external_agents.codex.max_tokens) — same failure mode the
        # main agent's max_completion_tokens guards: a long single reply gets
        # cut mid-sentence at the default cap. CAVEAT: measured on 0.146 with a
        # custom provider codex does NOT send this key (gateway default applies)
        # — kept for versions/future support. Before extra_args so a same-key
        # -c there still gets the last word.
        if spec.llm.get("max_tokens"):
            args += ["-c", f"model_max_output_tokens={int(spec.llm['max_tokens'])}"]
        if spec.extra_args:
            args += [str(a) for a in spec.extra_args]
        resume_id = _resume_ids.get(("codex", session_key))
        if resume_id:
            args += ["resume", resume_id]
        args.append("-")            # prompt comes from stdin

        # Creds: only the key is injected via env (CODEX_API_KEY outranks
        # auth.json); provider/base_url travel as -c argv, never env — codex
        # reads no base_url env for custom providers.
        user_env: dict = {}
        if spec.llm.get("api_key"):
            user_env["CODEX_API_KEY"] = spec.llm["api_key"]
        # 中文 Windows 上 python 子进程 stdio 默认 GBK（同 claude 侧的坑）
        user_env.setdefault("PYTHONUTF8", "1")
        user_env.setdefault("PYTHONIOENCODING", "utf-8")
        env = build_safe_env(user_env)

        try:
            proc = _spawn_cli(bin_resolved, args, cwd, env)
        except Exception as e:
            _log_event(f"spawn FAILED (codex) bin={bin_resolved} err={e}")
            return None
        _add_pid({"pid": proc.pid, "engine": "codex", "sessionId": resume_id, "cwd": cwd})
        _log_event(
            f"spawn (codex) pid={proc.pid} bin={bin_resolved} cwd={cwd} "
            f"resume={resume_id or 'none'} model={spec.model or '?'} perm={pm}"
        )
        # Full argv for debugging (flags only — creds go via env, never argv).
        _log_event(f"  cmd: {' '.join(_routed_argv(bin_resolved, args))}")
        return proc

    def _handle_line(self, session_key: str, scratch: dict, text: str) -> None:
        try:
            obj = json.loads(text)
        except Exception:
            return  # not JSON (codex may print a banner line first)
        if not isinstance(obj, dict):
            return
        t = obj.get("type")

        if t == "thread.started":
            tid = obj.get("thread_id")
            if isinstance(tid, str) and tid:
                scratch["thread_id"] = tid
                _resume_ids[("codex", session_key)] = tid
            return
        if t == "turn.completed":
            _finalize(scratch, exit_reason="completed",
                      final_text="\n\n".join(scratch["texts"]))
            return
        if t == "turn.failed":
            msg = (obj.get("error") or {}).get("message") or "codex turn failed"
            _finalize(scratch, exit_reason="failed", error=msg,
                      emit_kind="error", emit_msg=msg)
            return
        if t == "error":
            # "Reconnecting... X/Y" is codex's transient retry noise — the
            # turn verdict (completed/failed) is authoritative, so hold the
            # message only as an EOF fallback, never finalize on it.
            msg = obj.get("message") or ""
            if msg and not msg.startswith("Reconnecting"):
                scratch["error"] = scratch["error"] or msg
            return
        if not isinstance(t, str) or not t.startswith("item."):
            return
        item = obj.get("item") or {}
        if not isinstance(item, dict):
            return
        it = item.get("type")
        phase = t[len("item."):]

        if it == "reasoning":
            # Summaries arrive whole on completed; started/updated would
            # duplicate the text if also emitted.
            if phase == "completed" and isinstance(item.get("text"), str) and item["text"]:
                _emit(scratch, {"type": "thought_delta", "text": item["text"]})
        elif it == "agent_message":
            if phase == "completed" and isinstance(item.get("text"), str):
                scratch["texts"].append(item["text"])
                _emit(scratch, {"type": "text_delta", "text": item["text"]})
        elif it == "command_execution":
            if phase == "started":
                cmd = str(item.get("command") or "")
                scratch["tool_trace"].append(
                    {"tool": "shell", "args": cmd, "result": "", "status": "ok"})
                _emit(scratch, {"type": "tool_call", "name": "shell", "args": cmd})
            elif phase == "completed":
                out = str(item.get("aggregated_output") or "")
                status = "error" if item.get("status") == "failed" else "ok"
                _record_tool_result(scratch, "shell",
                                    str(item.get("command") or ""), out, status)
                _emit(scratch, {"type": "tool_result", "name": "shell",
                                "args": out, "elapsed": 0})
        elif it == "mcp_tool_call":
            name = f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}"
            if phase == "started":
                call_args = str(item.get("arguments") or "")
                scratch["tool_trace"].append(
                    {"tool": name, "args": call_args, "result": "", "status": "ok"})
                _emit(scratch, {"type": "tool_call", "name": name, "args": call_args})
            elif phase == "completed":
                out = str(item.get("result") or item.get("error") or "")
                _record_tool_result(scratch, name,
                                    str(item.get("arguments") or ""), out,
                                    "error" if item.get("error") else "ok")
                _emit(scratch, {"type": "tool_result", "name": name,
                                "args": out, "elapsed": 0})
        elif it == "file_change":
            # Structured "which files changed" — codex-only, no claude
            # equivalent. No separate result event exists, so emit one to
            # avoid a forever-running trace row on clients.
            if phase == "completed":
                changes = json.dumps(item.get("changes") or [], ensure_ascii=False)
                scratch["tool_trace"].append(
                    {"tool": "file_change", "args": changes, "result": "", "status": "ok"})
                _emit(scratch, {"type": "tool_call", "name": "file_change",
                                "args": changes})
                _emit(scratch, {"type": "tool_result", "name": "file_change",
                                "args": "", "elapsed": 0})

    def _build_result(self, scratch: dict, t0: float,
                      is_interrupted_fn) -> ExternalAgentResult:
        duration = round(time.monotonic() - t0, 2)
        if scratch["done"]:
            reason = scratch["exit_reason"] or "completed"
        elif is_interrupted_fn():
            reason = "interrupted"   # agent.interrupt → proc killed → EOF
        else:
            reason = "failed"
            scratch["error"] = scratch["error"] or "codex 进程意外退出"
        return ExternalAgentResult(
            final_text=scratch["final_text"],
            exit_reason=reason,
            tool_trace=scratch["tool_trace"],
            duration_seconds=duration,
            error=scratch["error"],
            session_id=scratch["thread_id"],
        )


# Claude driver — ported + hardened from an earlier ClaudeRunner.

class _ClaudeSession:
    """One long-lived claude child process per session_key."""

    __slots__ = (
        "session_key", "spec", "proc", "session_id", "ready", "stopping",
        "ready_timer", "idle_timer", "stderr_buf", "current_turn", "_lock",
    )

    def __init__(self, session_key: str, spec: TurnSpec):
        self.session_key = session_key
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = _resume_ids.get(("claude", session_key))
        self.ready = threading.Event()
        self.stopping = False
        self.ready_timer: Optional[threading.Timer] = None
        self.idle_timer: Optional[threading.Timer] = None
        self.stderr_buf: List[str] = []
        self.current_turn: Optional[dict] = None
        self._lock = threading.Lock()  # guards current_turn/stopping vs timers


class _TreeKillHandle:
    """Duck-types Popen for register_subprocess. kill_subprocesses() calls
    ``.kill()`` on whatever is registered; a bare proc.kill() would only kill
    the cmd.exe wrapper — claude.exe survives as a grandchild, still holds the
    stdout pipe, and the read loop never sees EOF. kill_tree takes the whole
    tree down."""

    def __init__(self, pid: int):
        self.pid = pid

    def kill(self) -> None:
        kill_tree(self.pid)


class ClaudeDriver(_SweptDriver):
    """Drives the local ``claude`` CLI as one long-lived child per session."""

    def __init__(self):
        super().__init__()
        self._sessions: Dict[str, _ClaudeSession] = {}
        self._map_lock = threading.Lock()

    def run_turn(self, session_key: str, prompt: str, spec: TurnSpec,
                 on_event: Callable[[dict], None]) -> ExternalAgentResult:
        self._maybe_sweep()
        # Lazy import keeps this module importable without the tools package
        # (and is_interrupted resolves the agent bound around tool dispatch).
        from tools.interrupt import is_interrupted, register_subprocess, unregister_subprocess

        t0 = time.monotonic()

        # Resolve / spawn the session (reuse warm, respawn dead).
        with self._map_lock:
            s = self._sessions.get(session_key)
            if s and (s.proc is None or s.proc.poll() is not None or s.stopping):
                self._sessions.pop(session_key, None)
                s = None
            if s is None:
                s = self._spawn(session_key, spec)
                if s is None:
                    return ExternalAgentResult(
                        exit_reason="failed",
                        error="无法启动 claude 子进程（见 external-agent.log）",
                        duration_seconds=round(time.monotonic() - t0, 2),
                    )
                self._sessions[session_key] = s
            reuse = s

        s = reuse
        self._cancel_idle(s)

        # Attach the proc to the agent bound around tool dispatch so
        # agent.interrupt → kill_subprocesses unblocks the read loop. Done
        # here, NOT in _spawn: the import above is function-local (invisible
        # to _spawn's module-global lookup — a LOAD_GLOBAL there raises
        # NameError, which _spawn's except swallowed, so /stop never killed
        # anything), and every turn must re-attach anyway: a warm session was
        # unregistered by the previous turn's exit.
        kill_handle = _TreeKillHandle(s.proc.pid)
        register_subprocess(kill_handle)

        scratch = {
            "tool_blocks": {},      # content_block index → {name, json}
            "tool_use_names": {},   # tool_use id → name
            "streamed_text": False,
            "done": False,
            "final_text": "",
            "exit_reason": None,
            "error": None,
            "tool_trace": [],
            "on_event": on_event,
        }
        with s._lock:
            s.current_turn = scratch

        # Write the turn IMMEDIATELY — even on a fresh spawn. stream-json claude
        # does not boot (nor emit system/init) until it receives the first stdin
        # line; buffering it "until ready" deadlocks. Writing IS the boot trigger.
        line = json.dumps(
            {"type": "user", "message": {"role": "user", "content": prompt}}
        ) + "\n"
        if spec.debug:
            _log_traffic(f">>> PROMPT session={session_key}\n{prompt}")
        try:
            s.proc.stdin.write(line.encode("utf-8"))
            s.proc.stdin.flush()
        except Exception as e:
            scratch["done"] = True
            scratch["exit_reason"] = "failed"
            scratch["error"] = f"写入 claude stdin 失败：{e}"

        # Read stdout until this turn's result frame (or proc death).
        if not scratch["done"]:
            try:
                while True:
                    raw = s.proc.stdout.readline()
                    if not raw:
                        break  # EOF — proc died (interrupt / crash / teardown)
                    text = raw.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    if spec.debug:
                        _log_traffic(f"<<< {text}")
                    self._handle_line(s, scratch, text)
                    if scratch["done"]:
                        break
            except Exception as e:
                scratch["error"] = scratch["error"] or f"读取 claude stdout 失败：{e}"

        result = self._build_result(s, scratch, t0, is_interrupted)

        # Cleanup: unregister the proc from the agent's interrupt list; reap the
        # session if the proc is dead, else arm idle TTL for reuse.
        unregister_subprocess(kill_handle)
        with s._lock:
            s.current_turn = None
        if s.proc is None or s.proc.poll() is not None:
            self._teardown(s)
        elif result.exit_reason == "completed":
            self._schedule_idle_reap(s)
        return result

    def interrupt(self, session_key: str) -> None:
        with self._map_lock:
            s = self._sessions.get(session_key)
        if s:
            self._halt(s)

    def dispose(self, session_key: str) -> None:
        with self._map_lock:
            s = self._sessions.get(session_key)
        if s:
            self._halt(s)

    def _spawn(self, session_key: str, spec: TurnSpec) -> Optional[_ClaudeSession]:
        bin_resolved = _resolve_bin(spec.bin or "claude")

        args = [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", spec.permission_mode,
        ]
        if spec.model:
            args += ["--model", str(spec.model)]
        if spec.extra_args:
            args += [str(a) for a in spec.extra_args]
        # Cold path: known session_id → respawn WITH --resume (continue the prior
        # session whose process died). First-ever spawn has no id → capture it.
        resume_id = _resume_ids.get(("claude", session_key))
        if resume_id:
            args += ["--resume", resume_id]

        cwd = spec.cwd or str(Path.home())

        # Safe baseline env + explicit anthropic creds only.
        user_env: dict = {}
        if spec.llm.get("api_key"):
            user_env["ANTHROPIC_API_KEY"] = spec.llm["api_key"]
        if spec.llm.get("base_url"):
            user_env["ANTHROPIC_BASE_URL"] = spec.llm["base_url"]
        # Output cap (external_agents.claude.max_tokens); without it claude
        # applies per-model internal caps for unknown/custom models.
        if spec.llm.get("max_tokens"):
            user_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(spec.llm["max_tokens"])
        # 中文 Windows 上 python 子进程 stdio 默认 GBK，claude 按 UTF-8 捕获
        # → 乱码（字节在 claude 解码时就丢了，下游无法恢复）。强制 python 系
        # UTF-8 从源头修掉这一类；GBK 输出的内部 CLI 靠 prompt 指令的
        # iconv 兜底（见 external_agent_tool 的 Windows 编码指令）。
        user_env.setdefault("PYTHONUTF8", "1")
        user_env.setdefault("PYTHONIOENCODING", "utf-8")
        env = build_safe_env(user_env)

        try:
            proc = _spawn_cli(bin_resolved, args, cwd, env)
        except Exception as e:
            _log_event(f"spawn FAILED bin={bin_resolved} err={e}")
            return None

        s = _ClaudeSession(session_key, spec)
        s.proc = proc
        # Drain stderr (undrained → claude blocks on a full pipe); the tail
        # feeds the ready-timeout error message.
        _start_stderr_drain(proc, s.stderr_buf, f"claude-{session_key}")

        # Ready gate: no system/init within READY_TIMEOUT_S → stuck on init.
        ready_timer = threading.Timer(READY_TIMEOUT_S, self._on_ready_timeout,
                                      args=(session_key,))
        ready_timer.daemon = True
        s.ready_timer = ready_timer
        ready_timer.start()

        _add_pid({"pid": proc.pid, "engine": "claude", "sessionId": resume_id, "cwd": cwd})
        _log_event(
            f"spawn pid={proc.pid} bin={bin_resolved} cwd={cwd} "
            f"resume={resume_id or 'none'} model={spec.model or '?'} "
            f"perm={spec.permission_mode}"
        )
        # Full argv for debugging (flags only — creds go via env, never argv).
        _log_event(f"  cmd: {' '.join(_routed_argv(bin_resolved, args))}")
        return s

    def _handle_line(self, s: _ClaudeSession, scratch: dict, text: str) -> None:
        try:
            obj = json.loads(text)
        except Exception:
            return  # not JSON (claude may print a banner line first)
        if not isinstance(obj, dict):
            return

        # Capture session id wherever it appears (init + every result).
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid:
            s.session_id = sid
            _resume_ids[("claude", s.session_key)] = sid

        if obj.get("type") == "system" and obj.get("subtype") == "init":
            s.ready.set()
            if s.ready_timer:
                s.ready_timer.cancel()
                s.ready_timer = None
            return

        t = obj.get("type")
        if t == "stream_event":
            self._on_stream_event(scratch, obj.get("event"))
        elif t == "user":
            self._on_user_message(scratch, obj)
        elif t == "result":
            self._on_result(scratch, obj)

    def _on_stream_event(self, scratch: dict, ev) -> None:
        if not isinstance(ev, dict):
            return
        et = ev.get("type")
        if et == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use" and isinstance(ev.get("index"), int):
                scratch["tool_blocks"][ev["index"]] = {
                    "name": cb.get("name", "tool"), "json": ""}
                if cb.get("id"):
                    scratch["tool_use_names"][cb["id"]] = cb.get("name", "tool")
            return
        if et == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and isinstance(d.get("text"), str):
                scratch["streamed_text"] = True
                _emit(scratch, {"type": "text_delta", "text": d["text"]})
            elif dt == "thinking_delta" and isinstance(d.get("thinking"), str):
                _emit(scratch, {"type": "thought_delta", "text": d["thinking"]})
            elif (dt == "input_json_delta" and isinstance(ev.get("index"), int)
                  and isinstance(d.get("partial_json"), str)):
                blk = scratch["tool_blocks"].get(ev["index"])
                if blk:
                    blk["json"] += d["partial_json"]
            return
        if et == "content_block_stop" and isinstance(ev.get("index"), int):
            blk = scratch["tool_blocks"].pop(ev["index"], None)
            if blk:
                scratch["tool_trace"].append(
                    {"tool": blk["name"], "args": blk["json"],
                     "result": "", "status": "ok"})
                _emit(scratch, {"type": "tool_call", "name": blk["name"],
                                "args": blk["json"]})

    def _on_user_message(self, scratch: dict, obj: dict) -> None:
        raw = (obj.get("message") or {}).get("content")
        if raw is None:
            raw = obj.get("content")
        if not isinstance(raw, list):
            return
        for block in raw:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            name = scratch["tool_use_names"].get(block.get("tool_use_id"), "tool")
            content = block.get("content")
            content = content if isinstance(content, str) else ""
            _update_trace_result(scratch, name, content)
            _emit(scratch, {"type": "tool_result", "name": name,
                            "args": content, "elapsed": 0})

    def _on_result(self, scratch: dict, obj: dict) -> None:
        subtype = obj.get("subtype")
        is_error = bool(subtype) and subtype != "success"
        text = obj.get("result") if isinstance(obj.get("result"), str) else ""
        if is_error:
            msg = text or f"claude 错误：{subtype}"
            _finalize(scratch, exit_reason="failed", error=msg,
                      emit_kind="error", emit_msg=msg)
            return
        # Safety net: if no text streamed, surface the result text now.
        if not scratch["streamed_text"] and text:
            scratch["streamed_text"] = True
            _emit(scratch, {"type": "text_delta", "text": text})
        _finalize(scratch, exit_reason="completed", final_text=text)

    def _build_result(self, s: _ClaudeSession, scratch: dict, t0: float,
                      is_interrupted_fn) -> ExternalAgentResult:
        duration = round(time.monotonic() - t0, 2)
        if scratch["done"]:
            reason = scratch["exit_reason"] or "completed"
        else:
            # Proc died without a result frame.
            if s.stopping and scratch["error"]:
                reason = "failed"        # our ready-timeout / teardown w/ error
            elif s.stopping:
                reason = "interrupted"   # our teardown (halt/idle)
            elif is_interrupted_fn():
                reason = "interrupted"   # agent.interrupt → proc killed
            else:
                reason = "failed"
                scratch["error"] = scratch["error"] or "claude 进程意外退出"
        return ExternalAgentResult(
            final_text=scratch["final_text"],
            exit_reason=reason,
            tool_trace=scratch["tool_trace"],
            duration_seconds=duration,
            error=scratch["error"],
            session_id=s.session_id,
        )

    def _on_ready_timeout(self, session_key: str) -> None:
        with self._map_lock:
            s = self._sessions.get(session_key)
        if not s:
            return
        with s._lock:
            if s.ready.is_set() or s.stopping:
                return
            s.stopping = True
            turn = s.current_turn
        tail = "".join(s.stderr_buf).strip()[-800:]
        msg = ("claude 启动超时：45s 内未完成 system/init" +
               (f"\n\nstderr:\n{tail}" if tail
                else "(stderr 无输出；可能内部网关不可达或 claude 卡初始化)"))
        _log_event("ready timeout")
        if turn:
            turn["error"] = msg
            _emit(turn, {"type": "error", "message": msg})
        self._teardown(s)

    def _halt(self, s: _ClaudeSession) -> None:
        with s._lock:
            if s.stopping:
                return
            s.stopping = True
        self._teardown(s)

    def _teardown(self, s: _ClaudeSession) -> None:
        with s._lock:
            s.stopping = True
        self._cancel_ready(s)
        self._cancel_idle(s)
        if s.proc:
            kill_tree(s.proc.pid)
            _remove_pid(s.proc.pid)
            try:
                s.proc.stdin.close()
            except Exception:
                pass
            try:
                s.proc.stdout.close()
            except Exception:
                pass
        with self._map_lock:
            if self._sessions.get(s.session_key) is s:
                self._sessions.pop(s.session_key, None)

    def _schedule_idle_reap(self, s: _ClaudeSession) -> None:
        self._cancel_idle(s)

        def reap():
            with self._map_lock:
                if self._sessions.get(s.session_key) is not s:
                    return  # replaced/removed
            with s._lock:
                if s.current_turn or s.stopping:
                    return
                s.stopping = True
            _log_event("idle reap")
            self._teardown(s)

        timer = threading.Timer(IDLE_TTL_S, reap)
        timer.daemon = True
        s.idle_timer = timer
        timer.start()

    def _cancel_idle(self, s: _ClaudeSession) -> None:
        if s.idle_timer:
            s.idle_timer.cancel()
            s.idle_timer = None

    def _cancel_ready(self, s: _ClaudeSession) -> None:
        if s.ready_timer:
            s.ready_timer.cancel()
            s.ready_timer = None

    def _maybe_sweep(self) -> None:
        if self._swept:
            return
        self._swept = True
        try:
            sweep_orphans()
        except Exception:
            logger.debug("orphan sweep failed", exc_info=True)


def sweep_orphans() -> None:
    """Kill engine child processes left over from an abnormal xihe exit.

    Run once per process lifetime (lazy on first run_turn of either driver).
    Dual validation: process alive + the entry's engine fingerprint, to
    defend against pid reuse. If the fingerprint can't be read (wmic
    blocked), degrade to trusting the PID file alone. Pre-engine pid-file
    entries carry no engine field → treated as claude.
    """
    entries = _read_pids()
    if not entries:
        return
    killed = 0
    for e in entries:
        pid = e.get("pid")
        if not pid:
            continue
        engine = e.get("engine") or "claude"
        if not process_alive(pid):
            continue
        cmd = list_process_command_line(pid)
        fp = _FINGERPRINTS.get(engine)
        if cmd is not None and fp and fp not in cmd:
            continue
        kill_tree(pid)
        killed += 1
    _write_pids([])
    if killed:
        _log_event(f"[sweep] killed {killed} orphan external-agent process(es)")


def _emit(scratch: dict, event: dict) -> None:
    cb = scratch.get("on_event")
    if cb:
        try:
            cb(event)
        except Exception:
            # never break the read loop over a forwarding callback, but leave
            # a trace — a broken forwarder means a black-boxed claude turn
            logger.debug("on_event forward failed", exc_info=True)


def _update_trace_result(scratch: dict, name: str, content: str,
                         status: Optional[str] = None) -> bool:
    for entry in reversed(scratch["tool_trace"]):
        if entry.get("tool") == name and entry.get("result") == "":
            entry["result"] = content
            if status is None:
                # Engines without an explicit verdict (claude): sniff the head.
                status = "error" if "error" in content[:80].lower() else "ok"
            entry["status"] = status
            return True
    return False


def _record_tool_result(scratch: dict, name: str, args: str, out: str,
                        status: str) -> None:
    """codex completed phase: fill the started entry, or record one — codex
    can emit completed without a preceding started line (banner-skips), and
    the tool must not silently vanish from the trace."""
    if not _update_trace_result(scratch, name, out, status=status):
        scratch["tool_trace"].append(
            {"tool": name, "args": args, "result": out, "status": status})


def _finalize(scratch: dict, exit_reason: str, final_text: str = "",
              error: Optional[str] = None, emit_kind: Optional[str] = None,
              emit_msg: Optional[str] = None) -> None:
    if scratch["done"]:
        return
    scratch["done"] = True
    scratch["exit_reason"] = exit_reason
    scratch["final_text"] = final_text
    scratch["error"] = error
    if emit_kind == "error":
        _emit(scratch, {"type": "error", "message": emit_msg or error or "claude error"})
    else:
        _emit(scratch, {"type": "complete", "text": final_text})


_claude_driver: Optional[ClaudeDriver] = None
_codex_driver: Optional[CodexDriver] = None


def get_driver(engine: str, config: Optional[dict] = None) -> ExternalAgentDriver:
    """Return the process-wide driver singleton for *engine*."""
    global _claude_driver, _codex_driver
    if engine == "claude":
        if _claude_driver is None:
            _claude_driver = ClaudeDriver()
        return _claude_driver
    if engine == "codex":
        if _codex_driver is None:
            _codex_driver = CodexDriver()
        return _codex_driver
    raise ValueError(f"未知的外部 agent 引擎：{engine}")
