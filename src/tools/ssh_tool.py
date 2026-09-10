"""Generic SSH tools — connect, exec, disconnect, status.

Auto-detects exec mode (exec_command, clean output + exit code) vs shell mode
(invoke_shell, interactive). Supports target_ip for jumping to downstream
machines. Session parameters saved to ~/.xihe-agent/ssh/sessions.json for
reconnect after gateway restart (only password needs re-entry).

Tools:
  ssh_connect: Connect to any SSH host
  ssh_exec: Execute command (optionally on a target via the connected host)
  ssh_disconnect: Close session
  ssh_status: Show all sessions
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools import registry, tool_error, tool_result
from tools import _ssh_tap

logger = logging.getLogger(__name__)

_paramiko = None          # imported on first real use (see _get_paramiko)
_paramiko_probed = False


def _get_paramiko():
    """Import paramiko on first actual use. The module import costs ~1s of
    transitive deps (cryptography) that every process start otherwise paid
    just so check_fn could report availability."""
    global _paramiko, _paramiko_probed
    if _paramiko is None and not _paramiko_probed:
        _paramiko_probed = True
        try:
            import paramiko
            _paramiko = paramiko
        except ImportError:
            _paramiko = None
    return _paramiko


from core.config import AGENT_HOME
_SSH_DIR = AGENT_HOME / "ssh"
_SESSIONS_FILE = _SSH_DIR / "sessions.json"
_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT = 30000

# Shell pty shape: 256-color TERM plus a size wide enough that log lines
# don't fold badly. The desktop viewer renders at this fixed size — the pty,
# not the viewer window, defines wrapping.
SHELL_TERM = "xterm-256color"
SHELL_COLS = 100
SHELL_ROWS = 30
# bastions drop silent transports on idle timeout; keepalive traffic prevents it
KEEPALIVE_SEC = 30


def _check_ssh() -> bool:
    """Availability without importing: find_spec locates the module without
    executing it (paramiko's own import is the expensive part)."""
    import importlib.util
    if not _paramiko_probed:
        return importlib.util.find_spec("paramiko") is not None
    return _paramiko is not None


@dataclass
class SSHSession:
    name: str
    client: object  # paramiko.SSHClient (lazy import — see _get_paramiko)
    host: str
    port: int
    user: str
    mode: str                        # "exec" or "shell"
    channel: Optional[object] = None # shell channel (only for shell mode)
    connected_at: float = 0.0
    last_activity: float = 0.0
    tap: Optional[object] = None     # _ssh_tap.SessionTap (viewer + input sharing)
    session_key: str = ""            # conversation that owns/last-used this session
    origin: str = "agent"            # "agent" | "desktop" — who connected


_sessions: dict[str, SSHSession] = {}
_saved_sessions: dict[str, dict] = {}
_ssh_lock = threading.RLock()


def _skey(session_key: str, name: str) -> str:
    """Registry key for a live session: conversations are isolated — two
    conversations using the same alias each get their own connection and tap
    (a shared shell tap would also share one agent_cursor, so interleaved
    commands from both conversations would corrupt each other's reads)."""
    return f"{session_key}|{name}"


def _save_sessions():
    try:
        _SSH_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        for s in _sessions.values():
            data[s.name] = {"host": s.host, "port": s.port, "user": s.user, "mode": s.mode}
        for name, params in _saved_sessions.items():
            if name not in data:
                data[name] = params
        _SESSIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save SSH sessions: %s", e)


def _load_saved_sessions():
    global _saved_sessions
    try:
        if _SESSIONS_FILE.exists():
            _saved_sessions = json.loads(_SESSIONS_FILE.read_text(encoding="utf-8"))
            logger.info("Loaded %d saved SSH sessions", len(_saved_sessions))
    except Exception:
        _saved_sessions = {}


_load_saved_sessions()


def _open_shell_channel(client, key: str, meta: dict):
    """invoke_shell + tap attach + banner consume. The pump owns channel.recv
    from the moment the channel exists, so the banner is read from the ring —
    the agent cursor is then advanced past it (the legacy connect discarded
    the banner too; only the viewer keeps it). Returns (channel, tap)."""
    channel = client.invoke_shell(term=SHELL_TERM, width=SHELL_COLS, height=SHELL_ROWS)
    tap = _ssh_tap.attach(key, channel=channel, meta=dict(meta, mode="shell"))
    time.sleep(1.5)
    _ssh_tap.read_until_prompt(tap, 0, timeout=10, check_interrupt=False)
    tap.agent_cursor = tap.ring.w
    return channel, tap


def _clean_shell_output(output, command=""):
    lines = output.split("\n")
    cleaned = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.match(r'^\[.*\][\$#]', s):
            continue
        if command and s == command:
            continue
        if s.startswith("Connecting to") or s.startswith("Last login"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _is_alive(session: SSHSession) -> bool:
    try:
        t = session.client.get_transport()
        return t is not None and t.is_active()
    except Exception:
        return False


def _cleanup_dead(key: str):
    session = _sessions.pop(key, None)
    _ssh_tap.close_tap(key)    # mark closed first — the pump self-exits, no join here
    if session:
        try:
            if session.channel:
                session.channel.close()
        except Exception:
            pass
        try:
            session.client.close()
        except Exception:
            pass


def _ssh_connect(args: dict, **kw) -> str:
    paramiko = _get_paramiko()
    if paramiko is None:
        return tool_error("paramiko not installed. pip install paramiko")

    name = args.get("name", "").strip()
    host = args.get("host", "").strip()
    port = int(args.get("port", 22))
    user = args.get("user", "").strip()
    password = args.get("password", "").strip() or args.get("token", "").strip()
    connect_timeout = int(args.get("timeout", 15))
    # mode default "shell" — most connections go through bastion (one-time
    # token). Use mode="exec" for direct SSH to normal servers.
    requested_mode = args.get("mode", "shell").strip().lower()

    if not name:
        return tool_error("name is required (session alias)")

    if not host and name in _saved_sessions:
        saved = _saved_sessions[name]
        host = saved.get("host", "")
        port = int(saved.get("port", 22))
        user = saved.get("user", user)

    if not host:
        return tool_error("host is required (or session '%s' must have saved params)" % name)
    if not user:
        return tool_error("user is required")

    ctx = kw.get("context") or {}
    session_key = str(ctx.get("session_key") or "")
    origin = str(kw.get("origin") or "agent")
    key = _skey(session_key, name)
    meta = {"host": host, "user": user, "origin": origin, "alias": name,
            "session_key": session_key, "cols": SHELL_COLS, "rows": SHELL_ROWS}

    with _ssh_lock:
        existing = _sessions.get(key)
        if existing and _is_alive(existing):
            return tool_result(
                success=True, session=name, host=existing.host, mode=existing.mode,
                message=f"Already connected: {name} -> {existing.host} ({existing.mode})",
            )
        if existing:
            _cleanup_dead(key)

    if not password:
        logger.info("SSH [%s]: no password, asking user via clarify", name)
        from tools.clarify_tool import _clarify
        return _clarify(
            question=f"请输入 {user}@{host} 的 SSH 密码/Token：",
            reason=f"SSH authentication for {name}@{host}",
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        logger.info("SSH [%s]: connecting %s@%s:%d (mode=%s)", name, user, host, port, requested_mode)
        client.connect(
            hostname=host, port=port, username=user, password=password,
            timeout=connect_timeout, look_for_keys=False, allow_agent=False,
        )
        client.get_transport().set_keepalive(KEEPALIVE_SEC)
        logger.info("SSH [%s]: connected, transport active=%s", name,
                     client.get_transport() is not None)
    except paramiko.AuthenticationException:
        logger.warning("SSH [%s]: auth failed for %s@%s", name, user, host)
        return tool_error(f"Authentication failed for {user}@{host}. Password/token may be expired.")
    except Exception as e:
        logger.warning("SSH [%s]: connect failed: %s: %s", name, type(e).__name__, e)
        return tool_error(f"SSH connect failed: {type(e).__name__}: {e}")

    mode = "exec"
    channel = None
    tap = None

    if requested_mode == "shell":
        # Skip exec_command — go straight to invoke_shell
        mode = "shell"
        try:
            logger.info("SSH [%s]: opening invoke_shell (mode=shell)", name)
            channel, tap = _open_shell_channel(client, key, meta)
        except Exception as e:
            logger.warning("SSH [%s]: invoke_shell failed: %s", name, e)
            _ssh_tap.close_tap(key)
            try:
                client.close()
            except Exception:
                pass
            return tool_error(f"Shell mode failed: {e}")
    else:
        try:
            stdin, stdout, stderr = client.exec_command("echo __SSH_OK__", timeout=5)
            test_out = stdout.read().decode("utf-8", errors="ignore").strip()
            if "__SSH_OK__" not in test_out:
                raise Exception("exec_command returned unexpected output")
            logger.info("SSH mode: exec (exec_command works)")
        except Exception:
            # exec failed — reconnect for shell mode (some hosts only allow
            # one channel per connection; exec consumed it, need fresh connect)
            logger.info("exec_command failed, reconnecting for shell mode")
            try:
                client.close()
            except Exception:
                pass
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=host, port=port, username=user, password=password,
                    timeout=connect_timeout, look_for_keys=False, allow_agent=False,
                )
                client.get_transport().set_keepalive(KEEPALIVE_SEC)
                mode = "shell"
                channel, tap = _open_shell_channel(client, key, meta)
                logger.info("SSH mode: shell (reconnected)")
            except Exception as e:
                try:
                    client.close()
                except Exception:
                    pass
                return tool_error(
                    f"Neither exec nor shell mode works: {e}. "
                    f"If using one-time token + bastion, try mode='shell' to "
                    f"skip the exec probe."
                )

    now = time.time()
    session = SSHSession(
        name=name, client=client, host=host, port=port, user=user,
        mode=mode, channel=channel, connected_at=now, last_activity=now,
        session_key=session_key, origin=origin,
    )
    if tap is None:
        # exec mode: passive tap — no pump, _exec_mode publishes its chunks
        tap = _ssh_tap.attach(key, channel=None,
                              meta=dict(meta, mode="exec"), session=session)
    else:
        tap.session = session
    session.tap = tap

    with _ssh_lock:
        _sessions[key] = session
        _saved_sessions[name] = {"host": host, "port": port, "user": user, "mode": mode}
        _save_sessions()

    return tool_result(
        success=True, session=name, host=host, port=port, user=user, mode=mode,
        message=f"Connected: {name} -> {host} ({mode})",
    )


def _ssh_exec(args: dict, **kw) -> str:
    paramiko = _get_paramiko()
    if paramiko is None:
        return tool_error("paramiko not installed")

    session_name = args.get("session", "").strip()
    command = args.get("command", "").strip()
    target_ip = args.get("target_ip", "").strip()
    timeout = min(int(args.get("timeout", _DEFAULT_TIMEOUT)), 600)

    if not session_name:
        return tool_error("session is required (use ssh_status to see available)")
    if not command:
        return tool_error("command is required")

    logger.info("SSH exec [%s]: cmd=%.100s target_ip=%s timeout=%d",
                session_name, command, target_ip or "(none)", timeout)

    sk = str((kw.get("context") or {}).get("session_key") or "")
    key = _skey(sk, session_name)

    with _ssh_lock:
        session = _sessions.get(key)
        if not session:
            logger.warning("SSH exec [%s]: session not found", session_name)
            if any(s.name == session_name for s in _sessions.values()):
                return tool_error(
                    f"Session '{session_name}' is connected in another conversation — "
                    f"sessions are per-conversation. Reconnect it here with "
                    f"ssh_connect(name='{session_name}')."
                )
            if session_name in _saved_sessions:
                saved = _saved_sessions[session_name]
                return tool_error(
                    f"Session '{session_name}' not connected (saved: "
                    f"{saved.get('user','?')}@{saved.get('host','?')}). "
                    f"Reconnect with ssh_connect(name='{session_name}')."
                )
            return tool_error(f"Session '{session_name}' not found. Use ssh_connect first.")

        if not _is_alive(session):
            logger.warning("SSH exec [%s]: session dead, cleaning up", session_name)
            _cleanup_dead(key)
            return tool_error(f"Session '{session_name}' disconnected. Reconnect with ssh_connect.")

        logger.info("SSH exec [%s]: using %s mode, alive=True", session_name, session.mode)

        try:
            if session.mode == "exec":
                result = _exec_mode(session, command, target_ip, timeout)
            else:
                result = _shell_mode(session, command, target_ip, timeout)
            session.last_activity = time.time()
            logger.info("SSH exec [%s]: success=%s exit=%s output=%d chars",
                        session_name, result.get("success"),
                        result.get("exit_code", "?"),
                        len(result.get("output", "")))
        except Exception as e:
            logger.warning("SSH exec [%s]: failed: %s: %s", session_name, type(e).__name__, e)
            _cleanup_dead(key)
            return tool_error(f"Exec failed (session may be dead): {e}")

    output = result.get("output", "")
    if len(output) > _MAX_OUTPUT:
        head = output[:_MAX_OUTPUT // 2]
        tail = output[-_MAX_OUTPUT // 2:]
        result["output"] = head + f"\n\n... [truncated - {len(output) - _MAX_OUTPUT:,} chars] ...\n\n" + tail

    logger.info("ssh_exec [%s/%s] exit=%s output=%d chars",
                session_name, target_ip or "host", result.get("exit_code", "?"),
                len(result.get("output", "")))
    return tool_result(session=session_name, host=session.host,
                       target=target_ip or session.host, **result)


def _drain_exec_channel(chan, timeout: int, tap=None):
    """Chunked read of an exec channel. stdout/stderr are kept separate
    (parity with the old stdout.read()/stderr.read() pair) and every chunk is
    published to the tap so the desktop panel streams live — the old
    stdout.read() made the whole output appear only when the command ended.
    The old exec_command(timeout=N) bounded each blocking recv; the same N
    here bounds time WITHOUT data (idle), not total runtime."""
    out: list[str] = []
    err: list[str] = []
    last_data = time.time()
    timed_out = False
    while True:
        got = False
        while chan.recv_ready():
            s = chan.recv(4096).decode("utf-8", errors="replace")
            if s:
                out.append(s)
                got = True
                if tap is not None:
                    tap.publish(s)
        while chan.recv_stderr_ready():
            s = chan.recv_stderr(4096).decode("utf-8", errors="replace")
            if s:
                err.append(s)
                got = True
                if tap is not None:
                    tap.publish(s)
        if got:
            last_data = time.time()
            continue
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.time() - last_data > timeout:
            timed_out = True
            break
        time.sleep(0.05)
    exit_code = chan.recv_exit_status() if chan.exit_status_ready() else -1
    return "".join(out), "".join(err), exit_code, timed_out


def _exec_mode(session: SSHSession, command: str, target_ip: str, timeout: int) -> dict:
    """Execute using exec_command (clean output + exit code)."""
    tap = session.tap
    # exec channels have no pty echo — synthesize the command line for the
    # viewer only; the agent-facing output stays exactly the streams.
    if tap is not None:
        tap.publish(f"$ {command}\n")
    if target_ip:
        # Jump via direct-tcpip channel
        paramiko = _get_paramiko()
        transport = session.client.get_transport()
        channel = transport.open_channel("direct-tcpip", (target_ip, 22), ("", 0), timeout=timeout)
        target_client = paramiko.SSHClient()
        target_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        target_client.connect(
            hostname=target_ip, port=22, username=session.user, sock=channel,
            timeout=timeout, look_for_keys=False, allow_agent=False,
        )
        stdin, stdout, stderr = target_client.exec_command(command, timeout=timeout)
        output, err, exit_code, timed_out = _drain_exec_channel(stdout.channel, timeout, tap)
        target_client.close()
    else:
        stdin, stdout, stderr = session.client.exec_command(command, timeout=timeout)
        output, err, exit_code, timed_out = _drain_exec_channel(stdout.channel, timeout, tap)

    if tap is not None:
        tap.publish(f"\n[exit {exit_code}]\n")

    result = {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output or "(no output)",
        **({"stderr": err[:5000]} if err.strip() else {}),
    }
    if timed_out:
        result["timed_out"] = True
    return result


def _shell_mode(session: SSHSession, command: str, target_ip: str, timeout: int) -> dict:
    """Execute using interactive shell (send/recv) through the session tap.

    The pump owns channel.recv; this sends via the tap (attribution: who
    typed what) and reads new bytes from the ring at the session-persistent
    agent cursor — desktop-typed commands surface in the next agent read
    instead of being swallowed. `prompt` reports the final prompt line: the
    cleaned output strips prompt lines, so without it the model cannot see
    where the stateful chain (ssh → sudo su → …) currently sits.

    For target_ip jumps: wraps command in `ssh -tt <ip> "<command>"` with
    proper escaping. Uses -tt to force TTY allocation so sudo works.
    """
    tap = session.tap
    if tap is None:
        raise RuntimeError("session tap missing (reconnect required)")

    if target_ip:
        # Escape double quotes in the command, then wrap in outer double quotes.
        # Use ssh -tt to allocate a PTY (required for sudo to work in nested ssh).
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        full_cmd = f'ssh -tt {target_ip} "{escaped}"'
    else:
        full_cmd = command

    logger.info("Shell mode full_cmd: %s", full_cmd[:120])
    start_off = tap.agent_cursor
    tap.send_input(full_cmd + "\n", who="agent")
    time.sleep(0.5)
    # Progress-aware wait: output flow keeps the read alive up to the total
    # ceiling; only a silent shell gives up at the idle mark (bounded by the
    # pre-600 era's 120s so a hung command can't stall the agent loop longer
    # than it used to).
    raw, cursor, _interrupted = _ssh_tap.read_until_prompt(
        tap, start_off, timeout=timeout, idle_timeout=min(timeout, 120))
    tap.agent_cursor = cursor
    output = _clean_shell_output(raw, full_cmd)

    result = {
        "success": True,
        "exit_code": None,
        "output": output or "(no output)",
        "mode": "shell",
    }
    prompt = _ssh_tap.last_prompt_line(raw)
    if prompt:
        result["prompt"] = prompt
    else:
        # No prompt by the time the wait ended: the command very likely still
        # runs in the remote shell, and anything sent now queues behind it.
        # Say so explicitly — "(no output)" alone reads as "done, nothing to
        # show" and invites sleep/echo poll pileups in the typed-ahead queue.
        result["still_running"] = True
        result["note"] = (
            "command likely still running remotely; further ssh_exec commands "
            "queue behind it — wait, then send a cheap command (echo PING) to "
            "collect its tail output instead of piling on polls"
        )
    if tap.user_input_since(start_off):
        result["user_input_observed"] = True
    return result


def _ssh_disconnect(args: dict, **kw) -> str:
    name = args.get("session", "").strip()
    disconnect_all = bool(args.get("all", False))
    sk = str((kw.get("context") or {}).get("session_key") or "")

    with _ssh_lock:
        if disconnect_all:
            # all= THIS conversation's sessions — closing another
            # conversation's bastion from here would be a surprise.
            pairs = [(k, s.name) for k, s in _sessions.items()
                     if s.session_key == sk]
            for k, _ in pairs:
                _cleanup_dead(k)
            return tool_result(success=True, closed=[n for _, n in pairs],
                               message=f"Disconnected {len(pairs)} session(s).")

        if not name:
            return tool_error("session is required (or use all=true)")

        key = _skey(sk, name)
        if key not in _sessions:
            return tool_result(success=True, message=f"Session '{name}' was not connected.")

        _cleanup_dead(key)
        return tool_result(success=True, session=name, message=f"Disconnected: {name}")


def _ssh_status(args: dict, **kw) -> str:
    with _ssh_lock:
        result_sessions = []
        live_aliases = set()

        for key, s in _sessions.items():
            alive = _is_alive(s)
            if not alive:
                _cleanup_dead(key)
                continue
            live_aliases.add(s.name)
            result_sessions.append({
                "name": s.name, "host": s.host, "user": s.user,
                "mode": s.mode, "alive": True,
                "session_key": s.session_key,
                "idle_seconds": int(time.time() - s.last_activity),
                "saved": True,
            })

        for name, params in _saved_sessions.items():
            if name not in live_aliases:
                result_sessions.append({
                    "name": name, "host": params.get("host", "?"),
                    "user": params.get("user", "?"), "mode": params.get("mode", "?"),
                    "alive": False, "idle_seconds": 0, "saved": True,
                })

    return tool_result(sessions=result_sessions, count=len(result_sessions))


registry.register(
    name="ssh_connect",
    toolset="ssh",
    schema={
        "type": "function",
        "function": {
            "name": "ssh_connect",
            "description": (
                "Connect to any SSH host. Auto-detects exec mode (clean output + "
                "exit code) or shell mode (interactive, for bastion/jump hosts). "
                "Session params saved for reconnect after restart. If password "
                "omitted, asks user via clarify."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Session alias (e.g. 'bastion', 'my-server')."},
                    "host": {"type": "string", "description": "Target host or IP. Omit to use saved params."},
                    "port": {"type": "integer", "description": "SSH port (default 22)."},
                    "user": {"type": "string", "description": "SSH username."},
                    "password": {"type": "string", "description": "Password or hardware token. Omit to ask user."},
                    "token": {"type": "string", "description": "Alias for password (hardware token OTP)."},
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "exec", "shell"],
                        "description": (
                            "Connection mode. 'auto' (default): try exec_command, fall back to shell. "
                            "'exec': use exec_command only (clean output + exit code, for normal servers). "
                            "'shell': use invoke_shell only (for bastion/jump hosts with one-time tokens — "
                            "avoids wasting the token on a failed exec probe)."
                        ),
                    },
                    "timeout": {"type": "integer", "description": "Connection timeout in seconds (default 15)."},
                },
                "required": ["name"],
            },
        },
    },
    handler=lambda args, **kw: _ssh_connect(args, **kw),
    check_fn=_check_ssh,
    read_only=False,
)

registry.register(
    name="ssh_exec",
    toolset="ssh",
    schema={
        "type": "function",
        "function": {
            "name": "ssh_exec",
            "description": (
                "Execute a command on a connected session. If target_ip is given, "
                "the command runs on that machine (via direct-tcpip in exec mode, "
                "or 'ssh target_ip cmd' in shell mode). Without target_ip, runs "
                "directly on the connected host. Sessions are per-conversation: "
                "a session connected in another conversation must be reconnected "
                "here with ssh_connect first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session name."},
                    "command": {"type": "string", "description": "Command to execute."},
                    "target_ip": {
                        "type": "string",
                        "description": "Target machine IP. If set, command runs there (through the connected session).",
                    },
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 600) — a TOTAL ceiling: while output keeps arriving the wait auto-extends up to it. If it expires, the command likely still runs remotely (still_running=true) and any new command queues behind it; re-read later with a cheap command instead of piling on polls. For jobs expected to exceed it, run them in the background and poll a result file."},
                },
                "required": ["session", "command"],
            },
        },
    },
    handler=lambda args, **kw: _ssh_exec(args, **kw),
    check_fn=_check_ssh,
    read_only=False,
)

registry.register(
    name="ssh_disconnect",
    toolset="ssh",
    schema={
        "type": "function",
        "function": {
            "name": "ssh_disconnect",
            "description": "Disconnect an SSH session. Use all=true for everything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session name to disconnect."},
                    "all": {"type": "boolean", "description": "Disconnect all sessions."},
                },
            },
        },
    },
    handler=lambda args, **kw: _ssh_disconnect(args, **kw),
    check_fn=_check_ssh,
    read_only=False,
)

registry.register(
    name="ssh_status",
    toolset="ssh",
    schema={
        "type": "function",
        "function": {
            "name": "ssh_status",
            "description": "Show all SSH sessions — active connections and saved params awaiting reconnect.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    handler=lambda args, **kw: _ssh_status(args, **kw),
    check_fn=_check_ssh,
    read_only=True,
)
