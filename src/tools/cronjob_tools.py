"""Cronjob tool — scheduled task management.

Design based on Hermes cron architecture:
- `deliver` parameter: "local" (no delivery), "origin" (send to creating chat),
  "platform:chat_id" (send to specific target)
- `[SILENT]` marker: agent can suppress delivery when nothing to report
- Schedule types: one-shot ("30m", "2h"), recurring ("every 30m"), cron ("0 9 * * *")
- Output saved to files for audit
- Origin tracking: stores platform/chat_id where job was created
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

from core.config import AGENT_HOME
_CRON_DIR = AGENT_HOME / "cron"
_JOBS_FILE = _CRON_DIR / "jobs.json"
_OUTPUT_DIR = _CRON_DIR / "output"
# Reusable script home (mirrors Hermes ~/.hermes/scripts/). A cron job's
# `script` field resolves a name against here, then the project ./scripts/.
_SCRIPTS_DIR = AGENT_HOME / "scripts"

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_running = False
_agent = None
_platform_adapter = None
_last_tick: float = 0  # Timestamp of last scheduler tick for health checks

# [SILENT] marker — agent returns this to suppress delivery
SILENT_MARKER = "[SILENT]"

# Job execution timeout (seconds) — prevents agent.chat() from hanging forever
JOB_TIMEOUT = 300

_active_sessions: dict[str, str] = {}  # job_id -> session_key
_active_agents: dict[str, object] = {}  # job_id -> running agent (cancel 可直接打断)
_cancel_flags: set[str] = set()  # session_keys that should be cancelled


def _interrupt_job_run(job_id: str) -> None:
    """打断该任务正在运行的一次执行——审批等待中也能立刻解除，不必等超时。"""
    agent = _active_agents.get(job_id)
    if agent is not None:
        try:
            agent.interrupt()
        except Exception:
            logger.warning("Interrupt job run failed (job=%s)", job_id,
                           exc_info=True)


def is_session_cancelled(session_key: str) -> bool:
    """Check if a session has been marked for cancellation."""
    return session_key in _cancel_flags


def clear_cancel_flag(session_key: str):
    """Clear the cancellation flag for a session."""
    _cancel_flags.discard(session_key)


def set_platform_adapter(adapter):
    global _platform_adapter
    _platform_adapter = adapter


def _inject_agent(agent):
    """Inject agent instance for scheduler execution.

    Called by the cronjob handler when parent_agent is available via kwargs,
    and stored for the background scheduler thread which runs outside dispatch.
    """
    global _agent
    _agent = agent


# Agent factory: a zero-arg callable returning a fresh XiheAgent. Preferred
# over the single _agent because each job gets its own instance (concurrency-
# safe). Set by the gateway at startup so cron jobs run autonomously without
# waiting for a chat-side cronjob tool call.
_agent_factory = None


def set_agent_factory(factory):
    """Register an agent factory (e.g. SharedContext.create_agent).

    With a factory set, the scheduler builds a fresh agent per job and does not
    need _agent (which is only populated when the cronjob tool is used in chat).
    """
    global _agent_factory
    _agent_factory = factory


def _get_agent():
    """Return an agent for running a job: fresh from factory if available,
    else the shared _agent, else None."""
    if _agent_factory is not None:
        try:
            return _agent_factory()
        except Exception as e:
            logger.warning("cron agent factory failed, falling back to shared _agent: %s", e)
    return _agent


def _has_agent() -> bool:
    """True if the scheduler can run jobs (factory or shared agent available)."""
    return _agent_factory is not None or _agent is not None


def _ensure_dirs():
    _CRON_DIR.mkdir(parents=True, exist_ok=True)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_jobs():
    global _jobs
    _ensure_dirs()
    if _JOBS_FILE.exists():
        try:
            data = json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
            _jobs = {j["id"]: j for j in data.get("jobs", [])}
        except Exception:
            _jobs = {}
    else:
        _jobs = {}


def _save_jobs():
    _ensure_dirs()
    jobs_list = list(_jobs.values())
    _JOBS_FILE.write_text(
        json.dumps({"jobs": jobs_list, "updated_at": datetime.now().isoformat()},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


_KEEP_OUTPUTS = 20


def _prune_outputs(job_dir: Path, pattern: str, keep: int = _KEEP_OUTPUTS) -> None:
    """Keep only the newest ``keep`` timestamped outputs — a 30m job writes
    48 files/day and nothing else ever cleaned them up. Timestamps sort
    lexicographically, so a name sort is a time sort."""
    try:
        files = sorted(job_dir.glob(pattern))
        for old in (files[:-keep] if len(files) > keep else ()):
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _save_job_output(job_id: str, output: str):
    _ensure_dirs()
    job_dir = _OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    (job_dir / f"{timestamp}.md").write_text(output, encoding="utf-8")
    _prune_outputs(job_dir, "*.md")


def _save_script_output(job_id: str, output: str):
    """Persist a job's script stdout for audit + context_from chaining."""
    _ensure_dirs()
    job_dir = _OUTPUT_DIR / job_id / "scripts"
    job_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    (job_dir / f"{timestamp}.txt").write_text(output, encoding="utf-8")
    _prune_outputs(job_dir, "*.txt")


