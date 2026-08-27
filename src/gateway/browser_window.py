"""Win32 snap control for the agent's CDP Chrome window (desktop browser panel).

The desktop app's browser panel is a placeholder region; this module moves the
detached CDP Chrome window to exactly cover it (native, fully interactive).
Chrome stays an ordinary top-level window — every failure mode degrades to
"Chrome floats independently". All Win32 calls live here (the desktop has no
user32 channel and takes no new deps); the desktop talks to it over the serve
HTTP API.
"""

import ctypes
import logging
import re
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

HWND_TOP = 0
GW_HWNDPREV = 3
GW_OWNER = 4
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_TOOLWINDOW = 0x80
SWP_NOACTIVATE = 0x10
SWP_SHOWWINDOW = 0x40
SWP_NOMOVE = 0x2
SWP_NOSIZE = 0x1
SW_HIDE = 0
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4

MIN_W, MIN_H = 500, 300  # Chrome's practical minimum; snap clamps to this

if _IS_WIN:
    import ctypes.wintypes as wt

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    _user32.SetWindowPos.restype = wt.BOOL
    _user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    _user32.GetWindow.restype = wt.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
    # Handle-taking calls MUST carry argtypes: an undeclared parameter goes
    # through as a 32-bit signed c_int, and HWNDs above 2**31 (returned fine
    # by the declared restype above as unsigned) then raise OverflowError
    # instead of failing the IsWindow check — a 500, not a graceful miss.
    _user32.IsWindow.argtypes = [wt.HWND]
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _user32.IsIconic.argtypes = [wt.HWND]
    _user32.IsZoomed.argtypes = [wt.HWND]
    _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wt.LONG
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    _user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    _user32.MonitorFromWindow.restype = wt.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    # Owner binding (GWLP_HWNDPARENT). SetWindowLongPtrW is the 64-bit symbol;
    # 32-bit user32 only exports SetWindowLongW (c_ssize_t == c_long there, so
    # one argtypes declaration fits both). argtypes are set once on the real
    # function object; the CALL dispatches through the `_user32` attribute so
    # tests can monkeypatch a shim in its place.
    _slp = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
    _slp.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _slp.restype = ctypes.c_ssize_t
    del _slp

# Touched only on the event-loop thread (snap/hide/show/release/status);
# launch() runs in an executor and must not mutate it — that is the
# thread-safety invariant that keeps this lock-free.
_state = {"hwnd": None, "pid": None, "snapped": None, "desktop_hwnd": None,
          "hidden": False, "owned": False}


def _unsupported():
    return {"ok": False, "supported": False, "reason": "windows-only"}


def _pid_file():
    from tools.browser_tool import _BROWSER_DIR
    return _BROWSER_DIR / "cdp.pid"


def _read_cdp_pid():
    try:
        return int(_pid_file().read_text().strip())
    except Exception:
        return None


def _pid_image_basename(pid):
    h = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.replace("\\", "/").split("/")[-1].lower()
        return None
    finally:
        _kernel32.CloseHandle(h)


def _windows_for_pid(pid, include_hidden=False):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not include_hidden and not _user32.IsWindowVisible(hwnd):
            return True
        pid_out = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
        if pid_out.value != pid:
            return True
        if _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        r = wt.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            found.append((hwnd, (r.right - r.left) * (r.bottom - r.top)))
        return True

    _user32.EnumWindows(_cb, 0)
    return found


