"""Browser automation tools — navigate, snapshot, interact with web pages.

Browser launch strategy (DEFAULT = CDP-managed real Chrome):
  xihe launches its own real Chrome/Edge with a dedicated user-data-dir
  (${AGENT_HOME}/browser/cdp-profile) and --remote-debugging-port=9222, then
  drives it over CDP. Real Chrome carries HSTS memory, so Secure SSO cookies
  (e.g. SSO token) survive the http->https upgrade on SSO callbacks — a fresh
  Playwright context has no HSTS cache and silently drops those cookies, which
  is why persistent Playwright can't hold SSO sessions (portal SSO loop).
  The CDP Chrome is a detached process: it outlives xihe so login state in
  cdp-profile persists across xihe restarts.

  Falls back to launch_persistent_context (Playwright) only when CDP is
  unavailable — no system Chrome found, or the debug port can't be reached.

Authentication layers (on top of the CDP default):
  StorageState import/export — browser_login to land on a page,
                browser_state_save/load for explicit JSON cookie/localStorage
                snapshots (secondary; the cdp-profile is the primary store).
  browser_connect — manual override to attach to an external Chrome over CDP.

Tools:
  browser_navigate, browser_snapshot, browser_click, browser_type,
  browser_scroll, browser_back, browser_forward, browser_reload,
  browser_press, browser_hover, browser_select, browser_upload,
  browser_check, browser_uncheck, browser_drag,
  browser_screenshot, browser_console, browser_vision, browser_close,
  browser_wait, browser_eval, browser_tab_new, browser_tab_list,
  browser_tab_switch, browser_tab_close,
  browser_frame, browser_cookies, browser_state_save, browser_state_load,
  browser_connect, browser_login

Requires playwright: pip install playwright && playwright install chromium
"""

import functools
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from tools import registry, tool_error, tool_result
from tools.interrupt import is_interrupted

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    _PW_OK = True
except ImportError:
    _PW_OK = False


from core.config import AGENT_HOME
_BROWSER_DIR = AGENT_HOME / "browser"
_PROFILE_DIR = _BROWSER_DIR / "profile"
_STATES_DIR = _BROWSER_DIR / "states"
# CDP-managed browser (DEFAULT). xihe launches its own real Chrome with this
# dedicated user-data-dir + --remote-debugging-port. Real Chrome carries HSTS
# memory, so Secure SSO cookies (e.g. SSO token) survive the http->https upgrade
# on the SSO callback — the persistent Playwright path drops them.
_CDP_PROFILE_DIR = _BROWSER_DIR / "cdp-profile"
# Desktop-pushed UI appearance ({"dark": bool}) — runtime state next to
# cdp.pid/states, deliberately NOT config: the desktop owns theming, xihe
# only honors the last push so gateway-triggered launches match too.
_APPEARANCE_FILE = _BROWSER_DIR / "appearance.json"
# CDP Chrome debug port. Configurable via browser.cdp_port in config.yaml so
# multiple xihe instances on one machine can each run their own browser (each
# AGENT_HOME already isolates the cdp-profile; the port was the only hardcoded
# conflict).
try:
    from core.config import load_config as _load_cfg
    _CDP_PORT = int(((_load_cfg().get("browser") or {}).get("cdp_port")) or 9222)
except Exception:
    _CDP_PORT = 9222
_CDP_URL = f"http://127.0.0.1:{_CDP_PORT}"

# _browser_instance: Browser (from launch) or Browser (from connect_over_cdp)
# _context: BrowserContext (from browser.new_context)
# _pw: Playwright instance (kept alive across browser launches to avoid
#      greenlet thread-switching errors in gateway mode)

_browser_instance = None   # Browser
_context = None            # BrowserContext
_page = None               # Page
_pw = None                 # Playwright instance (singleton)
_mode = None               # "launch" | "cdp"
_channel = None            # "chrome" | "msedge" | None (bundled Chromium)


# Playwright sync API is greenlet-based: _pw/_browser_instance/_context/_page
# are bound to the thread that created them. The gateway runs each inbound
# message on a fresh daemon thread (gateway/bot.py), so reusing module-global
# Playwright state across messages raises "Cannot switch to a different thread".
# Every Playwright call is therefore funneled through _run_on_browser_thread(),
# which executes the work on a dedicated long-lived "browser-worker" thread.

_browser_thread_lock = threading.Lock()  # guards lazy worker start (multi-agent race)
_browser_thread = None              # dedicated worker Thread
_browser_queue = None               # queue.Queue[_Task]
_browser_thread_ident = None        # get_ident() of the worker (set in worker)
_BROWSER_OP_TIMEOUT = 120.0         # default per-op timeout (seconds)
_SENTINEL = object()                # shutdown signal posted on the queue


class _Task:
    __slots__ = ("fn", "args", "kwargs", "result_slot", "error_slot", "done")

    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result_slot = [None]
        self.error_slot = [None]
        self.done = threading.Event()


def _ensure_browser_thread():
    """Lazy-start the browser worker thread (idempotent, thread-safe)."""
    global _browser_thread, _browser_queue
    # Fast path — no lock once the worker is running.
    if _browser_thread is not None and _browser_thread.is_alive():
        return
    with _browser_thread_lock:
        # Double-check inside the lock — another agent thread may have started it.
        if _browser_queue is None:
            _browser_queue = queue.Queue()
        if _browser_thread is None or not _browser_thread.is_alive():
            _browser_thread = threading.Thread(
                target=_browser_worker_main, name="browser-worker", daemon=True
            )
            _browser_thread.start()
            logger.info("Browser worker thread started")


def _browser_worker_main():
    """Worker loop: owns all Playwright state, runs submitted tasks on its thread."""
    global _browser_thread_ident, _pw
    _browser_thread_ident = threading.get_ident()
    while True:
        task = _browser_queue.get()
        if task is _SENTINEL:
            # Shutdown — must run on this thread so _pw.stop() is valid.
            try:
                _cleanup_browser()
            except Exception:
                pass
            if _pw is not None:
                try:
                    _pw.stop()
                except Exception:
                    pass
                _pw = None
            logger.info("Browser worker thread stopping")
            break
        try:
            task.result_slot[0] = task.fn(*task.args, **task.kwargs)
        except BaseException as e:
            task.error_slot[0] = e
        finally:
            task.done.set()


def _run_on_browser_thread(fn, *args, timeout=_BROWSER_OP_TIMEOUT, **kwargs):
    """Run fn on the browser worker thread, blocking the caller until done.

    Re-entrancy guard: if already on the worker thread, run inline so a handler
    invoking another doesn't deadlock waiting on its own queue.
    """
    if _browser_thread_ident is not None and \
            threading.get_ident() == _browser_thread_ident:
        return fn(*args, **kwargs)
    _ensure_browser_thread()
    task = _Task(fn, args, kwargs)
    _browser_queue.put(task)
    if not task.done.wait(timeout=timeout):
        logger.warning(
            "browser-worker did not respond in %.1fs (fn=%s)",
            timeout, getattr(fn, "__name__", fn),
        )
        raise TimeoutError(f"browser-worker did not respond in {timeout}s")
    if task.error_slot[0] is not None:
        raise task.error_slot[0]
    return task.result_slot[0]


def _check_browser() -> bool:
    return _PW_OK


def _check_browser_vision() -> bool:
    """browser_vision needs Playwright AND a configured vision auxiliary client.

    Without this gate, browser_vision appears in the schema whenever browser
    tools are loaded, but fails at runtime if vision_model isn't configured
    (auxiliary client not configured for vision).
    """
    if not _check_browser():
        return False
    try:
        from tools.vision_tools import _check_vision
        return _check_vision()
    except Exception:
        return False


def _pw_alive() -> bool:
    """Best-effort check if Playwright is still usable.

    WARNING: This is NOT reliable. Playwright's internal greenlet can die
    without any sign at the Python level. Simple property accesses like
    _pw.chromium just return cached Python objects and don't go through
    the greenlet. Only actual browser operations (launch, evaluate, etc.)
    go through the greenlet and can detect death.

    Callers MUST handle "cannot switch to a different thread" errors at
    the point of actual operations, not rely on this pre-check.
    """
    if _pw is None:
        return False
    return True  # Best guess — actual check happens at operation time


def _start_pw():
    """Start a fresh Playwright instance. Returns the instance or None."""
    global _pw
    try:
        _pw = sync_playwright().start()
        logger.info("Playwright started")
        return _pw
    except Exception as e:
        logger.error("Failed to start Playwright: %s", e)
        _pw = None
        return None


def _ensure_pw():
    """Get the Playwright instance, starting fresh if needed.

    Strategy: try to reuse existing _pw. If any operation with it fails
    with greenlet error, call _full_restart() which kills _pw, then
    start a new one.

    Returns the Playwright instance, or None on failure.
    """
    global _pw
    if _pw is not None:
        return _pw
    return _start_pw()