def _resolve_script(name: str) -> str | None:
    """Resolve a cron job `script` reference to an absolute path.

    Order: absolute path → CWD-relative → ${AGENT_HOME}/scripts/<name> →
    <project root>/scripts/<name>. Returns None if nothing exists.
    """
    if not name:
        return None
    p = Path(name)
    if p.is_absolute():
        return str(p) if p.exists() else None
    # Relative with separators: resolve against CWD.
    if os.sep in name or "/" in name:
        return str(p) if p.exists() else None
    # Bare filename: check the two script homes.
    for cand in (_SCRIPTS_DIR / name, Path.cwd() / "scripts" / name):
        if cand.exists():
            return str(cand)
    return None


def _run_job_script(script_ref: str, timeout: int) -> tuple[str, bool]:
    """Run a job's script. Returns (combined_output, wake_agent).

    wake gate: if the last non-empty stdout line is the JSON object
    `{"wakeAgent": false}`, the caller should skip the agent this tick and
    wake_agent is returned False (and that line is stripped from the output).
    On timeout / non-zero exit, stderr is folded into the output and
    wake_agent is False so the caller can treat it as a no-op/error.
    """
    path = _resolve_script(script_ref)
    if not path:
        return f"[script not found: {script_ref}]", False

    # Dispatch by extension so .py / .ps1 / .bat all work reliably on Windows
    # (a blanket shell=True mis-launches .py under cmd.exe). Unix falls through
    # to shell=True which honors shebangs.
    import sys as _sys
    low = path.lower()
    if low.endswith(".py"):
        cmd = [_sys.executable, path]
        use_shell = False
    elif low.endswith(".ps1"):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
        use_shell = False
    else:
        cmd = path
        use_shell = True

    # Inject AGENT_HOME so scripts can resolve the data root without hardcoding
    # ${AGENT_HOME} (follows agent_home config: env var > project yaml > default).
    from core.config import AGENT_HOME
    script_env = {**os.environ, "AGENT_HOME": str(AGENT_HOME)}
    try:
        proc = subprocess.run(
            cmd, shell=use_shell, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace", env=script_env,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = f"[script timed out after {timeout}s]"
        rc = None
    except Exception as e:
        return f"[script failed to start: {e}]", False

    # Parse wake gate from the last non-empty stdout line.
    wake = True
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        try:
            obj = json.loads(lines[-1].strip())
            if isinstance(obj, dict) and obj.get("wakeAgent") is False:
                wake = False
                lines.pop()
                out = "\n".join(lines)
        except (json.JSONDecodeError, ValueError):
            pass

    combined = out
    if err.strip():
        combined = (combined + ("\n" if combined else "") + f"[stderr]\n{err}").rstrip()
    # Non-zero exit ⇒ don't wake the agent on garbage.
    if rc not in (0, None):
        wake = False
    return combined, wake


def _parse_duration(s: str) -> int:
    """Parse duration string into seconds. E.g., '30m' -> 1800, '2h' -> 7200."""
    s = s.strip().lower()
    m = re.match(r'^(\d+)\s*(s|sec|m|min|h|hr|d|day)', s)
    if not m:
        raise ValueError(f"Invalid duration: '{s}'")
    value = int(m.group(1))
    unit = m.group(2)[0]
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return value * multipliers[unit]


def parse_schedule(schedule: str) -> dict:
    """Parse schedule string into structured format.

    Returns dict with:
        - kind: "once" | "interval" | "cron"
        - For "once": "run_at" (ISO timestamp)
        - For "interval": "seconds" (int)
        - For "cron": "expr" (cron expression string)
    """
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()

    if schedule_lower.startswith("every "):
        duration_str = schedule[6:].strip()
        seconds = _parse_duration(duration_str)
        return {
            "kind": "interval",
            "seconds": seconds,
            "display": f"every {original[6:].strip()}",
        }

    parts = schedule.split()
    if len(parts) >= 5 and all(re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]):
        return {
            "kind": "cron",
            "expr": schedule,
            "display": schedule,
        }

    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        try:
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}",
            }
        except ValueError:
            pass

    try:
        seconds = _parse_duration(schedule)
        run_at = datetime.now() + timedelta(seconds=seconds)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "seconds_offset": seconds,
            "display": f"once in {original}",
        }
    except ValueError:
        pass

    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        "  - Duration: '30m', '2h', '1d' (one-shot)\n"
        "  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        "  - Cron: '0 9 * * *' (cron expression)\n"
        "  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
    )


