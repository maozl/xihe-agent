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


_sessions: dict[str, SSHSession] = {}
_saved_sessions: dict[str, dict] = {}
_ssh_lock = threading.RLock()


def _save_sessions():
    try:
        _SSH_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        for name, s in _sessions.items():
            data[name] = {"host": s.host, "port": s.port, "user": s.user, "mode": s.mode}
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


def _read_until_prompt(channel, timeout=10, check_interrupt=True):
    """Read shell output until prompt appears or timeout.
    If check_interrupt=True, also abort on agent interrupt signal."""
    output = ""
    start = time.time()
    while time.time() - start < timeout:
        if check_interrupt:
            try:
                from tools.interrupt import is_interrupted
                if is_interrupted():
                    output += "\n[interrupted]"
                    break
            except Exception:
                pass
        if channel.recv_ready():
            data = channel.recv(4096).decode("utf-8", errors="ignore")
            output += data
            if re.search(r'\][\$#]', data):
                time.sleep(0.2)
                while channel.recv_ready():
                    output += channel.recv(4096).decode("utf-8", errors="ignore")
                break
        else:
            time.sleep(0.1)
    return output


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


def _cleanup_dead(name: str):
    session = _sessions.pop(name, None)
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

    with _ssh_lock:
        existing = _sessions.get(name)
        if existing and _is_alive(existing):
            return tool_result(
                success=True, session=name, host=existing.host, mode=existing.mode,
                message=f"Already connected: {name} -> {existing.host} ({existing.mode})",
            )
        if existing:
            _cleanup_dead(name)

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

    if requested_mode == "shell":
        # Skip exec_command — go straight to invoke_shell
        mode = "shell"
        try:
            logger.info("SSH [%s]: opening invoke_shell (mode=shell)", name)
            channel = client.invoke_shell(term="xterm")
            time.sleep(1.5)
            shell_output = _read_until_prompt(channel, timeout=10)
            logger.info("SSH [%s]: shell ready, initial output (%d chars): %.200s",
                        name, len(shell_output), shell_output[:200])
        except Exception as e:
            logger.warning("SSH [%s]: invoke_shell failed: %s", name, e)
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
                mode = "shell"
                channel = client.invoke_shell(term="xterm")
                time.sleep(1.5)
                _read_until_prompt(channel, timeout=10)
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
    )

    with _ssh_lock:
        _sessions[name] = session
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
    timeout = min(int(args.get("timeout", _DEFAULT_TIMEOUT)), 120)

    if not session_name:
        return tool_error("session is required (use ssh_status to see available)")
    if not command:
        return tool_error("command is required")

    logger.info("SSH exec [%s]: cmd=%.100s target_ip=%s timeout=%d",
                session_name, command, target_ip or "(none)", timeout)

    with _ssh_lock:
        session = _sessions.get(session_name)
        if not session:
            logger.warning("SSH exec [%s]: session not found", session_name)
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
            _cleanup_dead(session_name)
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
            _cleanup_dead(session_name)
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


def _exec_mode(session: SSHSession, command: str, target_ip: str, timeout: int) -> dict:
    """Execute using exec_command (clean output + exit code)."""
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
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        target_client.close()
    else:
        stdin, stdout, stderr = session.client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")

    return {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output or "(no output)",
        **({"stderr": err[:5000]} if err.strip() else {}),
    }


def _shell_mode(session: SSHSession, command: str, target_ip: str, timeout: int) -> dict:
    """Execute using interactive shell (send/recv).

    For target_ip jumps: wraps command in `ssh -tt <ip> "<command>"` with
    proper escaping. Uses -tt to force TTY allocation so sudo works.
    """
    channel = session.channel
    if target_ip:
        # Escape double quotes in the command, then wrap in outer double quotes.
        # Use ssh -tt to allocate a PTY (required for sudo to work in nested ssh).
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        full_cmd = f'ssh -tt {target_ip} "{escaped}"'
    else:
        full_cmd = command

    logger.info("Shell mode full_cmd: %s", full_cmd[:120])
    channel.send(full_cmd + "\n")
    time.sleep(0.5)
    raw = _read_until_prompt(channel, timeout=timeout)
    output = _clean_shell_output(raw, full_cmd)

    return {
        "success": True,
        "exit_code": None,
        "output": output or "(no output)",
        "mode": "shell",
    }


def _ssh_disconnect(args: dict, **kw) -> str:
    name = args.get("session", "").strip()
    disconnect_all = bool(args.get("all", False))

    with _ssh_lock:
        if disconnect_all:
            closed = list(_sessions.keys())
            for n in closed:
                _cleanup_dead(n)
            return tool_result(success=True, closed=closed, message=f"Disconnected {len(closed)} session(s).")

        if not name:
            return tool_error("session is required (or use all=true)")

        if name not in _sessions:
            return tool_result(success=True, message=f"Session '{name}' was not connected.")

        _cleanup_dead(name)
        return tool_result(success=True, session=name, message=f"Disconnected: {name}")


def _ssh_status(args: dict, **kw) -> str:
    with _ssh_lock:
        result_sessions = []

        for name, s in _sessions.items():
            alive = _is_alive(s)
            if not alive:
                _cleanup_dead(name)
            result_sessions.append({
                "name": name, "host": s.host, "user": s.user,
                "mode": s.mode, "alive": alive,
                "idle_seconds": int(time.time() - s.last_activity) if alive else 0,
                "saved": True,
            })

        for name, params in _saved_sessions.items():
            if name not in _sessions:
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
                "directly on the connected host."
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
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 120)."},
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