def _launch_browser(storage_state=None, headless=False):
    """Launch browser with a PERSISTENT profile. Returns True on success.

    Uses launch_persistent_context so cookies, localStorage, IndexedDB,
    service workers, and the HSTS cache survive across restarts — critical
    for SSO sites whose Secure session cookies would otherwise be dropped
    over http redirect chains (a fresh temp context has no HSTS memory, so
    it stays on http and the Secure cookie never lands).

    headless defaults to False: the gateway typically runs on the user's own
    desktop, so a visible window lets the user complete SSO/2FA by hand.
    `storage_state` is accepted for backward compat but ignored — the
    persistent profile already carries the session.
    """
    global _browser_instance, _context, _page, _mode, _channel

    pw = _ensure_pw()
    if pw is None:
        return False

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    channels = ["chrome", "msedge", None] if _channel is None else [_channel]
    for ch in channels:
        try:
            launch_kwargs = {
                "headless": headless,
                "viewport": {"width": 1280, "height": 720},
                "ignore_https_errors": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if ch is not None:
                launch_kwargs["channel"] = ch
            ctx = pw.chromium.launch_persistent_context(str(_PROFILE_DIR), **launch_kwargs)
            _browser_instance = ctx  # BrowserContext (persistent mode)
            _context = ctx
            # Hide automation signals so enterprise SSO doesn't flag the session.
            _context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            # Log SSO/login network responses (SSO/passport pages only).
            def _log_resp(resp):
                try:
                    u = resp.url
                    if not any(k in u for k in ("sso", "passport", "signin", "signout", "ticket", "login")):
                        return
                    parts = [f"{resp.status}", u[:140]]
                    try:
                        hdrs = resp.headers
                        loc = hdrs.get("location")
                        sc = hdrs.get("set-cookie")
                        if loc:
                            parts.append(f"-> {loc[:140]}")
                        if sc:
                            parts.append(f"Set-Cookie: {sc[:200]}")
                    except Exception:
                        pass
                    logger.info("[net] %s", " | ".join(parts))
                except Exception:
                    logger.debug("net capture handler failed", exc_info=True)
            _context.on("response", _log_resp)

            _page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _channel = ch
            _mode = "persistent"
            logger.info("Browser launched (persistent): channel=%s", ch or "bundled-chromium")
            return True
        except Exception as e:
            if ch is None:
                logger.error("Failed to launch browser: %s", e)
            else:
                logger.info("Channel '%s' not available: %s", ch, e)
            continue

    _cleanup_browser()
    return False


def _ensure_browser() -> bool:
    """Ensure a usable browser is connected. Default: CDP-managed real Chrome.

    Prefers a CDP-managed system Chrome/Edge (dedicated cdp-profile + debug
    port) because real Chrome carries HSTS memory, so Secure SSO cookies
    survive the http->https upgrade on SSO callbacks — the persistent
    Playwright context drops them and can't hold SSO sessions. Falls back to
    a persistent Playwright context only if CDP is unavailable (no system
    Chrome or debug port unreachable).
    """
    if _browser_instance and _page:
        try:
            if _mode == "persistent":
                # BrowserContext has no is_connected(); check the underlying browser.
                browser = _browser_instance.browser
                if browser and browser.is_connected() and _pw_alive():
                    return True
            else:
                if _browser_instance.is_connected() and _pw_alive():
                    return True
        except Exception:
            pass
        _cleanup_browser()

    if not _PW_OK:
        return False

    if _ensure_cdp_browser():
        return True

    logger.warning("CDP browser unavailable, falling back to persistent Playwright context")
    return _launch_browser()


def _cleanup_browser():
    """Clean up browser context and page. Playwright process is left alive
    but may be dead due to greenlet exit — _ensure_pw() handles restart."""
    global _browser_instance, _context, _page, _mode
    try:
        if _context:
            _context.close()
    except Exception:
        pass
    try:
        if _browser_instance:
            _browser_instance.close()
    except Exception:
        pass
    _browser_instance = None
    _context = None
    _page = None
    _mode = None


# Candidate system Chrome/Edge executables, in preference order.
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_system_chrome():
    """Locate a system Chrome or Edge executable. Returns absolute path or None.

    Preference order: well-known install paths, then PATH lookup (which/where).
    """
    for cand in _CHROME_CANDIDATES:
        try:
            if Path(cand).is_file():
                return cand
        except Exception:
            continue
    for name in ("chrome.exe", "chrome", "msedge.exe", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _cdp_port_open(timeout=0.75):
    """True if a Chrome remote-debugging endpoint is already answering on 9222."""
    try:
        with urllib.request.urlopen(f"{_CDP_URL}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _theme_launch_args(dark: bool) -> list[str]:
    # Chrome UI follows the OS theme by default; the snapped panel should
    # match the desktop app instead. No inverse flag exists — light means
    # "follow the OS" (a dark-OS machine still shows a dark Chrome UI).
    return ["--force-dark-mode"] if dark else []


def _appearance_dark() -> bool:
    try:
        return bool(json.loads(_APPEARANCE_FILE.read_text(encoding="utf-8"))["dark"])
    except Exception:
        return False


def set_appearance(dark: bool) -> dict:
    _APPEARANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _APPEARANCE_FILE.write_text(json.dumps({"dark": bool(dark)}), encoding="utf-8")
    return {"ok": True, "dark": bool(dark)}


def _launch_cdp_chrome():
    """Launch a detached real Chrome with the CDP profile + debug port.

    Returns True if the debug endpoint becomes reachable, False otherwise.
    The process is detached: it outlives xihe so the cdp-profile login state
    persists across xihe restarts.
    """
    _CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    exe = _find_system_chrome()
    if not exe:
        logger.warning("No system Chrome/Edge found for CDP launch")
        return False
    args = [
        exe,
        f"--remote-debugging-port={_CDP_PORT}",
        f"--user-data-dir={_CDP_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        # restart taskkills the previous instance, so Chrome boots as if after
        # a crash and nags "restore pages?" — restore silently instead, and
        # pass no startup URL: with one, the blank tab sat IN FRONT of the
        # restored session, so the panel showed about:blank even after the
        # user clicked 恢复.
        "--restore-last-session",
        *_theme_launch_args(_appearance_dark()),
    ]
    try:
        # Detached so it survives xihe exit; Windows-only flags guarded by hasattr.
        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS
        proc = subprocess.Popen(args, close_fds=True, creationflags=creationflags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning("Failed to launch CDP Chrome (%s): %s", exe, e)
        return False
    # Wait for the debug port to come up.
    for _ in range(40):
        if _cdp_port_open(timeout=0.25):
            logger.info("CDP Chrome launched: %s (port %d)", exe, _CDP_PORT)
            # pid written on the success path only: a second Popen against an
            # already-running profile just signals the real browser and exits,
            # so a pid captured there would be a transient that poisons
            # window resolution (desktop browser panel finds the window by pid).
            try:
                (_BROWSER_DIR / "cdp.pid").write_text(str(proc.pid))
            except Exception:
                logger.debug("cdp.pid write failed", exc_info=True)
            return True
        import time as _time
        _time.sleep(0.2)
    logger.warning("CDP Chrome launched but debug port never answered")
    return False


def _ensure_cdp_browser():
    """Connect to (launching if needed) the CDP-managed Chrome. Returns bool.

    MUST run on the browser worker thread (touches _pw which is thread-affine).
    On success sets _browser_instance/_context/_page/_mode='cdp'. Returns False
    if no system Chrome or connection fails — caller falls back to persistent.
    """
    global _browser_instance, _context, _page, _pw, _mode
    if not _cdp_port_open():
        if not _launch_cdp_chrome():
            return False
    try:
        pw = _ensure_pw()
        if pw is None:
            return False
        try:
            _browser_instance = pw.chromium.connect_over_cdp(_CDP_URL)
        except Exception as e:
            if "cannot switch to a different thread" in str(e):
                _full_restart()
                pw = _ensure_pw()
                if pw is None:
                    return False
                _browser_instance = pw.chromium.connect_over_cdp(_CDP_URL)
            else:
                raise
        contexts = _browser_instance.contexts
        if contexts:
            _context = contexts[0]
            pages = _context.pages
            _page = pages[0] if pages else _context.new_page()
        else:
            _context = _browser_instance.new_context()
            _page = _context.new_page()
        _mode = "cdp"
        logger.info("Connected to CDP-managed browser")
        return True
    except Exception as e:
        logger.warning("CDP connect failed: %s", e)
        _cleanup_browser()
        return False


def _full_restart():
    """Nuclear option: kill everything including _pw, start fresh.

    MUST run on the browser worker thread — it touches _pw, which is
    thread-affine. Currently only called from within _browser_connect's
    worker closure. Clears browser/context/page and stops _pw; the next
    _ensure_pw() (also on the worker) starts a fresh Playwright instance.
    """
    global _browser_instance, _context, _page, _pw, _mode, _channel
    logger.warning("Full Playwright restart — greenlet thread died")
    _cleanup_browser()
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None
    _channel = None
    logger.info("Playwright fully stopped; will restart on next browser call")


def shutdown_browser():
    """Fully shut down Playwright and stop the browser worker. Process-exit only."""
    global _browser_thread, _browser_thread_ident, _pw
    if _browser_thread is not None and _browser_queue is not None:
        if _browser_thread.is_alive():
            _browser_queue.put(_SENTINEL)
            _browser_thread.join(timeout=10)
        _browser_thread = None
        _browser_thread_ident = None
    # Best-effort: ensure _pw is stopped even if the worker never started or
    # died before processing the sentinel (stop() may raise if greenlet is gone).
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None


def _require_page():
    """Return the current page, or None if no browser page is loaded.

    Note: this cannot reliably detect Playwright greenlet death because
    Page.url is a cached property that doesn't go through the greenlet.
    Greenlet death is caught at the point of actual browser operations
    and handled by the dispatch-level post-processing in ToolRegistry.
    """
    if _page is None:
        return None
    try:
        _ = _page.url
        return _page
    except Exception:
        return None


def _with_page(fn):
    """Decorator: inject page as first arg, return error if no page loaded.

    The page lookup and fn body run on the browser worker thread — Playwright
    objects are thread-affine, so `page` never crosses back to the agent thread.
    """
    @functools.wraps(fn)
    def wrapper(args, **kw):
        def _body():
            page = _require_page()
            if not page:
                return tool_error("No page loaded. Use browser_navigate first.")
            return fn(page, args, **kw)
        return _run_on_browser_thread(_body)
    return wrapper


_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio",
    "combobox", "searchbox", "slider", "spinbutton",
    "switch", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "treeitem", "option", "heading",
}


def _build_accessibility_tree(node, depth=0, counter=None) -> list[str]:
    if counter is None:
        counter = {"n": 0}

    lines = []
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    checked = node.get("checked")
    disabled = node.get("disabled", False)

    ref = ""
    if role in _INTERACTIVE_ROLES and name:
        counter["n"] += 1
        ref = f" [@e{counter['n']}]"

    indent = "  " * depth
    parts = [f"{indent}{role}{ref}"]

    if name:
        parts[0] += f" '{name}'"
    if value:
        parts[0] += f" value='{value[:100]}'"
    if checked is not None:
        parts[0] += f" checked={checked}"
    if disabled:
        parts[0] += " [disabled]"

    lines.append(parts[0])

    for child in node.get("children", []):
        lines.extend(_build_accessibility_tree(child, depth + 1, counter))

    return lines


def _snapshot_accessibility_tree(page) -> str:
    try:
        tree = page.accessibility.snapshot()
        if not tree:
            return _snapshot_inner_text(page)
        lines = _build_accessibility_tree(tree)
        text = "\n".join(lines)
        if len(text) > 30000:
            head = text[:24000]
            tail = text[-5000:]
            omitted = len(text) - 29000
            text = head + f"\n...[truncated, {omitted} chars omitted]...\n" + tail
        return text
    except Exception:
        return _snapshot_inner_text(page)


def _snapshot_inner_text(page) -> str:
    try:
        text = page.inner_text("body")
        if len(text) > 30000:
            head = text[:24000]
            tail = text[-5000:]
            omitted = len(text) - 29000
            text = head + f"\n...[truncated, {omitted} chars omitted]...\n" + tail
        return text
    except Exception as e:
        return f"(snapshot failed: {e})"


def _resolve_ref(page, ref: str):
    if not ref.startswith("@"):
        return None
    try:
        tree = page.accessibility.snapshot()
        if not tree:
            return None
        counter = {"n": 0}
        return _find_by_ref(tree, ref, counter)
    except Exception:
        return None


def _find_by_ref(node, target_ref: str, counter: dict) -> dict | None:
    role = node.get("role", "")
    name = node.get("name", "")

    if role in _INTERACTIVE_ROLES and name:
        counter["n"] += 1
        ref_id = f"@e{counter['n']}"
        if ref_id == target_ref:
            return {"role": role, "name": name}

    for child in node.get("children", []):
        result = _find_by_ref(child, target_ref, counter)
        if result:
            return result

    return None


def _resolve_ref_to_locator(page, ref: str, selector: str = ""):
    """Resolve ref/selector to a Locator, or return (None, error_msg)."""
    if ref:
        info = _resolve_ref(page, ref)
        if not info:
            return None, f"Element {ref} not found. Use browser_snapshot to see available elements."
        return page.get_by_role(info["role"], name=info["name"]).first, None
    if selector:
        return page.locator(selector).first, None
    return None, "ref or selector is required"


def _browser_navigate(args: dict, **kw) -> str:
    url = args.get("url", "")
    if not url:
        return tool_error("url is required")
    if not url.startswith("http"):
        url = "https://" + url

    def _nav():
        if not _ensure_browser():
            return tool_error("No browser available. Install playwright: pip install playwright && playwright install chromium")
        try:
            _page.goto(url, timeout=15000, wait_until="domcontentloaded")
            final_url = _page.url
            title = _page.title()
            result = {"success": True, "url": final_url, "title": title}
            low = final_url.lower()
            if any(k in low for k in ("passport", "/signin", "/signout", "/login", "sso", "cas/login")):
                result["sso_hint"] = (
                    "Landed on a login/SSO page. The browser is CDP-managed real "
                    "Chrome and the session persists in "
                    "${AGENT_HOME}/browser/cdp-profile. Ask the user to scan / "
                    "complete login in the open Chrome window, then call "
                    "browser_snapshot to continue. Do NOT relaunch Chrome or fall "
                    "back to Playwright."
                )
            return tool_result(**result)
        except Exception as e:
            return tool_error(f"Navigation failed: {e}")

    return _run_on_browser_thread(_nav)


@_with_page
def _browser_back(page, args: dict, **kw) -> str:
    try:
        page.go_back(timeout=10000, wait_until="domcontentloaded")
        return tool_result(success=True, url=page.url, title=page.title())
    except Exception as e:
        return tool_error(f"Back navigation failed: {e}")


@_with_page
def _browser_forward(page, args: dict, **kw) -> str:
    try:
        page.go_forward(timeout=10000, wait_until="domcontentloaded")
        return tool_result(success=True, url=page.url, title=page.title())
    except Exception as e:
        return tool_error(f"Forward navigation failed: {e}")


@_with_page
def _browser_reload(page, args: dict, **kw) -> str:
    try:
        page.reload(timeout=15000, wait_until="domcontentloaded")
        return tool_result(success=True, url=page.url, title=page.title())
    except Exception as e:
        return tool_error(f"Reload failed: {e}")


@_with_page
def _browser_click(page, args: dict, **kw) -> str:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    text_match = args.get("text", "")

    if ref:
        locator, err = _resolve_ref_to_locator(page, ref)
        if err:
            return tool_error(err)
        try:
            locator.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            return tool_result(success=True, url=page.url)
        except Exception as e:
            return tool_error(f"Click on {ref} failed: {e}")

    if not selector and not text_match:
        return tool_error("ref, selector, or text is required")

    try:
        if text_match:
            page.click(f"text={text_match}", timeout=5000)
        else:
            page.click(selector, timeout=5000)
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        return tool_result(success=True, url=page.url)
    except Exception as e:
        return tool_error(f"Click failed: {e}")


@_with_page
def _browser_type(page, args: dict, **kw) -> str:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    text = args.get("text", "")
    submit = args.get("submit", False)

    if not text:
        return tool_error("text is required")
    if not ref and not selector:
        return tool_error("ref or selector is required")

    target = selector
    if ref:
        info = _resolve_ref(page, ref)
        if not info:
            return tool_error(f"Element {ref} not found. Use browser_snapshot to see available elements.")
        if info["role"] in ("textbox", "searchbox", "combobox"):
            target = f"[aria-label='{info['name']}']" if info["name"] else selector

    try:
        page.fill(target, text, timeout=5000)
        if submit:
            page.press(target, "Enter")
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        return tool_result(success=True, url=page.url)
    except Exception as e:
        return tool_error(f"Type failed: {e}")


@_with_page
def _browser_press(page, args: dict, **kw) -> str:
    key = args.get("key", "")
    if not key:
        return tool_error("key is required")
    try:
        page.keyboard.press(key)
        page.wait_for_load_state("domcontentloaded", timeout=3000)
        return tool_result(success=True, url=page.url)
    except Exception as e:
        return tool_error(f"Press failed: {e}")


@_with_page
def _browser_scroll(page, args: dict, **kw) -> str:
    direction = args.get("direction", "down")
    amount = int(args.get("amount", 500))
    try:
        delta = amount if direction == "down" else -amount
        page.mouse.wheel(0, delta)
        return tool_result(success=True, url=page.url)
    except Exception as e:
        return tool_error(f"Scroll failed: {e}")


@_with_page
def _browser_hover(page, args: dict, **kw) -> str:
    ref = args.get("ref", "")
    selector = args.get("selector", "")

    if ref:
        locator, err = _resolve_ref_to_locator(page, ref)
        if err:
            return tool_error(err)
        try:
            locator.hover(timeout=5000)
            return tool_result(success=True, hovered=ref, url=page.url)
        except Exception as e:
            return tool_error(f"Hover on {ref} failed: {e}")

    if not selector:
        return tool_error("ref or selector is required")

    try:
        page.hover(selector, timeout=5000)
        return tool_result(success=True, hovered=selector, url=page.url)
    except Exception as e:
        return tool_error(f"Hover failed: {e}")


@_with_page
def _browser_select(page, args: dict, **kw) -> str:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    values = args.get("values", [])

    if isinstance(values, str):
        values = [values]

    if not values:
        return tool_error("values is required (string or array of strings)")

    target = selector
    if ref:
        info = _resolve_ref(page, ref)
        if not info:
            return tool_error(f"Element {ref} not found. Use browser_snapshot to see available elements.")
        name = info["name"]
        target = f"select[aria-label='{name}']" if name else selector

    if not target:
        return tool_error("ref or selector is required")

    try:
        page.select_option(target, values, timeout=5000)
        return tool_result(success=True, selected=values, url=page.url)
    except Exception as e:
        return tool_error(f"Select failed: {e}")


@_with_page
def _browser_drag(page, args: dict, **kw) -> str:
    source_ref = args.get("source_ref", "")
    target_ref = args.get("target_ref", "")
    source_selector = args.get("source_selector", "")
    target_selector = args.get("target_selector", "")

    if not source_ref and not source_selector:
        return tool_error("source_ref or source_selector is required")
    if not target_ref and not target_selector:
        return tool_error("target_ref or target_selector is required")

    try:
        # Resolve source
        if source_ref:
            src_loc, err = _resolve_ref_to_locator(page, source_ref)
            if err:
                return tool_error(f"Source element {source_ref} not found.")
        else:
            src_loc = page.locator(source_selector).first

        # Resolve target
        if target_ref:
            tgt_loc, err = _resolve_ref_to_locator(page, target_ref)
            if err:
                return tool_error(f"Target element {target_ref} not found.")
        else:
            tgt_loc = page.locator(target_selector).first

        src_loc.drag_to(tgt_loc, timeout=5000)
        return tool_result(success=True, url=page.url)
    except Exception as e:
        return tool_error(f"Drag failed: {e}")


@_with_page
def _browser_check(page, args: dict, **kw) -> str:
    ref = args.get("ref", "")
    selector = args.get("selector", "")

    if ref:
        locator, err = _resolve_ref_to_locator(page, ref)
        if err:
            return tool_error(err)
        try:
            locator.check(timeout=5000)
            return tool_result(success=True, checked=ref)
        except Exception as e:
            return tool_error(f"Check {ref} failed: {e}")

    if not selector:
        return tool_error("ref or selector is required")
    try:
        page.check(selector, timeout=5000)
        return tool_result(success=True, checked=selector)
    except Exception as e:
        return tool_error(f"Check failed: {e}")


@_with_page
def _browser_uncheck(page, args: dict, **kw) -> str:
    ref = args.get("ref", "")
    selector = args.get("selector", "")

    if ref:
        locator, err = _resolve_ref_to_locator(page, ref)
        if err:
            return tool_error(err)
        try:
            locator.uncheck(timeout=5000)
            return tool_result(success=True, unchecked=ref)
        except Exception as e:
            return tool_error(f"Uncheck {ref} failed: {e}")

    if not selector:
        return tool_error("ref or selector is required")
    try:
        page.uncheck(selector, timeout=5000)
        return tool_result(success=True, unchecked=selector)
    except Exception as e:
        return tool_error(f"Uncheck failed: {e}")


@_with_page
def _browser_snapshot(page, args: dict, **kw) -> str:
    try:
        url = page.url
        title = page.title()
        text = _snapshot_accessibility_tree(page)
        from tools.redact import redact_sensitive_text
        text = redact_sensitive_text(text)
        return tool_result(url=url, title=title, text=text)
    except Exception as e:
        return tool_error(f"Snapshot failed: {e}")


@_with_page
def _browser_screenshot(page, args: dict, **kw) -> str:
    try:
        path = args.get("path", "")
        if not path:
            # Default to an absolute path under ${AGENT_HOME}/browser/ so we
            # never drop screenshot.png in the project root (cwd). Timestamped
            # so repeated shots don't clobber each other.
            import time as _time
            _BROWSER_DIR.mkdir(parents=True, exist_ok=True)
            path = str(_BROWSER_DIR / f"screenshot_{int(_time.time())}.png")
        page.screenshot(path=path, full_page=False)
        return tool_result(success=True, path=path)
    except Exception as e:
        return tool_error(f"Screenshot failed: {e}")


@_with_page
def _browser_console(page, args: dict, **kw) -> str:
    try:
        logs = page.evaluate("""() => {
            const entries = [];
            if (window.__console_logs) {
                return window.__console_logs.slice(-50);
            }
            return entries;
        }""")
        errors = page.evaluate("""() => {
            if (window.__js_errors) {
                return window.__js_errors.slice(-20);
            }
            return [];
        }""")

        error_text = ""
        try:
            error_elements = page.query_selector_all("[role='alert'], .error, .alert-danger")
            if error_elements:
                error_text = "\n".join(el.inner_text() for el in error_elements[:5])
        except Exception:
            pass

        return tool_result(
            console_logs=logs or [],
            js_errors=errors or [],
            visible_errors=error_text or None,
            url=page.url,
        )
    except Exception as e:
        return tool_error(f"Console check failed: {e}")


def _browser_vision(args: dict, **kw) -> str:
    prompt = args.get("prompt", "Describe what you see on this page, including any text, buttons, forms, and interactive elements.")

    def _capture():
        # Runs on the browser worker — returns {"path", "url"} or None
        page = _require_page()
        if not page:
            return None
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        page.screenshot(path=path, full_page=False)
        return {"path": path, "url": page.url}

    try:
        captured = _run_on_browser_thread(_capture)
    except Exception as e:
        return tool_error(f"Browser vision failed: {e}")

    if not captured:
        return tool_error("No page loaded. Use browser_navigate first.")

    # LLM analysis runs on the agent thread so it does NOT occupy the single
    # browser worker while a slow vision call is in flight.
    try:
        from tools.vision_tools import _vision_analyze
        result = json.loads(_vision_analyze({"image": captured["path"], "prompt": prompt}))
        if result.get("success") or result.get("analysis"):
            analysis = result.get("analysis", result.get("error", ""))
            return tool_result(analysis=analysis, url=captured["url"])
        return tool_error(f"Vision analysis failed: {result.get('error', 'unknown')}")
    except ImportError:
        return tool_error("Vision analysis not available. Install vision dependencies.")
    except Exception as e:
        return tool_error(f"Browser vision failed: {e}")


def _browser_wait(args: dict, **kw) -> str:
    # Peek timeout to derive the worker wait budget (covers the time.sleep fallback)
    try:
        timeout_ms = int(args.get("timeout", 10000))
    except Exception:
        timeout_ms = 10000

    def _run():
        page = _require_page()
        if not page:
            return tool_error("No page loaded. Use browser_navigate first.")
        return _wait_body(page, args)

    op_timeout = max(_BROWSER_OP_TIMEOUT, timeout_ms / 1000 + 15)
    return _run_on_browser_thread(_run, timeout=op_timeout)


def _wait_body(page, args: dict) -> str:
    timeout = int(args.get("timeout", 10000))

    # Wait for text to appear
    text = args.get("text", "")
    if text:
        try:
            page.wait_for_selector(f"text={text}", timeout=timeout, state="visible")
            return tool_result(success=True, waited="text", text=text)
        except Exception as e:
            return tool_error(f"Wait for text '{text}' timed out: {e}")

    # Wait for selector / element
    selector = args.get("selector", "")
    if selector:
        state = args.get("state", "visible")
        try:
            page.wait_for_selector(selector, timeout=timeout, state=state)
            return tool_result(success=True, waited="selector", selector=selector, state=state)
        except Exception as e:
            return tool_error(f"Wait for selector '{selector}' timed out: {e}")

    # Wait for URL pattern
    url_pattern = args.get("url", "")
    if url_pattern:
        try:
            page.wait_for_url(url_pattern, timeout=timeout)
            return tool_result(success=True, waited="url", url=page.url)
        except Exception as e:
            return tool_error(f"Wait for URL '{url_pattern}' timed out: {e}")

    # Wait for load state
    load_state = args.get("load_state", "")
    if load_state:
        try:
            page.wait_for_load_state(load_state, timeout=timeout)
            return tool_result(success=True, waited="load_state", state=load_state)
        except Exception as e:
            return tool_error(f"Wait for load state '{load_state}' timed out: {e}")

    # Wait for JS function
    js_fn = args.get("function", "")
    if js_fn:
        try:
            page.wait_for_function(js_fn, timeout=timeout)
            return tool_result(success=True, waited="function")
        except Exception as e:
            return tool_error(f"Wait for function timed out: {e}")

    # Just sleep for timeout ms
    import time
    time.sleep(timeout / 1000)
    return tool_result(success=True, waited="timeout", ms=timeout)


def _browser_tab_new(args: dict, **kw) -> str:
    url = args.get("url", "")

    def _new_tab():
        global _page
        if not _ensure_browser():
            return tool_error("No browser available.")
        try:
            context = _page.context
            new_page = context.new_page()
            if url:
                new_page.goto(url, timeout=15000, wait_until="domcontentloaded")
            _page = new_page
            return tool_result(success=True, url=new_page.url, title=new_page.title() if url else "")
        except Exception as e:
            return tool_error(f"New tab failed: {e}")

    return _run_on_browser_thread(_new_tab)


@_with_page
def _browser_tab_list(page, args: dict, **kw) -> str:
    try:
        context = page.context
        tabs = []
        for i, p in enumerate(context.pages):
            tabs.append({
                "index": i,
                "url": p.url,
                "title": p.title() if p.url != "about:blank" else "",
                "active": p == _page,
            })
        return tool_result(tabs=tabs, count=len(tabs))
    except Exception as e:
        return tool_error(f"List tabs failed: {e}")


@_with_page
def _browser_tab_switch(page, args: dict, **kw) -> str:
    global _page
    index = args.get("index")
    if index is None:
        return tool_error("index is required")
    try:
        context = page.context
        idx = int(index)
        if idx < 0 or idx >= len(context.pages):
            return tool_error(f"Tab index {idx} out of range (0-{len(context.pages) - 1})")
        _page = context.pages[idx]
        _page.bring_to_front()
        return tool_result(success=True, url=_page.url, title=_page.title(), index=idx)
    except Exception as e:
        return tool_error(f"Switch tab failed: {e}")


@_with_page
def _browser_tab_close(page, args: dict, **kw) -> str:
    global _page
    index = args.get("index")
    try:
        context = page.context
        if index is not None:
            idx = int(index)
            if idx < 0 or idx >= len(context.pages):
                return tool_error(f"Tab index {idx} out of range")
            target = context.pages[idx]
            target.close()
            if target == _page:
                if context.pages:
                    _page = context.pages[0]
                else:
                    _page = None
        else:
            page.close()
            if context.pages:
                _page = context.pages[0]
            else:
                _page = None
        return tool_result(success=True, remaining=len(context.pages) if context.pages else 0)
    except Exception as e:
        return tool_error(f"Close tab failed: {e}")


@_with_page
def _browser_eval(page, args: dict, **kw) -> str:
    expression = args.get("expression", "")
    if not expression:
        return tool_error("expression is required")
    try:
        result = page.evaluate(expression)
        if result is None:
            return tool_result(success=True, result=None)
        if isinstance(result, (dict, list)):
            serialized = json.dumps(result, ensure_ascii=False, default=str)
            if len(serialized) > 30000:
                serialized = serialized[:24000] + f"\n...[truncated, {len(serialized) - 24000} chars omitted]..."
            return tool_result(success=True, result=serialized)
        return tool_result(success=True, result=str(result))
    except Exception as e:
        return tool_error(f"Eval failed: {e}")


@_with_page
def _browser_upload(page, args: dict, **kw) -> str:
    selector = args.get("selector", "")
    files = args.get("files", [])
    if isinstance(files, str):
        files = [files]
    if not selector:
        return tool_error("selector is required")
    if not files:
        return tool_error("files is required (path or array of paths)")
    try:
        page.set_input_files(selector, files)
        return tool_result(success=True, uploaded=len(files), selector=selector)
    except Exception as e:
        return tool_error(f"Upload failed: {e}")


@_with_page
def _browser_frame(page, args: dict, **kw) -> str:
    selector = args.get("selector", "")
    name = args.get("name", "")
    action = args.get("action", "switch")

    if action == "main":
        return tool_result(success=True, frame="main")

    if not selector and not name:
        frames = []
        for f in page.frames:
            frames.append({
                "name": f.name,
                "url": f.url,
                "is_main": f == page.main_frame,
            })
        return tool_result(frames=frames, count=len(frames))

    try:
        if name:
            frame = page.frame(name=name)
        elif selector:
            frame = page.frame_locator(selector).frame
        else:
            return tool_error("selector or name is required")

        if not frame:
            return tool_error(f"Frame not found. Available: {[f.name for f in page.frames]}")

        return tool_result(success=True, frame=frame.name or frame.url, url=frame.url)
    except Exception as e:
        return tool_error(f"Frame switch failed: {e}")


@_with_page
def _browser_cookies(page, args: dict, **kw) -> str:
    action = args.get("action", "list")
    context = page.context

    try:
        if action == "list":
            cookies = context.cookies()
            return tool_result(cookies=cookies, count=len(cookies))
        elif action == "set":
            name = args.get("name", "")
            value = args.get("value", "")
            domain = args.get("domain", "")
            path = args.get("path", "/")
            if not name:
                return tool_error("name is required")
            cookie = {"name": name, "value": value, "path": path}
            if domain:
                cookie["domain"] = domain
            context.add_cookies([cookie])
            return tool_result(success=True, action="set", name=name)
        elif action == "clear":
            context.clear_cookies()
            return tool_result(success=True, action="clear")
        else:
            return tool_error(f"Unknown action: {action}. Use: list, set, clear")
    except Exception as e:
        return tool_error(f"Cookies operation failed: {e}")


@_with_page
def _browser_state_save(page, args: dict, **kw) -> str:
    name = args.get("name", "default")
    if not name:
        return tool_error("name is required")

    safe_name = re.sub(r'[^\w\-.]', '_', name)
    _STATES_DIR.mkdir(parents=True, exist_ok=True)
    state_path = _STATES_DIR / f"{safe_name}.json"

    try:
        state = page.context.storage_state()
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        cookie_count = len(state.get("cookies", []))
        origin_count = len(state.get("origins", []))
        return tool_result(
            success=True,
            name=name,
            path=str(state_path),
            cookies=cookie_count,
            origins=origin_count,
            message=f"Browser state '{name}' saved ({cookie_count} cookies, {origin_count} origins).",
        )
    except Exception as e:
        return tool_error(f"Failed to save state: {e}")


def _browser_state_load(args: dict, **kw) -> str:
    """Load a previously saved browser state (cookies + localStorage).

    Closes the current browser and reopens with the saved state.
    """
    name = args.get("name", "default")
    if not name:
        return tool_error("name is required")

    safe_name = re.sub(r'[^\w\-.]', '_', name)
    state_path = _STATES_DIR / f"{safe_name}.json"

    if not state_path.exists():
        available = []
        if _STATES_DIR.exists():
            available = [p.stem for p in _STATES_DIR.glob("*.json")]
        if available:
            return tool_error(
                f"State '{name}' not found. Available: {', '.join(available)}. "
                f"Use browser_login(url='...') to log in and browser_state_save(name='{name}') to save."
            )
        return tool_error(
            f"State '{name}' not found. No saved states exist. "
            f"Use browser_login(url='...') to log in first, then browser_state_save(name='{name}')."
        )

    try:
        state_json = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        return tool_error(f"Failed to read state file: {e}")

    def _load():
        # Ensure a browser (CDP preferred), then inject state in-place.
        # NOTE: do NOT _cleanup_browser + _launch_browser(storage_state) —
        # _launch_browser ignores storage_state (kept only for backward compat),
        # and cleanup+relaunch tears down the CDP-managed Chrome mid-session
        # (CDP connects to a real Chrome's existing context; it can't relaunch
        # with a storage_state like persistent can), causing "Page closed" on the
        # next navigate. Inject cookies + localStorage into the current context.
        if not _ensure_browser():
            return False
        try:
            cookies = state_json.get("cookies", [])
            if cookies:
                _context.add_cookies(cookies)
            origins = state_json.get("origins", [])
            if origins:
                ls_map = {}
                for o in origins:
                    origin = o.get("origin", "")
                    items = {
                        i["name"]: i["value"]
                        for i in o.get("localStorage", [])
                        if "name" in i and "value" in i
                    }
                    if origin and items:
                        ls_map[origin] = items
                if ls_map:
                    # init_script runs on each navigation, restoring localStorage
                    # per-origin (Playwright has no context.add_localStorage).
                    script = (
                        "const _m=" + json.dumps(ls_map) + ";"
                        "const _o=_m[location.origin];"
                        "if(_o){for(const[k,v] of Object.entries(_o))"
                        "{try{localStorage.setItem(k,v)}catch(e){}}}"
                    )
                    _context.add_init_script(script)
            return True
        except Exception as e:
            logger.warning("State injection failed: %s", e)
            return False

    if not _run_on_browser_thread(_load):
        return tool_error("Failed to launch browser for state load")

    cookie_count = len(state_json.get("cookies", []))
    origin_count = len(state_json.get("origins", []))
    return tool_result(
        success=True,
        name=name,
        cookies=cookie_count,
        origins=origin_count,
        message=f"Browser state '{name}' loaded ({cookie_count} cookies, {origin_count} origins).",
    )


def _browser_state_list(args: dict, **kw) -> str:
    """List all saved browser states."""
    _STATES_DIR.mkdir(parents=True, exist_ok=True)
    states = []
    for p in sorted(_STATES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            states.append({
                "name": p.stem,
                "cookies": len(data.get("cookies", [])),
                "origins": len(data.get("origins", [])),
                "size": p.stat().st_size,
            })
        except Exception:
            states.append({"name": p.stem, "error": "corrupt"})
    return tool_result(states=states, count=len(states))


def _browser_state_delete(args: dict, **kw) -> str:
    """Delete a saved browser state."""
    name = args.get("name", "")
    if not name:
        return tool_error("name is required")

    safe_name = re.sub(r'[^\w\-.]', '_', name)
    state_path = _STATES_DIR / f"{safe_name}.json"

    if not state_path.exists():
        return tool_error(f"State '{name}' not found.")

    try:
        state_path.unlink()
        return tool_result(success=True, name=name, message=f"State '{name}' deleted.")
    except Exception as e:
        return tool_error(f"Failed to delete state: {e}")


def _browser_connect(args: dict, **kw) -> str:
    """Connect to a running browser via Chrome DevTools Protocol.

    This lets you reuse a browser where you're already logged in,
    perfect for 2FA/SMS verification scenarios.

    To use this, start Chrome with remote debugging:
      Windows: chrome.exe --remote-debugging-port=9222
      macOS:   /Applications/Google\\ Chrome.app/.../Chrome --remote-debugging-port=9222
      Edge:    msedge.exe --remote-debugging-port=9222
    (9222 by default; set BROWSER_CDP_PORT to use a different port, e.g. when
    running multiple xihe instances on one machine.)
    """
    cdp_url = args.get("url", "")
    if not cdp_url:
        return tool_error(
            "CDP URL is required. "
            f"Start Chrome with --remote-debugging-port={_CDP_PORT} (set BROWSER_CDP_PORT to change), "
            f"then use url='http://127.0.0.1:{_CDP_PORT}'"
        )
    # Chrome's --remote-debugging-port binds IPv4 127.0.0.1; "localhost" often
    # resolves to IPv6 ::1 and gets ECONNREFUSED. Normalize to 127.0.0.1.
    cdp_url = cdp_url.replace("://localhost", "://127.0.0.1").replace("://[::1]", "://127.0.0.1")

    def _connect():
        global _browser_instance, _context, _page, _pw, _mode, _channel
        _cleanup_browser()
        try:
            pw = _ensure_pw()
            if pw is None:
                return tool_error("Failed to start Playwright")
            try:
                _browser_instance = pw.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                if "cannot switch to a different thread" in str(e):
                    _full_restart()
                    pw = _ensure_pw()
                    if pw is None:
                        return tool_error("Failed to restart Playwright after greenlet death")
                    _browser_instance = pw.chromium.connect_over_cdp(cdp_url)
                else:
                    raise

            contexts = _browser_instance.contexts
            if contexts:
                _context = contexts[0]
                pages = _context.pages
                if pages:
                    _page = pages[0]
                else:
                    _page = _context.new_page()
            else:
                _context = _browser_instance.new_context()
                _page = _context.new_page()

            _mode = "cdp"

            page_urls = []
            for ctx in _browser_instance.contexts:
                for p in ctx.pages:
                    try:
                        page_urls.append(p.url)
                    except Exception:
                        page_urls.append("(unknown)")

            return tool_result(
                success=True,
                mode="cdp",
                connected=True,
                pages=page_urls,
                page_count=len(page_urls),
                message=f"Connected to browser via CDP. {len(page_urls)} page(s) available.",
            )
        except Exception as e:
            _cleanup_browser()
            return tool_error(f"CDP connection failed: {e}. Make sure Chrome is running with --remote-debugging-port.")

    return _run_on_browser_thread(_connect)


def _browser_login(args: dict, **kw) -> str:
    """Navigate to a login page and take a screenshot for the user.

    Does NOT auto-fill credentials — the LLM uses browser_type/browser_click
    to fill in the form based on user input.
    """
    system_url = args.get("url", "")
    system_name = args.get("system", "")

    if not system_url:
        return tool_error("url is required (e.g. 'http://portal.example.internal')")

    def _login():
        if not _ensure_browser():
            return tool_error("Failed to start browser")

        page = _require_page()
        if not page:
            return tool_error("Failed to create browser page")

        try:
            page.goto(system_url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception as e:
            return tool_error(f"Navigation failed: {e}")

        import time as _time
        screenshot_name = f"login_{system_name or 'page'}_{int(_time.time())}.png"
        screenshot_path = str(_BROWSER_DIR / screenshot_name)
        try:
            page.screenshot(path=screenshot_path, full_page=False)
        except Exception:
            screenshot_path = None

        result = {
            "success": True,
            "url": page.url,
            "title": page.title(),
            "message": (
                f"Navigated to {page.url}. "
                f"Use browser_snapshot to see the page structure, "
                f"then help the user log in with browser_type and browser_click. "
                f"After login succeeds, call browser_state_save(name='{system_name or 'default'}') to persist the session."
            ),
        }
        if screenshot_path:
            result["screenshot"] = screenshot_path
            result["message"] += f" Send the screenshot to the user with send_image(image_url='{screenshot_path}')."

        return tool_result(**result)

    return _run_on_browser_thread(_login)


def _browser_close(args: dict, **kw) -> str:
    def _close():
        _cleanup_browser()
        return tool_result(success=True, action="closed")
    return _run_on_browser_thread(_close)


def _browser_logout(args: dict, **kw) -> str:
    """Clear login state.

    Default (light): clear cookies + localStorage for a domain, including the
    SSO parent domain so shared-domain cookies (e.g. .internal passport SSO)
    are removed too. wipe_profile=True (heavy): close the CDP Chrome and delete
    the profile dir(s) on disk, so the next browser_navigate boots a fresh,
    unlogged-in browser.
    """
    domain = args.get("domain", "")
    logout_url = args.get("url", "")
    wipe = bool(args.get("wipe_profile", False))

    def _do():
        # Heavy: nuke the on-disk profile so next launch is clean.
        if wipe:
            wiped = []
            _cleanup_browser()
            for d in (_CDP_PROFILE_DIR, _PROFILE_DIR):
                if d.exists():
                    try:
                        shutil.rmtree(d)
                        wiped.append(str(d))
                    except Exception as e:
                        logger.warning("Failed to wipe %s: %s", d, e)
            return tool_result(
                success=True, action="wipe_profile", wiped=wiped,
                message="Closed the browser and deleted the profile dir(s). "
                        "The next browser_navigate starts a fresh, unlogged-in Chrome.",
            )

        # Light: per-domain clear on the running context.
        if not _ensure_browser():
            return tool_error("No browser available")
        page = _require_page()
        if not page:
            return tool_error("No active page")
        context = page.context

        # Normalize target host: accept "portal.example.internal" or a full URL.
        tgt = domain
        if tgt and "://" in tgt:
            try:
                tgt = tgt.split("/")[2]
            except Exception:
                pass
        try:
            cur_host = page.url.split("/")[2] if "://" in (page.url or "") else ""
        except Exception:
            cur_host = ""
        if not tgt:
            tgt = cur_host
        # SSO parent domain (example.internal from portal.example.internal) so cookies set on the
        # parent / passport subdomain are cleared too.
        parent = ".".join(tgt.split(".")[-2:]) if tgt.count(".") >= 2 else ""

        def _host_matches(h):
            if not tgt:
                return False
            return (h == tgt or h.endswith("." + tgt)
                    or (parent and (h == parent or h.endswith("." + parent))))

        # Cookies: filter out matching, clear all, re-add the rest.
        cleared_cookies = 0
        kept = []
        try:
            for c in context.cookies():
                cdom = (c.get("domain") or "").lstrip(".")
                if _host_matches(cdom):
                    cleared_cookies += 1
                else:
                    kept.append(c)
            context.clear_cookies()
            if kept:
                context.add_cookies(kept)
        except Exception as e:
            return tool_error(f"Cookie clear failed: {e}")

        # localStorage: origin-scoped; clear on every page whose host matches.
        cleared_storages = []
        try:
            pages = context.pages
        except Exception:
            pages = [page]
        for p in pages:
            try:
                purl = p.url or ""
                if not purl.startswith("http"):
                    continue
                if _host_matches(purl.split("/")[2]):
                    p.evaluate("() => { try { localStorage.clear(); } catch(e){} }")
                    cleared_storages.append(purl.split("/")[2])
            except Exception:
                logger.debug("storage clear failed for one page", exc_info=True)

        msg = (f"Cleared {cleared_cookies} cookie(s) for {tgt}"
               + (f" (+parent {parent})" if parent else "")
               + (f"; localStorage on {cleared_storages}" if cleared_storages else "")
               + ".")
        nav_url = ""
        if logout_url:
            try:
                page.goto(logout_url, timeout=15000, wait_until="domcontentloaded")
                nav_url = page.url
                msg += f" Navigated to logout URL -> {nav_url}."
            except Exception as e:
                msg += f" (logout URL nav failed: {e})"
        else:
            try:
                page.reload(timeout=15000, wait_until="domcontentloaded")
            except Exception:
                logger.debug("post-logout reload failed", exc_info=True)
        return tool_result(
            success=True, action="clear", domain=tgt, parent=parent,
            cookies_cleared=cleared_cookies, localStorage_cleared=cleared_storages,
            logout_url=nav_url, message=msg,
        )

    return _run_on_browser_thread(_do)


registry.register(
    name="browser_navigate",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Navigate to a URL in the browser. Returns the page title and URL. "
                "After navigating, use browser_snapshot to see the page content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    handler=lambda args, **kw: _browser_navigate(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_snapshot",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": (
                "Get a structured snapshot of the current page using accessibility tree. "
                "Interactive elements (buttons, links, inputs) are shown with ref IDs like [@e1], [@e2]. "
                "Use these ref IDs with browser_click and browser_type to interact with elements."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_snapshot(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_click",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click on an element. Prefer using ref ID from browser_snapshot (e.g. '@e5') "
                "for precise targeting. Falls back to CSS selector or text matching."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot (e.g. '@e5')"},
                    "selector": {"type": "string", "description": "CSS selector (fallback if no ref)"},
                    "text": {"type": "string", "description": "Text to match (fallback if no ref/selector)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_click(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_type",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": (
                "Type text into an input field. Prefer using ref ID from browser_snapshot. "
                "Clears existing text before typing. Set submit=true to press Enter after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot (e.g. '@e3')"},
                    "selector": {"type": "string", "description": "CSS selector (fallback if no ref)"},
                    "text": {"type": "string", "description": "Text to type"},
                    "submit": {"type": "boolean", "description": "Press Enter after typing (default: false)"},
                },
                "required": ["text"],
            },
        },
    },
    handler=lambda args, **kw: _browser_type(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_scroll",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the page to reveal more content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction (default: down)"},
                    "amount": {"type": "integer", "description": "Scroll amount in pixels (default: 500)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_scroll(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_back",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_back",
            "description": "Navigate back to the previous page in browser history.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_back(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_press",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_press",
            "description": (
                "Press a keyboard key. Useful for submitting forms (Enter), "
                "navigating (Tab), or keyboard shortcuts (Control+a)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to press (e.g. Enter, Tab, Escape, Control+a)"},
                },
                "required": ["key"],
            },
        },
    },
    handler=lambda args, **kw: _browser_press(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_screenshot",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current page. Returns the file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional save path. If omitted, saves to ${AGENT_HOME}/browser/screenshot_<timestamp>.png (never the project root). Pass an absolute path only if you need it elsewhere."},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_screenshot(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_console",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_console",
            "description": (
                "Get browser console output and JavaScript errors. "
                "Useful for debugging web pages and checking for JS errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_console(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_vision",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_vision",
            "description": (
                "Take a screenshot of the current page and analyze it with vision AI. "
                "Use when the accessibility tree snapshot doesn't capture visual layout well "
                "(charts, images, complex layouts)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to look for in the screenshot (default: describe the page)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_vision(args, **kw),
    check_fn=_check_browser_vision,
    read_only=False,
)

registry.register(
    name="browser_close",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "Close the browser and release resources. Persistent profile is preserved.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_close(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_wait",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": (
                "Wait for a condition on the page. Supports waiting for text, selector, URL, "
                "load state, JS function, or a fixed timeout. Essential before interacting with "
                "dynamically loaded content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Wait for text to appear on page"},
                    "selector": {"type": "string", "description": "Wait for CSS selector to appear"},
                    "url": {"type": "string", "description": "Wait for URL to match pattern (glob)"},
                    "load_state": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "description": "Wait for load state"},
                    "function": {"type": "string", "description": "Wait for JS function to return truthy"},
                    "state": {"type": "string", "enum": ["visible", "hidden", "attached", "detached"], "description": "Element state to wait for (with selector, default: visible)"},
                    "timeout": {"type": "integer", "description": "Timeout in ms (default: 10000)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_wait(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_hover",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_hover",
            "description": (
                "Hover over an element. Useful for opening dropdown menus, "
                "revealing tooltip text, or triggering hover effects."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot (e.g. '@e3')"},
                    "selector": {"type": "string", "description": "CSS selector (fallback if no ref)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_hover(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_select",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": (
                "Select option(s) in a <select> dropdown. Use browser_snapshot to find the dropdown, "
                "then select by value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot"},
                    "selector": {"type": "string", "description": "CSS selector for the <select> element"},
                    "values": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Value(s) to select",
                    },
                },
                "required": ["values"],
            },
        },
    },
    handler=lambda args, **kw: _browser_select(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_tab_new",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_tab_new",
            "description": "Open a new browser tab, optionally with a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open in new tab (optional)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_tab_new(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_tab_list",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_tab_list",
            "description": "List all open browser tabs with their URLs and titles.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_tab_list(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_tab_switch",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_tab_switch",
            "description": "Switch to a different browser tab by index. Use browser_tab_list to see indices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Tab index (0-based)"},
                },
                "required": ["index"],
            },
        },
    },
    handler=lambda args, **kw: _browser_tab_switch(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_tab_close",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_tab_close",
            "description": "Close a browser tab. Closes current tab if no index specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Tab index to close (default: current tab)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_tab_close(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_eval",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_eval",
            "description": (
                "Execute JavaScript in the browser page and return the result. "
                "Use for extracting data, manipulating DOM, or calling page APIs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "JavaScript expression to evaluate"},
                },
                "required": ["expression"],
            },
        },
    },
    handler=lambda args, **kw: _browser_eval(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_upload",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_upload",
            "description": "Upload file(s) to a file input element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the file input element"},
                    "files": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "File path(s) to upload",
                    },
                },
                "required": ["selector", "files"],
            },
        },
    },
    handler=lambda args, **kw: _browser_upload(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_cookies",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_cookies",
            "description": (
                "Manage browser cookies. List all cookies, set a cookie, or clear all cookies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "set", "clear"],
                        "description": "Action: list (default), set, or clear",
                    },
                    "name": {"type": "string", "description": "Cookie name (for set action)"},
                    "value": {"type": "string", "description": "Cookie value (for set action)"},
                    "domain": {"type": "string", "description": "Cookie domain (for set action)"},
                    "path": {"type": "string", "description": "Cookie path (default: '/')"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_cookies(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_forward",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_forward",
            "description": "Navigate forward in browser history.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_forward(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_reload",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_reload",
            "description": "Reload the current page.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_reload(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_drag",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_drag",
            "description": "Drag an element to a target element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string", "description": "Source element ref from snapshot"},
                    "target_ref": {"type": "string", "description": "Target element ref from snapshot"},
                    "source_selector": {"type": "string", "description": "Source CSS selector (fallback)"},
                    "target_selector": {"type": "string", "description": "Target CSS selector (fallback)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_drag(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_check",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_check",
            "description": "Check a checkbox or radio button.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot"},
                    "selector": {"type": "string", "description": "CSS selector (fallback)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_check(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_uncheck",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_uncheck",
            "description": "Uncheck a checkbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot"},
                    "selector": {"type": "string", "description": "CSS selector (fallback)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_uncheck(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_frame",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_frame",
            "description": (
                "List or switch to iframe/frame on the page. Use action='main' to switch back to main frame. "
                "Call without arguments to list all frames."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the iframe element"},
                    "name": {"type": "string", "description": "Frame name attribute to switch to"},
                    "action": {"type": "string", "enum": ["switch", "main"], "description": "Action (default: switch, or 'main' to return)"},
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_frame(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_state_save",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_state_save",
            "description": (
                "Save the current browser login state (cookies + localStorage) to a named file. "
                "Use before closing the browser to preserve login sessions. "
                "Saved states can be loaded later with browser_state_load."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for this state (e.g. 'github', 'work-google'). Default: 'default'",
                    },
                },
            },
        },
    },
    handler=lambda args, **kw: _browser_state_save(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_state_load",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_state_load",
            "description": (
                "Load a previously saved browser state (cookies + localStorage). "
                "Closes the current browser and reopens with the saved login state. "
                "Use to switch between different accounts or restore login sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the saved state to load. Use browser_state_list to see available states.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    handler=lambda args, **kw: _browser_state_load(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_state_list",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_state_list",
            "description": "List all saved browser states with their cookie counts.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    handler=lambda args, **kw: _browser_state_list(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_state_delete",
    toolset="web",
    subagent_blocked=True,
    schema={
        "type": "function",
        "function": {
            "name": "browser_state_delete",
            "description": "Delete a saved browser state file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the state to delete"},
                },
                "required": ["name"],
            },
        },
    },
    handler=lambda args, **kw: _browser_state_delete(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_connect",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_connect",
            "description": (
                "ADVANCED manual override: connect to an EXTERNAL Chrome/Edge that is already "
                "running with --remote-debugging-port and that you did NOT launch. Rarely needed — "
                "by default browser_navigate already drives xihe's own CDP-managed Chrome, so do "
                "NOT call this for normal browsing or SSO login. Use only when the user explicitly "
                "points you at a separate browser they started themselves. NEVER kill, taskkill, or "
                "restart any Chrome process via the terminal tool — that destroys the user's regular "
                "browser (open tabs, unsaved work). Use the literal http://127.0.0.1:9222 (localhost "
                "resolves to IPv6 ::1 and gets refused)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "CDP URL of the external browser (e.g. 'http://127.0.0.1:9222')",
                    },
                },
                "required": ["url"],
            },
        },
    },
    handler=lambda args, **kw: _browser_connect(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_login",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_login",
            "description": (
                "Navigate to a system's login page (in the CDP-managed Chrome) and take a "
                "screenshot. Use this when the user needs to log into a web system "
                "(e.g. internal portals) and browser_navigate isn't specific enough. xihe's own "
                "Chrome is used — never suggest the user start Chrome themselves. "
                "After calling this, use browser_snapshot to see the login form, "
                "then browser_type/browser_click to fill in credentials the user provides. "
                "After login succeeds, call browser_state_save(name=system) for an explicit "
                "snapshot (the cdp-profile already persists the session across restarts)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Login page URL (e.g. 'http://portal.example.internal')",
                    },
                    "system": {
                        "type": "string",
                        "description": "System name for state saving (e.g. 'wiki')",
                    },
                },
                "required": ["url"],
            },
        },
    },
    handler=lambda args, **kw: _browser_login(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)