def _cron_parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into the set of matching ints. Supports *, N, a-b, a-b/n, */n, comma lists."""
    out: set[int] = set()
    for part in field.split(','):
        step = 1
        base = part
        if '/' in part:
            base, step_str = part.split('/', 1)
            step = int(step_str)
        if base == '*':
            start, end = lo, hi
        elif '-' in base:
            a, b = base.split('-', 1)
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = start if step > 1 else start
        if step > 1 and base != '*' and '-' not in base:
            # "N/n" — start at N, step to hi
            end = hi
        out.update(range(start, end + 1, step))
    return out


def _cron_next_run(expr: str, after: datetime) -> str:
    """Next minute strictly after `after` matching the 5-field cron expr. DOM & DOW are AND'd (classic cron OR-on-restriction not supported)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression (need 5 fields): {expr}")
    minutes = _cron_parse_field(fields[0], 0, 59)
    hours = _cron_parse_field(fields[1], 0, 23)
    doms = _cron_parse_field(fields[2], 1, 31)
    months = _cron_parse_field(fields[3], 1, 12)
    dows = _cron_parse_field(fields[4], 0, 6)  # 0=Sunday
    # Also accept 7 as Sunday
    if 7 in dows:
        dows.add(0)

    cand = (after.replace(second=0, microsecond=0) + timedelta(minutes=1))
    limit = after + timedelta(days=732)  # 2-year cap guards impossible exprs
    while cand <= limit:
        # Python weekday: Mon=0..Sun=6 → cron: Sun=0..Sat=6
        cron_dow = (cand.weekday() + 1) % 7
        if (cand.minute in minutes and cand.hour in hours
                and cand.day in doms and cand.month in months
                and cron_dow in dows):
            return cand.isoformat()
        cand += timedelta(minutes=1)
    raise ValueError(f"No cron match within 2 years for: {expr}")


def _compute_next_run(schedule: dict, last_run_at: str = None) -> str | None:
    """Compute ISO timestamp for next run. Returns None if no more runs."""
    now = datetime.now()

    if schedule["kind"] == "once":
        run_at = schedule.get("run_at")
        if not run_at:
            return None
        # Already ran?
        if last_run_at:
            return None
        return run_at

    elif schedule["kind"] == "interval":
        seconds = schedule["seconds"]
        if last_run_at:
            last = datetime.fromisoformat(last_run_at)
            return (last + timedelta(seconds=seconds)).isoformat()
        return (now + timedelta(seconds=seconds)).isoformat()

    elif schedule["kind"] == "cron":
        ref = datetime.fromisoformat(last_run_at) if last_run_at else now
        return _cron_next_run(schedule["expr"], ref)

    return None


