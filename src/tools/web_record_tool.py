"""Web Record Tool — record a user's browser actions via `playwright codegen`.

Launches ``playwright codegen`` as a subprocess (system Chrome/Edge, same
family the agent's ``browser_*`` tools use) and waits for the user to perform
their actions and close the browser. It then returns the generated Playwright
Python script inline plus the saved login-state path.

This tool ONLY records. It does NOT build the skill — the agent turns the
recording into a skill via ``skill_manage``, guided by the bundled
``web-record-to-skill`` skill (translation table, output format, login caveats).

Why a separate browser from the agent's ``browser_*`` tools
-----------------------------------------------------------
``playwright codegen`` launches its OWN browser: a fresh temp profile, even
with ``--channel chrome``. The agent's ``browser_*`` tools drive a CDP-managed
system Chrome on port 9222 with the persistent ``cdp-profile``. They are NOT
the same process and do not share a profile dir, and codegen has no
``--connect-over-cdp`` / ``--user-data-dir`` flag, so it cannot attach to the
agent's Chrome.

Login persistence (the recording profile)
-----------------------------------------
The recording browser runs with a PERSISTENT user-data-dir
(``<AGENT_HOME>/browser/recording-profile``) via codegen ``--user-data-dir``, so
login SURVIVES across recordings: cookies, localStorage, AND HSTS all persist.
The user logs in once per site on the first recording; subsequent recordings
start already logged in. (This is why we no longer use codegen's
``--load-storage``/``storage_state`` — storageState carries only
cookies+localStorage and not HSTS, so it couldn't hold SSO that redirects
through http:// before upgrading to https; a real profile can.)

If ``save_login`` is set, codegen's ``--save-storage`` ALSO exports a
cookies+localStorage JSON (returned as ``auth_state_path``) for optional reuse
in the agent's CDP Chrome via ``browser_state_load``. The recording profile is
separate from browser_tool's ``cdp-profile`` so the two Chrome processes don't
clash. Delete ``recording-profile`` to reset all saved logins.
"""

import logging
import re
import subprocess
import sys
import time

from core.config import AGENT_HOME
from tools import registry, tool_error, tool_result
from tools.interrupt import is_interrupted, register_subprocess, unregister_subprocess

logger = logging.getLogger(__name__)

_RECORDINGS_DIR = AGENT_HOME / "browser" / "recordings"
# Persistent profile for the RECORDING browser. Survives across recordings, so
# login (cookies + HSTS + session) persists too — the user logs in once per
# site, then later recordings start already logged in. Separate from
# browser_tool's cdp-profile so the two Chromes don't clash.
_RECORDING_PROFILE_DIR = AGENT_HOME / "browser" / "recording-profile"


# Deliberately the SAME gate as the browser_* tools (browser_tool._PW_OK):
# available iff Playwright is importable. We do NOT additionally probe
# `playwright --version` — that extra subprocess check once hid this tool in
# the gateway even though browser_* worked, so the agent couldn't record and
# fell back to driving the browser itself with browser_*. Keeping parity means
# web_record is always visible alongside browser_*. (If codegen itself is
# broken at runtime, the handler returns a clear error instead.)


def _check_web_record() -> bool:
    try:
        from tools.browser_tool import _PW_OK
        return bool(_PW_OK)
    except Exception:
        return False


def _pick_channel() -> str:
    """Choose the codegen --channel matching an installed system browser.

    Reuses browser_tool's detection so we launch the same browser family the
    agent's browser_* tools would. Returns 'chrome' or 'msedge' (default
    'chrome' if detection fails — codegen then surfaces a clear error).
    """
    try:
        from tools.browser_tool import _find_system_chrome
        exe = _find_system_chrome()
        if exe and "msedge" in exe.replace("\\", "/").split("/")[-1].lower():
            return "msedge"
    except Exception:
        pass
    return "chrome"


def _kill_proc(proc) -> None:
    """Best-effort kill + reap of a codegen subprocess."""
    try:
        proc.kill()
    except Exception:
        # a failed kill leaves the codegen browser running — visible, not silent
        logger.warning("web_record: kill of codegen subprocess failed (pid=%s)",
                       getattr(proc, "pid", "?"), exc_info=True)
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def _run_codegen(channel, url, rec_path, auth_path, profile_dir, timeout):
    """Run one codegen attempt, polling for exit / interrupt / timeout.

    Returns (returncode, elapsed_seconds). returncode is the int process exit
    code, or the strings 'interrupted' / 'timeout'.
    """
    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        "--target", "python",
        "--ignore-https-errors",
        "--channel", channel,
        "--user-data-dir", str(profile_dir),
        "-o", str(rec_path),
    ]
    if auth_path is not None:
        cmd += ["--save-storage", str(auth_path)]
    cmd.append(url)

    logger.info("web_record launching codegen: %s", " ".join(cmd))
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        return (f"spawn-error:{e}", 0.0)

    register_subprocess(proc)  # so agent interrupt() / timeout can kill it
    start = time.monotonic()
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                return (rc, time.monotonic() - start)
            if is_interrupted():
                _kill_proc(proc)
                return ("interrupted", time.monotonic() - start)
            if time.monotonic() - start >= timeout:
                _kill_proc(proc)
                return ("timeout", time.monotonic() - start)
            time.sleep(0.5)
    finally:
        unregister_subprocess(proc)