def _resolve_hwnd():
    """Fast path only (cache + pid file + EnumWindows, all single-digit ms).
    Returns hwnd or None. Process-command-line rediscovery is a separate
    executor path (launch), never taken here — this runs inline on the loop."""
    if not _IS_WIN:
        return None
    cached = _state["hwnd"]
    if cached and _user32.IsWindow(cached):
        pid_out = wt.DWORD()
        _user32.GetWindowThreadProcessId(cached, ctypes.byref(pid_out))
        if pid_out.value == _state["pid"]:
            return cached
    _state["hwnd"] = None

    pid = _read_cdp_pid()
    # pid-reuse guard: the file may now point at an unrelated process
    if pid is not None and _pid_image_basename(pid) not in ("chrome.exe", "msedge.exe"):
        pid = None
    _state["pid"] = pid
    if pid is None:
        return None

    wins = _windows_for_pid(pid)
    if not wins:
        # Chrome alive but no visible window: it was SW_HIDE'd by a hide()
        # whose hidden/snapped state died with a serve restart, so the
        # show-again path can never fire. Re-surface the main frame ourselves
        # (hidden windows keep their geometry; GetWindowRect still works).
        wins = _windows_for_pid(pid, include_hidden=True)
        if not wins:
            return None
        pick = max(wins, key=lambda hw: hw[1])[0]
        _user32.ShowWindow(pick, SW_SHOWNOACTIVATE)
        logger.info("resurfaced hidden CDP Chrome window (hwnd=%s)", pick)
        _state["hwnd"] = pick
        return pick
    # largest visible window = the main browser frame; devtools/popups lose
    pick = max(wins, key=lambda hw: hw[1])[0]
    _state["hwnd"] = pick
    return pick


def _rect_of(hwnd):
    if not hwnd:
        return None
    r = wt.RECT()
    if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return [r.left, r.top, r.right, r.bottom]
    return None


def _work_area_of(hwnd):
    hmon = _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not hmon:
        return None
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return [mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom]
    return None


def _restore_if_iconic(hwnd):
    if _user32.IsIconic(hwnd) or _user32.IsZoomed(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)


def _set_window_long_ptr(hwnd, index, value):
    if hasattr(_user32, "SetWindowLongPtrW"):
        return _user32.SetWindowLongPtrW(hwnd, index, value)
    return _user32.SetWindowLongW(hwnd, index, value)


def _own_to(hwnd, owner_hwnd):
    """Owned-binding: makes the OS guarantee what the anchor math used to
    chase — owned always z-orders above its owner, hides with it on minimize,
    and leaves Alt-tab. Cross-process owner is documented behavior (dialog
    mechanism). Returns False when the set didn't take; callers fall back to
    the anchor path."""
    _set_window_long_ptr(hwnd, GWLP_HWNDPARENT, owner_hwnd)
    return _user32.GetWindow(hwnd, GW_OWNER) == owner_hwnd


def _place_above(chrome_hwnd, desktop_hwnd, x, y, w, h):
    flags = SWP_NOACTIVATE | SWP_SHOWWINDOW  # serve never steals keyboard focus
    anchor = _user32.GetWindow(desktop_hwnd, GW_HWNDPREV)
    if anchor == chrome_hwnd:
        # z-order already correct, but the rect may have changed (a width
        # drag re-snaps at a new rect) — position anyway. HWND_TOP, never
        # chrome as its own anchor (self-insertion is undefined); SWP_SHOWWINDOW
        # also reveals a hidden chrome in the same call.
        _user32.SetWindowPos(chrome_hwnd, HWND_TOP, x, y, w, h, flags)
        return True
    if not anchor or not _user32.IsWindow(anchor):
        anchor = HWND_TOP
    _user32.SetWindowPos(chrome_hwnd, anchor, x, y, w, h, flags)
    if _user32.GetWindow(desktop_hwnd, GW_HWNDPREV) == chrome_hwnd:
        return True
    # docs read: hWndInsertAfter precedes (sits above) the positioned window,
    # so anchoring on GW_HWNDPREV should land chrome directly above desktop;
    # the post-condition failed — try the mirror polarity before giving up.
    _user32.SetWindowPos(chrome_hwnd, desktop_hwnd, x, y, w, h, flags)
    ok = _user32.GetWindow(desktop_hwnd, GW_HWNDPREV) == chrome_hwnd
    if not ok:
        logger.warning("snap z-order self-check failed (chrome=%s desktop=%s)",
                       chrome_hwnd, desktop_hwnd)
    return ok


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def status():
    if not _IS_WIN:
        return _unsupported()
    from tools.browser_tool import _CDP_PORT, _cdp_port_open
    running = _cdp_port_open(0.3)
    hwnd = _resolve_hwnd() if running else None
    # best-effort heal: a desktop crash skips before-quit's release, and the
    # OS destroys owned windows with their owner — un-own while we still can
    # (the 5s status poll is the only heartbeat serve has)
    if _state["owned"]:
        dh = _state["desktop_hwnd"]
        if not dh or not _user32.IsWindow(dh):
            if hwnd:
                _set_window_long_ptr(hwnd, GWLP_HWNDPARENT, 0)
            _state["owned"] = False
    return {
        "ok": True, "supported": True, "port": _CDP_PORT, "running": running,
        "pid": _state["pid"], "hwnd": hwnd,
        "snapped": list(_state["snapped"]) if _state["snapped"] else None,
        "hidden": _state["hidden"], "rect": _rect_of(hwnd),
    }