def _create_job(args: dict, **kw) -> str:
    name = args.get("name", "")
    schedule_str = args.get("schedule", "")
    prompt = args.get("prompt", "")
    deliver = args.get("deliver", "origin")
    repeat = args.get("repeat")
    script = args.get("script", "") or ""
    no_agent = bool(args.get("no_agent", False))
    context_from = args.get("context_from", "") or ""

    if not schedule_str:
        return tool_error("schedule is required")
    # A job needs either a prompt or a script (no_agent script-only jobs have
    # no prompt; script+prompt jobs may also leave prompt to the script output).
    if not prompt and not script:
        return tool_error("either prompt or script is required")
    if no_agent and not script:
        return tool_error("no_agent=True requires a script")

    try:
        parsed_schedule = parse_schedule(schedule_str)
    except ValueError as e:
        return tool_error(str(e))

    if parsed_schedule["kind"] == "once" and repeat is None:
        repeat = 1

    # Resolve origin (where the job was created)
    context = kw.get("context", {}) or {}
    origin = None
    if context.get("chat_id") and context.get("platform"):
        origin = {
            "platform": context["platform"],
            "chat_id": context["chat_id"],
        }

    # Default delivery: origin if available, else local
    if deliver == "origin" and not origin:
        deliver = "local"

    job_name = name or (prompt[:50].strip() if prompt else f"script:{script}")
    job_id = uuid.uuid4().hex[:12]
    now_iso = datetime.now().isoformat()

    job = {
        "id": job_id,
        "name": job_name,
        "prompt": prompt,
        "script": script or None,
        "no_agent": no_agent,
        "context_from": context_from or None,
        "schedule": parsed_schedule,
        "schedule_display": parsed_schedule.get("display", schedule_str),
        "repeat": {"times": repeat, "completed": 0},
        "deliver": deliver,
        "origin": origin,
        "enabled": True,
        "state": "scheduled",
        "created_at": now_iso,
        "next_run_at": _compute_next_run(parsed_schedule),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
    }

    with _lock:
        _load_jobs()
        # Auto-replace existing jobs with the same name to prevent duplicates
        if job_name:
            old_ids = [jid for jid, j in _jobs.items() if j.get("name") == job_name]
            for old_id in old_ids:
                del _jobs[old_id]
                logger.info("Auto-replaced duplicate job '%s' (old id: %s)", job_name, old_id)
        _jobs[job_id] = job
        _save_jobs()

    parent_agent = kw.get("parent_agent")
    if parent_agent:
        _inject_agent(parent_agent)

    logger.info("Cronjob created: %s (%s, deliver=%s)", name or job_id, parsed_schedule["display"], deliver)
    return tool_result(
        success=True,
        job_id=job_id,
        name=job["name"],
        schedule=job["schedule_display"],
        deliver=deliver,
        next_run_at=job["next_run_at"],
        message=f"Cron job '{job['name']}' created. Results will be auto-delivered.",
    )


def _delete_job(args: dict) -> str:
    job_id = args.get("job_id", args.get("name", ""))
    if not job_id:
        return tool_error("job_id is required")

    with _lock:
        _load_jobs()
        # Try by ID first, then by name — delete ALL matching jobs
        deleted_ids = []
        if job_id in _jobs:
            deleted_ids.append(job_id)
        else:
            for jid, j in _jobs.items():
                if j.get("name") == job_id:
                    deleted_ids.append(jid)

        if not deleted_ids:
            return tool_error(f"Job not found: {job_id}")

        for did in deleted_ids:
            session_key = _active_sessions.get(did)
            if session_key:
                _cancel_flags.add(session_key)
                logger.info("Job %s marked for cancellation (session: %s)", did, session_key)
            _interrupt_job_run(did)
            del _jobs[did]

        _save_jobs()

    return tool_result(success=True, deleted=deleted_ids, count=len(deleted_ids))


def _list_jobs(args: dict) -> str:
    with _lock:
        _load_jobs()
        include_disabled = args.get("include_disabled", False)
        result = []
        for job_id, job in _jobs.items():
            if not include_disabled and not job.get("enabled", True):
                continue
            repeat_info = job.get("repeat", {})
            times = repeat_info.get("times")
            completed = repeat_info.get("completed", 0)
            if times is None:
                repeat_display = "forever"
            elif times == 1:
                repeat_display = "once"
            else:
                repeat_display = f"{completed}/{times}"

            result.append({
                "job_id": job_id,
                "name": job.get("name", ""),
                "schedule": job.get("schedule_display", ""),
                "repeat": repeat_display,
                "deliver": job.get("deliver", "origin"),
                "script": job.get("script"),
                "no_agent": job.get("no_agent", False),
                "context_from": job.get("context_from"),
                "enabled": job.get("enabled", True),
                "next_run_at": job.get("next_run_at"),
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
            })
    return tool_result(jobs=result, count=len(result))


def _run_job_manual(args: dict) -> str:
    """Trigger a job to run immediately (manual trigger)."""
    job_id = args.get("job_id", args.get("name", ""))
    if not job_id:
        return tool_error("job_id is required")

    with _lock:
        _load_jobs()
        job = _jobs.get(job_id)
        if not job:
            for jid, j in _jobs.items():
                if j.get("name") == job_id:
                    job_id = jid
                    job = j
                    break
        if not job:
            return tool_error(f"Job not found: {job_id}")

    with _lock:
        _jobs[job_id]["next_run_at"] = datetime.now().isoformat()
        _save_jobs()

    return tool_result(success=True, job_id=job_id, message="Job scheduled for immediate execution")


def _pause_job(args: dict) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required")
    with _lock:
        _load_jobs()
        if job_id not in _jobs:
            return tool_error(f"Job not found: {job_id}")
        _jobs[job_id]["enabled"] = False
        _jobs[job_id]["state"] = "paused"

        session_key = _active_sessions.get(job_id)
        if session_key:
            _cancel_flags.add(session_key)
            logger.info("Job %s marked for cancellation (session: %s)", job_id, session_key)
        _interrupt_job_run(job_id)

        _save_jobs()
    return tool_result(success=True, job_id=job_id)


