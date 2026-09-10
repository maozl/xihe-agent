"""Cronjob tool - agent-facing scheduled-task management.

Thin tool surface over the core.services.scheduler subsystem: handlers
translate tool args into service calls. The subsystem (jobs store, loop,
lock, execution, delivery) lives in core/services/scheduler.py - tool
modules hold only tool faces.
"""

import logging
import uuid
from datetime import datetime

from tools import registry, tool_error, tool_result
from core.support.process_lock import CrossProcessLock

logger = logging.getLogger(__name__)

from core.services import scheduler
from core.services.scheduler import (  # noqa: F401 - legacy import paths
    SILENT_MARKER,
    _JOBS_LOCK_FILE,
    _compute_next_run,
    _inject_agent,
    _interrupt_job_run,
    _load_jobs,
    _lock,
    _running,
    _save_jobs,
    parse_schedule,
    scheduler_health,
    set_agent_factory,
    set_platform_adapter,
    start_scheduler,
)
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

    with _lock, CrossProcessLock(_JOBS_LOCK_FILE):
        _load_jobs()
        # Auto-replace existing jobs with the same name to prevent duplicates
        if job_name:
            old_ids = [jid for jid, j in scheduler._jobs.items() if j.get("name") == job_name]
            for old_id in old_ids:
                del scheduler._jobs[old_id]
                logger.info("Auto-replaced duplicate job '%s' (old id: %s)", job_name, old_id)
        scheduler._jobs[job_id] = job
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

    with _lock, CrossProcessLock(_JOBS_LOCK_FILE):
        _load_jobs()
        # Try by ID first, then by name — delete ALL matching jobs
        deleted_ids = []
        if job_id in scheduler._jobs:
            deleted_ids.append(job_id)
        else:
            for jid, j in scheduler._jobs.items():
                if j.get("name") == job_id:
                    deleted_ids.append(jid)

        if not deleted_ids:
            return tool_error(f"Job not found: {job_id}")

        for did in deleted_ids:
            session_key = scheduler._active_sessions.get(did)
            if session_key:
                scheduler._cancel_flags.add(session_key)
                logger.info("Job %s marked for cancellation (session: %s)", did, session_key)
            _interrupt_job_run(did)
            del scheduler._jobs[did]
        _save_jobs()

    return tool_result(success=True, deleted=deleted_ids, count=len(deleted_ids))


def _list_jobs(args: dict) -> str:
    with _lock:
        _load_jobs()
        include_disabled = args.get("include_disabled", False)
        result = []
        for job_id, job in scheduler._jobs.items():
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
                "origin": job.get("origin"),
                "prompt": job.get("prompt", ""),
                "script": job.get("script"),
                "no_agent": job.get("no_agent", False),
                "context_from": job.get("context_from"),
                "enabled": job.get("enabled", True),
                "state": job.get("state", "scheduled"),
                "next_run_at": job.get("next_run_at"),
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
                "last_error": job.get("last_error"),
            })
    return tool_result(jobs=result, count=len(result))


def _run_job_manual(args: dict) -> str:
    """Run a job NOW, directly — bypasses the scheduler loop entirely.

    Works regardless of enabled state (paused jobs can be test-fired) and
    does NOT touch next_run_at: the recurring schedule is unaffected, this
    is a one-off execution just like the scheduler's own dispatch."""
    job_id = args.get("job_id", args.get("name", ""))
    if not job_id:
        return tool_error("job_id is required")

    with _lock:
        _load_jobs()
        job = scheduler._jobs.get(job_id)
        if not job:
            for jid, j in scheduler._jobs.items():
                if j.get("name") == job_id:
                    job_id = jid
                    job = j
                    break
        if not job:
            return tool_error(f"Job not found: {job_id}")
        snapshot = dict(job)

    import threading
    t = threading.Thread(target=scheduler._execute_job, args=(snapshot,),
                         daemon=True, name=f"cron-manual-{job_id}")
    t.start()
    return tool_result(success=True, job_id=job_id,
                       message="Job triggered — result will arrive at its delivery target")


def _pause_job(args: dict) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required")
    with _lock, CrossProcessLock(_JOBS_LOCK_FILE):
        _load_jobs()
        if job_id not in scheduler._jobs:
            return tool_error(f"Job not found: {job_id}")
        scheduler._jobs[job_id]["enabled"] = False
        scheduler._jobs[job_id]["state"] = "paused"

        session_key = scheduler._active_sessions.get(job_id)
        if session_key:
            scheduler._cancel_flags.add(session_key)
            logger.info("Job %s marked for cancellation (session: %s)", job_id, session_key)
        _interrupt_job_run(job_id)
        _save_jobs()
    return tool_result(success=True, job_id=job_id)


def _resume_job(args: dict) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required")
    with _lock, CrossProcessLock(_JOBS_LOCK_FILE):
        _load_jobs()
        if job_id not in scheduler._jobs:
            return tool_error(f"Job not found: {job_id}")
        scheduler._jobs[job_id]["enabled"] = True
        scheduler._jobs[job_id]["state"] = "scheduled"
        # Resume computes the next run from NOW — passing last_run_at (which
        # may be months stale after a long pause) would yield a past
        # timestamp that displays as nonsense until the next tick fires.
        scheduler._jobs[job_id]["next_run_at"] = _compute_next_run(
            scheduler._jobs[job_id]["schedule"])
        _save_jobs()
    return tool_result(success=True, job_id=job_id)


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
                            "Script name or path. Resolved against ${AGENT_HOME}/cron/scripts/, then the "
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
