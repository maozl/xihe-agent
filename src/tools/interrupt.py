"""Per-agent interrupt signaling for long-running tools.

A contextvar holds the agent currently running on this thread, so a tool can
check whether ITS OWN agent was interrupted — not a process-global flag. This
keeps interrupts scoped per session: one chat's /stop won't abort a tool
running for a different concurrent chat, which the previous global
threading.Event did (any agent's interrupt() set it for everyone).

The agent loop binds itself via bind_current_agent()/reset_current_agent()
around every tool dispatch (both the sequential and the parallel
ThreadPoolExecutor paths), so a tool always attributes an interrupt to the
agent that owns the turn it's running in.

Usage in tools:
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"output": "[interrupted]", "returncode": 130}
"""

import contextvars
import logging

logger = logging.getLogger(__name__)

# The agent whose turn is currently executing on this thread, or None when
# no agent loop is bound (e.g. a tool invoked outside any agent — cron
# no_agent scripts, tests). Per-thread/per-task via contextvars.
_current_agent = contextvars.ContextVar("xihe_current_agent", default=None)


def bind_current_agent(agent):
    """Bind `agent` as the current agent on this thread/context.

    Returns a token to pass to reset_current_agent(). The agent loop calls
    this around each tool dispatch so tools can attribute interrupts to the
    correct agent.
    """
    return _current_agent.set(agent)


def reset_current_agent(token):
    """Restore the previous binding (pass the token from bind_current_agent)."""
    _current_agent.reset(token)


def is_interrupted() -> bool:
    """True if the agent running on this thread has been interrupted.

    Safe to call from any thread; returns False when no agent is bound.
    """
    agent = _current_agent.get()
    if agent is None:
        return False
    try:
        return bool(agent.is_interrupted())
    except Exception:
        return False


def interruptible_iter(iterable, every=32):
    """Iterate `iterable`, checking for an interrupt every `every` items.

    Stops yielding as soon as an interrupt is detected, so the caller's `for`
    loop ends early. Wrap a slow loop's iterable to make a tool interrupt-
    responsive without hand-sprinkling is_interrupted() checks:

        for f in interruptible_iter(root.rglob("*")):
            ...

    After the loop, call is_interrupted() to detect early termination and flag
    partial results (the generator just stops — it does not signal WHY).

    Uses the per-agent contextvar, so the check is scoped to the session whose
    turn is running this loop. `every` trades latency vs check cost (each check
    is a sub-microsecond contextvar lookup). When no agent is bound,
    is_interrupted() returns False and this is a plain passthrough.
    """
    if every < 1:
        every = 1
    for i, item in enumerate(iterable):
        if i % every == 0 and is_interrupted():
            return
        yield item


def register_subprocess(proc) -> None:
    """Register a subprocess with the currently-running agent so its
    interrupt() can kill it (prompt /stop for subprocess tools). No-op when no
    agent is bound. The tool MUST call unregister_subprocess(proc) when done.

    Used by tools that spawn a long-running Popen and block on wait/communicate
    (terminal, execute_code). interrupt() on the agent kills every registered
    proc, unblocking the tool without per-tool polling.
    """
    agent = _current_agent.get()
    if agent is None:
        # legitimate in cron/no-agent contexts, but worth a trace: if this
        # fires from a tool dispatch the contextvar wiring broke and /stop
        # will silently kill nothing
        logger.debug("register_subprocess: no agent bound — proc %r not interruptible",
                     getattr(proc, "pid", proc))
        return
    try:
        agent.register_subprocess(proc)
    except Exception:
        # a swallowed failure here = /stop silently kills nothing
        logger.warning("register_subprocess failed (proc %r)",
                       getattr(proc, "pid", proc), exc_info=True)


def unregister_subprocess(proc) -> None:
    agent = _current_agent.get()
    if agent is not None:
        try:
            agent.unregister_subprocess(proc)
        except Exception:
            logger.warning("unregister_subprocess failed (proc %r)",
                           getattr(proc, "pid", proc), exc_info=True)


def run_interruptible(*popenargs, timeout=None, capture_output=False,
                      text=None, **kwargs):
    """subprocess.run drop-in that registers the child with the current agent
    so /stop can kill it mid-run.

    Supports the common kwargs (capture_output, text, timeout, encoding, cwd,
    env, ...). Returns a subprocess.CompletedProcess. On timeout, kills and
    reaps the child. The agent is resolved from the contextvar (set by the
    dispatch loop), so callers don't thread parent_agent — same model as
    is_interrupted(). When no agent is bound, behaves exactly like subprocess.run.

    Note: proc.kill() terminates the direct child; grandchildren (e.g. mvn→java)
    may be orphaned on Windows. The tool still unblocks promptly, which is the
    goal — full process-tree kill needs job objects (out of scope).
    """
    import subprocess
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr cannot be set with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if text is not None:
        kwargs["text"] = text
    proc = subprocess.Popen(*popenargs, **kwargs)
    register_subprocess(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            stdout, stderr = None, None
    finally:
        unregister_subprocess(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