def _resume_job(args: dict) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required")
    with _lock:
        _load_jobs()
        if job_id not in _jobs:
            return tool_error(f"Job not found: {job_id}")
        _jobs[job_id]["enabled"] = True
        _jobs[job_id]["state"] = "scheduled"
        _jobs[job_id]["next_run_at"] = _compute_next_run(
            _jobs[job_id]["schedule"], _jobs[job_id].get("last_run_at"))
        _save_jobs()
    return tool_result(success=True, job_id=job_id)


def _resolve_delivery_target(job: dict) -> dict | None:
    """Resolve where to deliver job results. Returns None for local-only jobs."""
    deliver = job.get("deliver", "origin")
    origin = job.get("origin")

    if deliver == "local":
        return None

    if deliver == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
            }
        return None

    # "platform:chat_id" format
    if ":" in deliver:
        platform_name, chat_id = deliver.split(":", 1)
        return {"platform": platform_name, "chat_id": chat_id}

    # Just platform name — send to adapter (if it matches)
    if origin and origin.get("platform") == deliver:
        return {"platform": deliver, "chat_id": str(origin["chat_id"])}

    return None


def _send_to_chat(chat_id: str, message: str) -> bool:
    """Send a message to a platform chat from a scheduler thread. Returns
    False when there is no adapter or the send fails."""
    if not chat_id or not _platform_adapter:
        return False
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _platform_adapter.send(chat_id, message), loop
            )
        else:
            asyncio.run(_platform_adapter.send(chat_id, message))
        return True
    except Exception as e:
        logger.error("Send to %s failed: %s", chat_id, e)
        return False


def _deliver_result(job: dict, content: str):
    """Deliver job result to the target. Skips if deliver=local or content is [SILENT]."""
    target = _resolve_delivery_target(job)
    if not target:
        return

    chat_id = target["chat_id"]
    if not chat_id or not _platform_adapter:
        logger.warning("Job '%s': no chat_id or adapter, result not delivered", job.get("id"))
        return

    # Check [SILENT] marker — agent may place it anywhere in the response
    if SILENT_MARKER in content:
        logger.info("Job '%s': [SILENT] — skipping delivery", job.get("id"))
        return

    task_name = job.get("name", job.get("id", "?"))
    message = f"[定时任务: {task_name}]\n{content}"[:4000]

    if _send_to_chat(chat_id, message):
        logger.info("Job '%s' delivered to %s", job.get("id"), chat_id)


def _make_approval_callbacks(job: dict, agent):
    """Build approval callbacks that surface this job's approval cards on its
    delivery chat (gateway adapter). Returns (request_cb, result_cb); both None
    when the job has no delivery channel — the agent then keeps the unattended
    behavior (ask → immediate deny instead of waiting out the timeout).
    """
    target = _resolve_delivery_target(job)
    if not target or not _platform_adapter:
        return None, None
    # 路由表按实际投递的通道登记：回复永远从这张适配器回来，deliver 里
    # 写的平台名（platform:chat_id 形式）与回复所在的通道未必一致。
    platform = str(getattr(_platform_adapter, "name", target["platform"]))
    chat_id = str(target["chat_id"])
    job_name = job.get("name", job.get("id", "?"))

    def _request(info: dict):
        card = (f"[定时任务: {job_name}] ⚠️ 需要人工确认\n"
                f"{info.get('summary', '')}\n"
                "回复 y 批准本次 / n 拒绝 / a 批准且本任务不再询问")[:4000]
        if not _send_to_chat(chat_id, card):
            # 卡片没送到就没有确认通道：抛错让 request_approval 立即拒绝，
            # 不空等超时
            raise RuntimeError(f"approval card delivery failed ({chat_id})")
        from tools._approvals import register_pending
        register_pending(
            platform, chat_id, info["id"],
            lambda approved, always, _id=info["id"]:
                agent.resolve_approval(_id, approved, always=always))

    def _result(info: dict, approved: bool, reason: str):
        from tools._approvals import unregister_pending
        unregister_pending(platform, chat_id, info.get("id"))
        verdict = "✅ 已批准" if approved else "❌ 已拒绝"
        _send_to_chat(chat_id, f"[定时任务: {job_name}] {verdict}（{reason}）")

    return _request, _result