def _web_record(args: dict, **kw) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return tool_error("url is required (the page to start recording from).")

    save_login = bool(args.get("save_login", True))
    try:
        timeout = int(args.get("timeout", 600))
    except (TypeError, ValueError):
        timeout = 600
    if timeout < 10:
        timeout = 10

    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    _RECORDING_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    rec_path = _RECORDINGS_DIR / f"recorded_{ts}.py"
    auth_path = _RECORDINGS_DIR / f"auth_{ts}.json" if save_login else None

    # Try the detected channel first, then the other as a fallback. A bad
    # channel fails fast at startup (nonzero exit, no recording file, <20s).
    primary = _pick_channel()
    channels = [primary] + [c for c in ("chrome", "msedge") if c != primary]

    last_err = None
    used_channel = None
    for ch in channels:
        rec_path.unlink(missing_ok=True)
        if auth_path:
            auth_path.unlink(missing_ok=True)
        rc, elapsed = _run_codegen(ch, url, rec_path, auth_path, _RECORDING_PROFILE_DIR, timeout)

        if rc == "interrupted":
            return tool_error("录制已中断（agent 收到 /stop）。录制内容未保存——请重新调用 web_record 再录一次。")
        if rc == "timeout":
            return tool_error(
                f"录制超时（等待 {timeout}s 未结束）。请关闭弹出的浏览器窗口结束录制后再重试。",
                hint="关掉浏览器窗口后重新调用 web_record；或在环境支持时调大 timeout。",
            )

        if isinstance(rc, str) and rc.startswith("spawn-error"):
            last_err = rc
            continue

        # Startup failure (wrong channel): nonzero, no file, fast. Try next.
        if rc != 0 and not rec_path.exists() and elapsed < 20:
            last_err = f"channel '{ch}' failed to start (rc={rc})"
            logger.info("web_record: %s; trying next channel", last_err)
            continue

        used_channel = ch
        break

    if not rec_path.exists():
        return tool_error(
            "录制未生成脚本文件。请在弹出的浏览器窗口里实际操作后再关闭它。",
            channels_attempted=channels, last_error=last_err,
        )

    try:
        script = rec_path.read_text(encoding="utf-8")
    except Exception as e:
        return tool_error(f"无法读取录制脚本 {rec_path}: {e}")

    page_calls = re.findall(r'\bpage\.\w+\s*\(', script)
    if not script.strip() or not page_calls:
        return tool_error(
            "录制为空（没捕获到 page 操作）。请在浏览器窗口里实际点击/输入后再关闭。",
            script_path=str(rec_path),
            hint="删除该空录制后重新调用 web_record。",
        )

    auth_state_path = str(auth_path) if (auth_path and auth_path.exists()) else None

    return tool_result(
        success=True,
        url=url,
        channel=used_channel,
        script_path=str(rec_path),
        script=script,
        auth_state_path=auth_state_path,
        steps_recorded=len(page_calls),
        hint=(
            "按 `web-record-to-skill` skill 把这段录制做成 skill：把脚本里的 "
            "page.goto/click/fill/type/press/select_option/evaluate/wait_for_timeout "
            "翻译成 browser_navigate/click/type/press/select/eval/wait 等步骤，用 "
            "skill_manage(action='create', name=..., category='workflows', content=<SKILL.md>) "
            "生成；再用 skill_manage(action='write_file', name=..., file_path='references/recorded.py', "
            "file_content=<本脚本>) 归档原始脚本，登录态归档到 references/auth_state.json（若有）。"
            "下次录制同站点会自动保持登录（持久 profile）。"
        ),
    )


registry.register(
    name="web_record",
    toolset="web",
    check_fn=_check_web_record,
    read_only=False,
    subagent_blocked=True,
    handler=_web_record,
    schema={
        "type": "function",
        "function": {
            "name": "web_record",
            "description": (
                "Record a user's web interactions into a Playwright script using "
                "`playwright codegen`, then return the recorded script. Use this to "
                "capture a business flow that you will then turn into a skill via "
                "skill_manage, following the `web-record-to-skill` skill.\n\n"
                "How it works: a Chrome/Edge window opens at `url`; the USER performs "
                "the actions and closes the browser to stop recording; this tool "
                "returns the generated Python script inline (plus the saved login "
                "state path). It does NOT create the skill itself.\n\n"
                "Login persists across recordings: the recording browser uses a "
                "persistent profile, so the user logs in once per site on the first "
                "recording and is auto-logged-in thereafter. The recording browser is "
                "SEPARATE from the agent's browser_* tools (different process/profile)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Starting URL for recording.",
                    },
                    "save_login": {
                        "type": "boolean",
                        "description": (
                            "Also export the recording session's storage state on close "
                            "(codegen --save-storage) and return its path, for optional "
                            "reuse in the agent's CDP Chrome via browser_state_load. "
                            "Default true. Login itself already persists via the profile."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "description": (
                            "Max seconds to wait for the user to close the browser "
                            "(default 600). On timeout the recording is discarded."
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
)