registry.register(
    name="browser_logout",
    toolset="web",
    schema={
        "type": "function",
        "function": {
            "name": "browser_logout",
            "description": (
                "Clear browser login state so a site is no longer remembered as logged in. "
                "Use when the user says they want to log out / sign out / clear login / "
                "退出登录 / 清理登录信息 for a system. Default clears cookies + localStorage "
                "for the given domain (and its SSO parent domain). Set wipe_profile=true to "
                "fully reset: close the CDP Chrome and delete the profile dir, so the next "
                "browser_navigate boots a fresh unlogged-in browser."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Domain to clear, e.g. 'portal.example.internal' (its SSO parent 'example.internal' "
                            "is cleared too). Accepts a full URL. If omitted, uses the "
                            "current page's domain."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional logout/signout URL to navigate to first (e.g. the site's signout endpoint).",
                    },
                    "wipe_profile": {
                        "type": "boolean",
                        "description": "If true, close the browser and delete the profile dir(s) for a complete reset. Logs out ALL sites, not just the domain.",
                    },
                },
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _browser_logout(args, **kw),
    check_fn=_check_browser,
    read_only=False,
)


# Records page operations performed in this CDP Chrome, producing a codegen-
# style Playwright script + actions with role/accessible-name metadata. The
# recorder (web_record_recorder.js) is persistently installed on the context
# but stays inert unless window.__pw_recording is true. It records trusted DOM
# events, so it captures human input AND the agent's own browser_* actions.
# Setup/collect run on the browser worker thread; the finish poll runs on the
# agent thread so the expose_binding callback (worker thread) can fire.

_BR_JS = (Path(__file__).parent / "web_record_recorder.js").read_text(encoding="utf-8")
_BR_BOUND = {"context": None}        # context the recorder is installed on
_BR_HOLDER = {"holder": None}        # current recording's buffer (set per call)
_BR_ACTIVE = {"on": False}           # master switch for popup-enablement
_BR_ENABLE_JS = ("(()=>{window.__pw_recording=true; window.__pw_done=false; window.__rec__=[];"
                 "if(window.__pw_mount) window.__pw_mount();})()")


def _br_on_rec(source, payload=None):
    """expose_binding callback — STORE only, never call page.* (deadlock-safe)."""
    h = _BR_HOLDER.get("holder")
    if h is None or not isinstance(payload, dict):
        return
    pid = payload.get("pageId") or "main"
    if pid not in h["pages"]:
        h["order"].append(pid)
    h["pages"][pid] = list(payload.get("actions") or [])
    if payload.get("done"):
        h["done"] = True
        logger.info("[browser_record] finish signal received (pageId=%s, actions=%d)",
                    pid, len(h["pages"].get(pid, [])))


def _br_install(ctx):
    """Install binding + recorder on ctx (once per ctx)."""
    if _BR_BOUND.get("context") is ctx:
        return
    try:
        ctx.expose_binding("__pw_rec", _br_on_rec)
    except Exception as e:
        logger.info("browser_record expose_binding note: %s", e)
    try:
        ctx.add_init_script(_BR_JS)
    except Exception as e:
        logger.info("browser_record add_init_script note: %s", e)
    # NOTE: we deliberately do NOT add a ctx.on("page") handler that calls page.*
    # (wait_for_load_state/evaluate) — that blocks/deadlocks the worker thread
    # (CLAUDE.md: never call page.* inside event callbacks), which prevented the
    # finish binding from firing and broke the 2nd popup. Popups are not
    # auto-recorded for now (main-page flow is).
    _BR_BOUND["context"] = ctx


def _br_record_on_context(ctx, page, url, drive=None):
    """Navigate, inject the recorder on THIS page, turn recording on.

    Page-scoped injection (add_init_script + evaluate) — reliable on
    connect_over_cdp contexts where context.add_init_script/expose_binding are
    unreliable (the finish button wouldn't appear when CDP already existed).
    """
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    try:
        page.add_init_script(_BR_JS)   # re-inject on future reloads
    except Exception as e:
        logger.info("browser_record page.add_init_script: %s", e)
    try:
        page.evaluate(_BR_JS)          # install in the current document NOW
    except Exception as e:
        logger.info("browser_record page.evaluate(recorder): %s", e)
    _BR_ACTIVE["on"] = True
    try:
        page.evaluate(_BR_ENABLE_JS)   # flag on + reset done/rec + mount button
    except Exception as e:
        logger.info("browser_record enable flag: %s", e)
    if drive:
        drive(page, ctx)


def _br_collect(ctx, holder):
    """Turn recording off, final-flush every page, return merged actions."""
    _BR_ACTIVE["on"] = False
    for pg in ctx.pages:
        try:
            pg.evaluate(
                "(()=>{window.__pw_recording=false;"
                "try{window.__pw_rec({pageId:window.__pw_page_id,actions:window.__rec__||[],done:false});}catch(e){}})()")
        except Exception:
            pass
    merged = []
    for pid in holder["order"]:
        merged.extend(holder["pages"].get(pid, []))
    return merged


def _br_q(s):
    return json.dumps("" if s is None else str(s))


def _br_emit(action):
    t = action.get("type")
    kind = action.get("kind", "css")
    sel = action.get("selector") or action.get("css")
    if kind == "css" or not sel:
        if t == "click": return f"    page.click({_br_q(sel)})"
        if t == "check": return f"    page.check({_br_q(sel)})"
        if t == "uncheck": return f"    page.uncheck({_br_q(sel)})"
        if t == "fill": return f"    page.fill({_br_q(sel)}, {_br_q(action.get('value'))})"
        if t == "press": return f"    page.press({_br_q(sel)}, {_br_q(action.get('key'))})"
        if t == "select": return f"    page.select_option({_br_q(sel)}, {_br_q(action.get('value'))})"
        return f"    # {t} on {_br_q(sel)}"
    if t == "click": return f"    page.{sel}.click()"
    if t == "check": return f"    page.{sel}.check()"
    if t == "uncheck": return f"    page.{sel}.uncheck()"
    if t == "fill": return f"    page.{sel}.fill({_br_q(action.get('value'))})"
    if t == "press": return f"    page.{sel}.press({_br_q(action.get('key'))})"
    if t == "select": return f"    page.{sel}.select_option({_br_q(action.get('value'))})"
    return f"    # {t} via {sel}"


def _br_generate_script(actions):
    lines = [
        "from playwright.sync_api import Playwright, sync_playwright",
        "", "",
        "def run(playwright: Playwright) -> None:",
        "    browser = playwright.chromium.launch(channel=\"chrome\", headless=False)",
        "    context = browser.new_context(ignore_https_errors=True)",
        "    page = context.new_page()",
    ]
    prev = None
    for a in actions:
        if a.get("type") == "goto":
            u = a.get("url")
            if u == prev:
                continue
            prev = u
            lines.append(f"    page.goto({_br_q(u)})")
        else:
            lines.append(_br_emit(a))
    lines += ["", "    context.close()", "    browser.close()", "", "",
              "with sync_playwright() as playwright:", "    run(playwright)", ""]
    return "\n".join(lines)


def _browser_record(args: dict, *, context=None, parent_agent=None, **kw) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return tool_error("url is required (the page to start recording from).")
    try:
        timeout = int(args.get("timeout", 600))
    except (TypeError, ValueError):
        timeout = 600
    if timeout < 10:
        timeout = 10

    holder = {"pages": {}, "order": [], "done": False, "closed": False}
    _BR_HOLDER["holder"] = holder
    try:
        def setup():
            # _context/_page are module globals set by _ensure_browser(); read
            # them AFTER ensure (bare names resolve at runtime — no snapshot bug).
            if not _ensure_browser():
                return (False, "CDP Chrome 起不来（确认系统装了 Chrome/Edge）")
            if _page is None:
                return (False, "没有可用 page（先 browser_navigate 打开一个页面）")
            try:
                _br_record_on_context(_context, _page, url)
                # Safe close handler: only sets a flag (never calls page.* inside
                # the callback, per CLAUDE.md) so closing the window ends recording.
                _page.on("close", lambda: holder.__setitem__("closed", True))
                logger.info("[browser_record] recording page ready: %s", _page.url)
                return (True, None)
            except Exception as e:
                return (False, f"setup failed: {e}")

        ok, err = _run_on_browser_thread(setup, timeout=120)
        if not ok:
            return tool_error(f"无法开始录制：{err}")
        deadline = time.monotonic() + timeout
        while True:
            if holder.get("closed") or is_interrupted() or time.monotonic() >= deadline:
                break
            # Robust finish detection: poll window.__pw_done (set by the finish
            # button) via a plain page.evaluate — works on connect_over_cdp
            # contexts where the expose_binding callback may not fire.
            try:
                if _run_on_browser_thread(lambda: bool(_page.evaluate("!!(window.__pw_done)")) if _page else False, timeout=10):
                    logger.info("[browser_record] finish detected via __pw_done")
                    break
            except Exception:
                pass
            time.sleep(0.4)
        logger.info("[browser_record] recording ended; reading actions")
        try:
            actions = _run_on_browser_thread(lambda: (_page.evaluate("window.__rec__||[]") if _page else []), timeout=30)
        except Exception:
            actions = []
    finally:
        _BR_HOLDER["holder"] = None
        _BR_ACTIVE["on"] = False

    if not actions:
        return tool_error("没捕获到操作。在 agent 的 Chrome 窗口里操作后点右下角「完成录制」按钮结束。")
    script = _br_generate_script(actions)
    by_type, by_kind = {}, {}
    for a in actions:
        by_type[a.get("type", "?")] = by_type.get(a.get("type", "?"), 0) + 1
        by_kind[a.get("kind", "?")] = by_kind.get(a.get("kind", "?"), 0) + 1
    return tool_result(success=True, url=url, action_count=len(actions),
                       by_type=by_type, selector_kinds=by_kind, actions=actions, script=script,
                       hint="在 agent 的 CDP Chrome 里录制的 DOM 操作（含 role/name + 语义选择器）。")


registry.register(
    name="browser_record",
    toolset="web",
    check_fn=_check_browser,
    read_only=False,
    subagent_blocked=True,
    handler=_browser_record,
    schema={
        "type": "function",
        "function": {
            "name": "browser_record",
            "description": (
                "Record page operations in THIS browser (the agent's CDP Chrome), "
                "then return a Playwright python script + actions with role/name metadata. "
                "A Chrome window opens at `url`; the user operates and clicks the on-page "
                "\"完成录制\" button to finish. Records trusted DOM events, so it also "
                "captures the agent's browser_* actions. Login/state are the agent's (cdp-profile)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Starting URL for recording."},
                    "timeout": {"type": "number", "description": "Max seconds to wait (default 600)."},
                },
                "required": ["url"],
            },
        },
    },
)


# Two-phase, non-blocking: start flips the recorder on (it then captures the
# AGENT's own browser_* actions, since those produce the same trusted DOM events
# a human would), the agent explores with browser_*, stop collects the captured
# actions + script. Lets the agent record a flow it performs itself (vs
# browser_record, which records a human). Same recorder, same output shape.


def _browser_record_start(args: dict, **kw) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return tool_error("url is required (the page to start exploring from).")

    def setup():
        if not _ensure_browser():
            return (False, "CDP Chrome 不可用（确认系统装了 Chrome/Edge）")
        if _page is None:
            return (False, "没有可用 page（先 browser_navigate 打开一个页面）")
        try:
            _br_record_on_context(_context, _page, url)  # navigate + inject + flag on
            return (True, _page.url)
        except Exception as e:
            return (False, f"setup failed: {e}")

    ok, res = _run_on_browser_thread(setup, timeout=120)
    if not ok:
        return tool_error(f"无法开始录制：{res}")
    return tool_result(
        success=True, url=res,
        message=("录制已开始（agent 自主探索模式）。现在用 browser_* 去探索/操作目标流程"
                 "（navigate/click/type/...），你的每一步都会被录下。完成后调用 "
                 "browser_record_stop 取出录制结果。注意：跨整页刷新（不同网址）会丢之前步骤，"
                 "尽量在同一 SPA 页面内操作。"),
    )


def _browser_record_stop(args: dict, **kw) -> str:
    def collect():
        if not _ensure_browser():
            return (False, "CDP Chrome 不可用", "", [])
        actions = []
        url = ""
        try:
            if _page is not None:
                actions = _page.evaluate("window.__rec__||[]")
                url = _page.url
                _page.evaluate("window.__pw_recording=false; window.__pw_done=false;")
        except Exception as e:
            logger.info("browser_record_stop: %s", e)
        return (True, "", url, actions)

    ok, _err, url, actions = _run_on_browser_thread(collect, timeout=30)
    if not ok:
        return tool_error("无法读取录制结果。")
    if not actions:
        return tool_error("没捕获到操作。start 后要用 browser_* 实际操作（navigate/click/type…）才会被录下。")
    script = _br_generate_script(actions)
    by_type, by_kind = {}, {}
    for a in actions:
        by_type[a.get("type", "?")] = by_type.get(a.get("type", "?"), 0) + 1
        by_kind[a.get("kind", "?")] = by_kind.get(a.get("kind", "?"), 0) + 1
    return tool_result(
        success=True, url=url, action_count=len(actions), by_type=by_type,
        selector_kinds=by_kind, actions=actions, script=script,
        hint=("这是 agent 自己探索录下的 DOM 操作。把 actions 翻译成 browser_* 步骤写进 skill"
              "（同 web-record-to-skill 的翻译表）；script 归档 references/recorded.py。"),
    )


registry.register(
    name="browser_record_start",
    toolset="web",
    check_fn=_check_browser,
    read_only=False,
    subagent_blocked=True,
    handler=_browser_record_start,
    schema={
        "type": "function",
        "function": {
            "name": "browser_record_start",
            "description": (
                "Start AGENT-driven recording: flips the recorder on in this Chrome. "
                "Then the agent explores the site with its own browser_* tools "
                "(navigate/click/type/...) and every action is captured (browser_* "
                "fires the same trusted DOM events). Call browser_record_stop to "
                "collect. Use this when you (the agent) should perform and record the "
                "flow yourself, vs browser_record which records a human."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Starting URL to explore from."},
                },
                "required": ["url"],
            },
        },
    },
)


registry.register(
    name="browser_record_stop",
    toolset="web",
    check_fn=_check_browser,
    read_only=False,
    subagent_blocked=True,
    handler=_browser_record_stop,
    schema={
        "type": "function",
        "function": {
            "name": "browser_record_stop",
            "description": (
                "Stop AGENT-driven recording: read the captured actions + generate a "
                "codegen-style Playwright script, and turn the recorder off. Pair with "
                "browser_record_start. Returns actions (with role/name metadata) + script."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
)