def _get_due_jobs() -> list[dict]:
    """Get all jobs that are due to run now."""
    now = datetime.now()
    due = []
    for job_id, job in _jobs.items():
        if not job.get("enabled", True):
            continue
        next_run = job.get("next_run_at")
        if not next_run:
            continue
        try:
            next_run_dt = datetime.fromisoformat(next_run)
            if next_run_dt <= now:
                due.append(job.copy())
        except (ValueError, TypeError):
            continue
    return due


def _mark_job_run(job_id: str, success: bool, error: str = None):
    """Mark a job as having been run, update next_run_at, auto-delete if repeat limit reached."""
    try:
        with _lock:
            _load_jobs()
            if job_id not in _jobs:
                return
            job = _jobs[job_id]
            now_iso = datetime.now().isoformat()
            job["last_run_at"] = now_iso
            job["last_status"] = "ok" if success else "error"
            job["last_error"] = error if not success else None

            # Increment completed count
            repeat = job.get("repeat", {})
            if repeat:
                repeat["completed"] = repeat.get("completed", 0) + 1
                times = repeat.get("times")
                completed = repeat["completed"]
                if times is not None and times > 0 and completed >= times:
                    # Repeat limit reached — remove the job
                    del _jobs[job_id]
                    _save_jobs()
                    return

            # Recompute only if the scheduler hasn't already advanced
            # next_run_at at dispatch time (eager advance prevents
            # re-entrancy on slow jobs).
            cur_next = job.get("next_run_at")
            already_advanced = False
            if cur_next:
                try:
                    already_advanced = datetime.fromisoformat(cur_next) > datetime.now()
                except Exception:
                    pass
            if not already_advanced:
                next_run = _compute_next_run(job.get("schedule", {}), now_iso)
                if next_run is None:
                    job["enabled"] = False
                    job["state"] = "completed"
                else:
                    job["next_run_at"] = next_run
            if job.get("state") == "running":
                job["state"] = "scheduled"
            _save_jobs()
    except Exception as e:
        logger.error("_mark_job_run failed for %s: %s", job_id, e)


def _execute_job(job: dict):
    """Execute a single cron job. Runs inside its own thread from scheduler.

    Three modes (mirrors Hermes):
      - no_agent script: script stdout IS the result, zero LLM tokens.
      - script + agent:  script runs first; if it emits {"wakeAgent": false}
                         the agent is skipped (silent tick), else its stdout is
                         injected into the prompt before the agent runs.
      - prompt only:     classic path, fresh agent.chat(prompt).
    context_from, if set, prepends another job's latest output to the prompt.
    """
    job_id = job["id"]
    prompt = job.get("prompt", "") or ""
    script = job.get("script")
    no_agent = bool(job.get("no_agent", False))
    context_from = job.get("context_from")
    job_name = job.get("name", job_id)

    # 1. context_from: prepend another job's most recent output.
    if context_from:
        try:
            src_dir = _OUTPUT_DIR / context_from
            if src_dir.exists():
                outs = sorted(src_dir.glob("*.md"),
                              key=lambda f: f.stat().st_mtime, reverse=True)
                if outs:
                    latest = outs[0].read_text(encoding="utf-8").strip()[:8192]
                    prompt = (f"## Output from job '{context_from}'\n"
                              f"The following is the most recent output from a "
                              f"preceding cron job. Use it as context.\n\n"
                              f"```\n{latest}\n```\n\n{prompt}")
        except Exception as e:
            logger.warning("Cronjob %s: context_from read failed: %s", job_id, e)

    # 2. Script pre-run (no_agent OR script-feeding-prompt).
    if script:
        script_output, wake = _run_job_script(script, JOB_TIMEOUT)
        _save_script_output(job_id, script_output)
        logger.info("Cronjob %s script ran (wake=%s)", job_id, wake)

        if no_agent:
            result = script_output.strip() or "[SILENT]"
            output = (f"# Cron Job (no_agent): {job_name}\n"
                      f"**Run:** {datetime.now().isoformat()}\n**Script:** {script}\n\n{result}")
            _save_job_output(job_id, output)
            _deliver_result(job, result)
            _mark_job_run(job_id, True)
            logger.info("Cronjob executed (no_agent): %s", job_name)
            return

        if not wake:
            logger.info("Cronjob %s: wake gate -> agent skipped (silent tick)", job_id)
            _mark_job_run(job_id, True)
            return

        prompt = f"## Script Output\n```\n{script_output}\n```\n\n{prompt}"

    # 3. Agent path (prompt-only or script+prompt with wake=True).
    agent = _get_agent()
    if agent is None:
        logger.error("Cronjob %s: no agent available", job_id)
        _mark_job_run(job_id, False, "agent not available")
        return

    cron_hint = (
        "[SYSTEM: You are running as a scheduled cron job. "
        "Execute the task efficiently and respond with the final answer directly. "
        "Your final text response will be automatically delivered to the user — "
        "do NOT use send_message. "
        "If nothing new to report, respond with exactly \"[SILENT]\". "
        "Be concise — avoid unnecessary tool calls.]\n\n"
    )
    # Expand ${AGENT_HOME} in the job prompt so cron prompts reference the
    # data root without hardcoding ${AGENT_HOME} (follows agent_home config).
    from core.config import expand_agent_vars
    full_prompt = cron_hint + expand_agent_vars(prompt)

    result = None
    exc = None
    session_key = f"cron_{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 审批：记忆桶按任务名（同任务所有运行/跨进程共享"不再询问"）；有投递
    # 通道的任务把审批卡弹到目标聊天，无通道维持无人值守即拒。
    request_cb, result_cb = _make_approval_callbacks(job, agent)
    approval_kwargs = ({"approval_request_callback": request_cb,
                         "approval_result_callback": result_cb}
                       if request_cb is not None else {})

    with _lock:
        _active_sessions[job_id] = session_key
        _active_agents[job_id] = agent

    try:
        from core.session import SessionSource
        cron_source = SessionSource(platform="cron", chat_id=session_key, chat_type="dm")
        result = agent.chat(
            source=cron_source,
            user_message=full_prompt,
            approval_key=f"cron_job:{job_name}",
            **approval_kwargs,
        )
    except Exception as e:
        exc = e
    finally:
        with _lock:
            _active_sessions.pop(job_id, None)
            _active_agents.pop(job_id, None)
            _cancel_flags.discard(session_key)

    if exc is not None:
        logger.error("Cronjob %s failed: %s", job_id, exc)
        _mark_job_run(job_id, False, str(exc))
        return

    if result and "cancelled by user" in result.lower():
        logger.info("Cronjob %s was cancelled", job_id)
        _mark_job_run(job_id, False, "cancelled by user")
        return

    if not result or getattr(agent, "_last_exit_reason", None) == "max_iterations":
        logger.warning("Cronjob %s hit max iterations or got empty result", job_id)
        _mark_job_run(job_id, False, "agent hit max iterations without producing a useful response")
        return

    output = f"# Cron Job: {job_name}\n**Run:** {datetime.now().isoformat()}\n\n{result}"
    _save_job_output(job_id, output)

    _deliver_result(job, result)

    _mark_job_run(job_id, True)
    logger.info("Cronjob executed: %s", job_name)