def launch():
    """Blocking (Popen + port poll, up to ~8s) — executor only; must not touch
    _state (the loop owns it). Returns {"launched": bool}; the caller fetches
    status() on the loop thread afterwards."""
    if not _IS_WIN:
        return {"launched": False, "reason": "windows-only"}
    from tools.browser_tool import _CDP_PORT, _cdp_port_open, _launch_cdp_chrome
    if not _cdp_port_open(0.25):
        if not _launch_cdp_chrome():
            return {"launched": False}
    try:
        have_pid = _pid_file().exists()
    except Exception:
        have_pid = False
    if not have_pid:
        # port answers but no pid file: Chrome from an older xihe that didn't
        # write one — rediscover by command line and heal the file
        pid = _scan_for_cdp_chrome(_CDP_PORT)
        if pid:
            try:
                _pid_file().write_text(str(pid))
            except Exception:
                logger.debug("cdp.pid heal write failed", exc_info=True)
    return {"launched": True}


_PORT_RE_TPL = r"--remote-debugging-port={port}(?:\s|$)"


def restart():
    """Kill the CDP Chrome and relaunch it with current launch flags (theme
    re-apply). Blocking — executor only; never touches _state (same invariant
    as launch()). Graceful taskkill first so Chrome saves its session, /F
    fallback if the port keeps answering. Login state lives in cdp-profile and
    survives; open tabs do not."""
    if not _IS_WIN:
        return {"restarted": False, "reason": "windows-only"}
    from tools.browser_tool import _cdp_port_open, _cleanup_browser, _launch_cdp_chrome
    pid = _read_cdp_pid()
    if pid is not None and _pid_image_basename(pid) in ("chrome.exe", "msedge.exe"):
        subprocess.run(["taskkill", "/PID", str(pid)],
                       capture_output=True, timeout=8)
        for _ in range(10):
            if not _cdp_port_open(0.25):
                break
            time.sleep(0.3)
        if _cdp_port_open(0.25):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8)
            for _ in range(6):
                if not _cdp_port_open(0.25):
                    break
                time.sleep(0.3)
    elif _cdp_port_open(0.25):
        # port answers but no owned, image-guarded pid — never kill blind
        return {"restarted": False, "reason": "no-owned-pid"}
    # stale playwright refs to the dead chrome; close() on dead objects is
    # try/except'd inside, so this just resets the module globals
    _cleanup_browser()
    if not _launch_cdp_chrome():
        return {"restarted": False}
    return {"restarted": True}


def _scan_for_cdp_chrome(port):
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe' or name='msedge.exe'",
             "get", "ProcessId,CommandLine", "/value"],
            capture_output=True, timeout=8, text=True).stdout or ""
    except Exception:
        logger.debug("wmic scan for cdp chrome failed", exc_info=True)
        return None
    want = re.compile(_PORT_RE_TPL.format(port=port))
    for block in out.split("\n\n"):
        cmdline = pid = None
        for line in block.splitlines():
            if line.startswith("CommandLine="):
                cmdline = line[len("CommandLine="):]
            elif line.startswith("ProcessId="):
                pid = line[len("ProcessId="):].strip()
        if cmdline and pid and want.search(cmdline):
            try:
                return int(pid)
            except ValueError:
                continue
    return None