def _scheduler_loop():
    global _running, _last_tick
    while _running:
        try:
            time.sleep(60)
            _last_tick = time.monotonic()
            if not _has_agent():
                continue

            with _lock:
                _load_jobs()
                due_jobs = _get_due_jobs()
                # Eagerly advance next_run_at BEFORE spawning threads. Otherwise
                # a slow job (e.g. keepalive that takes minutes) is still in the
                # past on the next 60s tick → gets re-fired concurrently. This
                # is "at-most-once per schedule" dispatch; _mark_job_run still
                # records last_run/status at completion.
                now_iso = datetime.now().isoformat()
                for j in due_jobs:
                    job_id = j["id"]
                    src = _jobs.get(job_id)
                    if not src:
                        continue
                    nxt = _compute_next_run(src.get("schedule", {}), now_iso)
                    if nxt is None:
                        src["enabled"] = False
                        src["state"] = "completed"
                    else:
                        src["next_run_at"] = nxt
                        src["state"] = "running"
                if due_jobs:
                    _save_jobs()

            # Execute each job in its own thread — parallel, non-blocking
            for job in due_jobs:
                try:
                    t = threading.Thread(
                        target=_execute_job, args=(job,),
                        daemon=True,
                        name=f"cron-{job['id']}",
                    )
                    t.start()
                    logger.info("Cronjob %s started in parallel thread", job["id"])
                except Exception as e:
                    logger.error("Cronjob %s start error: %s", job.get("id"), e, exc_info=True)
                    try:
                        _mark_job_run(job["id"], False, str(e))
                    except Exception:
                        pass
        except Exception as e:
            # Outer catch — keep the scheduler alive no matter what
            logger.error("Scheduler loop error: %s", e, exc_info=True)


def scheduler_health() -> dict:
    """Return scheduler health status. Useful for diagnostics."""
    now = time.monotonic()
    alive = _running and (now - _last_tick < 180) if _last_tick else _running
    return {
        "running": _running,
        "alive": alive,
        "last_tick_ago": round(now - _last_tick, 1) if _last_tick else None,
        "agent_set": _agent is not None,
        "adapter_set": _platform_adapter is not None,
    }