def snap(x, y, w, h, desktop_hwnd):
    if not _IS_WIN:
        return _unsupported()
    if not all(_is_int(v) for v in (x, y, w, h, desktop_hwnd)):
        return {"ok": False, "reason": "bad-args"}
    x, y, w, h = int(x), int(y), int(w), int(h)
    w, h = max(w, MIN_W), max(h, MIN_H)
    hwnd = _resolve_hwnd()
    if hwnd is None:
        return {"ok": False, "reason": "no-chrome-window"}
    if not _user32.IsWindow(desktop_hwnd):
        return {"ok": False, "reason": "bad-desktop-hwnd"}
    _restore_if_iconic(hwnd)
    if _own_to(hwnd, desktop_hwnd):
        # owned: z-order is OS-guaranteed, so plain HWND_TOP positioning —
        # no anchor needed (and no focus-event z-repair upstream either)
        if not _user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h,
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW):
            return {"ok": False, "reason": "place-failed"}
        _state.update(hwnd=hwnd, snapped=(x, y, w, h), desktop_hwnd=desktop_hwnd,
                      hidden=False, owned=True)
        return {"ok": True, "rect": [x, y, w, h]}
    if not _place_above(hwnd, desktop_hwnd, x, y, w, h):
        return {"ok": False, "reason": "place-failed"}
    _state.update(hwnd=hwnd, snapped=(x, y, w, h), desktop_hwnd=desktop_hwnd, hidden=False)
    return {"ok": True, "rect": [x, y, w, h]}


def hide():
    if not _IS_WIN:
        return _unsupported()
    hwnd = _resolve_hwnd()
    if hwnd is None:
        return {"ok": False, "reason": "no-chrome-window"}
    fg = _user32.GetForegroundWindow()
    if fg:
        fg_pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        if fg_pid.value == _state["pid"]:
            # the desktop window blurred because the user clicked INTO chrome
            # (or a chrome popup) — that's not "left the app"; keep it visible
            return {"ok": True, "hidden": False, "reason": "chrome-foreground"}
    _user32.ShowWindow(hwnd, SW_HIDE)
    _state["hidden"] = True
    return {"ok": True, "hidden": True}


def show(desktop_hwnd=None):
    if not _IS_WIN:
        return _unsupported()
    dh = int(desktop_hwnd) if _is_int(desktop_hwnd) else _state["desktop_hwnd"]
    rect = _state["snapped"]
    if not dh or not rect:
        return {"ok": False, "reason": "never-snapped"}
    if not _user32.IsWindow(dh):
        return {"ok": False, "reason": "bad-desktop-hwnd"}
    hwnd = _resolve_hwnd()
    if hwnd is None:
        return {"ok": False, "reason": "no-chrome-window"}
    _restore_if_iconic(hwnd)
    if not _place_above(hwnd, dh, *rect):
        return {"ok": False, "reason": "place-failed"}
    _state.update(hwnd=hwnd, desktop_hwnd=dh, hidden=False)
    return {"ok": True}


def release():
    if not _IS_WIN:
        return _unsupported()
    was_snapped = _state["snapped"] is not None
    _state["snapped"] = None
    hwnd = _resolve_hwnd()
    if hwnd is None:
        return {"ok": True, "was_snapped": was_snapped}
    if _state["owned"]:
        # must precede the desktop app's exit: the OS destroys owned windows
        # with their owner, and a released Chrome is meant to outlive it
        _set_window_long_ptr(hwnd, GWLP_HWNDPARENT, 0)
        _state["owned"] = False
    if _state["hidden"]:
        _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        _state["hidden"] = False
    dh = _state["desktop_hwnd"]
    placed = False
    if dh and _user32.IsWindow(dh):
        dr = _rect_of(dh)
        wa = _work_area_of(dh)
        cr = _rect_of(hwnd)
        if dr and wa and cr:
            cw, ch = cr[2] - cr[0], cr[3] - cr[1]
            x = dr[2] + 16
            if x + cw > wa[2]:
                x = dr[0] - cw - 16
            if x >= wa[0]:
                # beside the desktop window, top aligned — no foreground steal
                # (Windows foreground-lock blocks cross-process activation
                # anyway; chrome comes forward on first click)
                _user32.SetWindowPos(hwnd, HWND_TOP, x, dr[1], cw, ch, SWP_NOACTIVATE)
                placed = True
    if not placed:
        _user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                             SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
    return {"ok": True, "was_snapped": was_snapped}