def start_scheduler():
    global _running
    if _running:
        return
    _running = True
    _load_jobs()
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    logger.info("Cron scheduler started")


def _check_cronjob() -> bool:
    # Auto-restart scheduler if thread died
    if not _running:
        start_scheduler()
    return True


def _cronjob(args: dict, **kw) -> str:
    parent_agent = kw.get("parent_agent")
    if parent_agent:
        _inject_agent(parent_agent)

    action = args.get("action", "list")
    if action == "create":
        return _create_job(args, **kw)
    elif action == "delete" or action == "remove":
        return _delete_job(args)
    elif action == "list":
        return _list_jobs(args)
    elif action in ("run", "trigger"):
        return _run_job_manual(args)
    elif action == "pause":
        return _pause_job(args)
    elif action == "resume":
        return _resume_job(args)
    else:
        return tool_error(f"Unknown action: {action}. Use: create, list, delete, run, pause, resume")


registry.register(
    name="cronjob",
    toolset="scheduler",
    subagent_blocked=True,
    schema={
        "type": "function",
        "function": {
            "name": "cronjob",
            "description": (
                "Manage scheduled cron jobs. A job runs in a fresh, stateless session each tick — "
                "prompts must be self-contained (no memory of prior runs; persist state to files "
                "yourself if needed). The agent's final response is auto-delivered to the target; "
                "respond with [SILENT] to suppress delivery.\n\n"
                "Three job shapes:\n"
                "- prompt only: classic — runs one agent.chat(prompt).\n"
                "- script + no_agent: deterministic script is the whole job, ZERO LLM tokens. "
                "Script stdout is the delivered result. Use for watchdogs / data sync / cleanup.\n"
                "- script (+ prompt): script runs first; its stdout is injected into the prompt as "
                "'## Script Output' before the agent runs. If the script's last stdout line is exactly "
                "{\"wakeAgent\": false}, the agent is SKIPPED that tick (silent, no tokens) — use this "
                "so the agent only wakes when the script finds something new.\n\n"
                "context_from: set to another job's id to prepend that job's most recent output to this "
                "job's prompt (chain jobs into a pipeline A -> B -> C).\n\n"
                "Schedule formats:\n"
                "- '30m', '2h', '1d' — one-shot (runs once after the delay)\n"
                "- 'every 30m', 'every 2h' — recurring interval\n"
                "- '0 9 * * *' — cron expression\n"
                "- '2026-06-01T09:00:00' — one-shot at specific time\n\n"
                "Delivery targets:\n"
                "- 'origin' (default) — send to the chat where the job was created\n"
                "- 'local' — no delivery, just save output locally (for background tasks)\n"
                "- 'platform:chat_id' — send to a specific target (e.g. 'wecom:chat123')"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "delete", "run", "pause", "resume"],
                        "description": "Action to perform (default: list)",
                    },
                    "name": {"type": "string", "description": "Job name (optional)"},
                    "schedule": {"type": "string", "description": "Schedule (e.g., '30m', 'every 2h', '0 9 * * *')"},
                    "prompt": {"type": "string", "description": "Self-contained prompt for the job. Optional if script is given."},
                    "script": {
                        "type": "string",
                        "description": (
                            "Script name or path. Resolved against ${AGENT_HOME}/scripts/, then the "
                            "project ./scripts/, or an absolute/CWD-relative path. With no_agent=true the "
                            "script IS the job (0 tokens, stdout delivered). Without no_agent the script "
                            "runs first and its stdout is injected into the prompt; emit a final line "
                            "{\"wakeAgent\": false} to skip the agent this tick."
                        ),
                    },
                    "no_agent": {
                        "type": "boolean",
                        "description": "Pure script mode — never call the LLM. Requires script. Script stdout is the result.",
                    },
                    "context_from": {
                        "type": "string",
                        "description": "Another job's id; its most recent saved output is prepended to this job's prompt (pipeline chaining).",
                    },
                    "deliver": {
                        "type": "string",
                        "description": "Delivery target: 'origin' (default), 'local', or 'platform:chat_id'",
                    },
                    "repeat": {"type": "integer", "description": "Repeat count (omit for default: once for one-shot, forever for recurring)"},
                    "job_id": {"type": "string", "description": "Job ID (for delete/run/pause/resume)"},
                },
                "required": ["action"],
            },
        },
    },
    handler=lambda args, **kw: _cronjob(args, **kw),
    check_fn=_check_cronjob,
)
