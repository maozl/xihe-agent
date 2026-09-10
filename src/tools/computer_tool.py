"""Desktop automation tools — screen, mouse, keyboard, windows, apps, clipboard, UIA.

Windows and macOS, both requiring an interactive login session (a desktop
this process can see and drive; service/unattended sessions have none).

Platform notes:
- Windows: the process is made DPI-aware at import (per-monitor-v2 first),
  so screenshot pixels and pyautogui input share one physical-pixel grid
  even at 125%/150% scaling. UIA rects still arrive on the target app's own
  grid (Electron reports physical px) and are calibrated per query — see
  _uia_grid_ratio. xihe running with normal privileges cannot touch
  elevated windows (UIPI drops the input silently); a locked screen produces
  stale/black captures.
- macOS: mouse/keyboard need pyobjc (Quartz) and the host terminal must be
  granted Accessibility (辅助功能). Window listing and the UI element tree go
  through System Events (osascript) and additionally need Automation
  (自动化) permission — failures surface as tool errors with that hint.
  Retina displays capture at 2x: screen_size/scale/changed_region are always
  reported in the logical point grid that computer_click and UIA rects use,
  so callers never juggle two grids.

Grounding is text-first: computer_uia exposes the accessibility element tree
as text (name/role/rect with precomputed centers) and is the cheap,
deterministic locator; computer_screenshot (+ vision_analyze / image_ocr)
covers self-drawn UI the accessibility tree can't see.
"""

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    from PIL import Image, ImageChops, ImageGrab
    if _IS_WIN:
        import win32clipboard
        import win32con
        import win32gui
        import win32process
    _DESKTOP_OK = _IS_WIN or _IS_MAC
except ImportError:
    _DESKTOP_OK = False

_UIA_OK = False
if _DESKTOP_OK:
    if _IS_WIN:
        try:
            import comtypes
            import pywinauto
            _UIA_OK = True
        except ImportError:
            pass
    else:
        # macOS walks System Events via osascript (always present); permission
        # is only knowable at call time, so the gate passes and errors guide.
        _UIA_OK = True

from core.config import AGENT_HOME
_COMPUTER_DIR = AGENT_HOME / "computer"


def _set_dpi_awareness() -> None:
    # Must run before captures/inputs; callers that already set awareness (or
    # were denied) get an error code instead of an exception — swallow both.
    # PMv2 keeps metrics, GDI captures and cursor input on ONE physical grid
    # at any scale; the v1 call (SetProcessDpiAwareness) leaves GDI captures
    # DWM-virtualized while metrics go physical — two grids in one process.
    try:
        pmv2 = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(pmv2):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if _DESKTOP_OK and _IS_WIN:
    _set_dpi_awareness()

# Gateway builds a fresh agent per inbound message; without this lock two
# sessions would interleave clicks/clipboard writes on the same desktop.
_desk_lock = threading.RLock()


def _check_desktop() -> bool:
    return _DESKTOP_OK


def _check_uia() -> bool:
    return _UIA_OK


def _failsafe_error() -> str:
    return tool_error(
        "用户急停：鼠标被移到屏幕角落触发 pyautogui FAILSAFE，本次操作已中止。",
        failsafe=True,
    )


def _run_cmd(args: list, input_bytes: bytes | None = None, timeout: int = 15):
    """subprocess.run wrapper for macOS helpers (pbcopy/osascript/open).

    Separated so tests can fake the process boundary; CREATE_NO_WINDOW keeps
    console flashes out of gateway runs on Windows.
    """
    kwargs = {"input": input_bytes, "capture_output": True, "timeout": timeout}
    if _IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, **kwargs)


def _virtual_screen() -> tuple:
    if _IS_WIN:
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    w, h = pyautogui.size()
    return (0, 0, int(w), int(h))


def _clamp_to_screen(x: int, y: int) -> tuple:
    left, top, width, height = _virtual_screen()
    return (min(max(int(x), left), left + width - 1),
            min(max(int(y), top), top + height - 1))


# ---------------------------------------------------------------------------
# Clipboard (Windows: win32clipboard CF_UNICODETEXT / macOS: pbcopy/pbpaste)
# ---------------------------------------------------------------------------

def _open_clipboard() -> None:
    # Another process can hold the clipboard briefly (IM/clipboard managers);
    # OpenClipboard fails with error 5 until it closes — retry for ~0.5s.
    last: Exception | None = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as e:
            last = e
            time.sleep(0.05)
    raise RuntimeError(f"clipboard busy (held by another app): {last}")


def _clipboard_set(text: str) -> None:
    if not _IS_WIN:
        p = _run_cmd(["pbcopy"], input_bytes=str(text).encode("utf-8"))
        if p.returncode != 0:
            raise RuntimeError(
                f"pbcopy failed: {(p.stderr or b'').decode('utf-8', 'replace')[:120]}")
        return
    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, str(text))
    finally:
        win32clipboard.CloseClipboard()


def _clipboard_get() -> str:
    if not _IS_WIN:
        p = _run_cmd(["pbpaste"])
        if p.returncode != 0:
            raise RuntimeError(
                f"pbpaste failed: {(p.stderr or b'').decode('utf-8', 'replace')[:120]}")
        return p.stdout.decode("utf-8", "replace")
    _open_clipboard()
    try:
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return data if isinstance(data, str) else ""
    finally:
        win32clipboard.CloseClipboard()


def _paste_text(text: str, restore: bool = True) -> None:
    # Clipboard paste is the only reliable input path on Windows: keystroke
    # emulation breaks on IME/focus and can't do CJK at all. The user's
    # clipboard is saved and restored; apps that read it asynchronously may
    # race the restore — restore_clipboard=False skips that.
    prev = None
    if restore:
        try:
            prev = _clipboard_get()
        except Exception:
            prev = None
    _clipboard_set(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    if restore and prev is not None:
        time.sleep(0.1)
        try:
            _clipboard_set(prev)
        except Exception:
            logger.warning("clipboard restore after paste failed")


# ---------------------------------------------------------------------------
# computer_screenshot
# ---------------------------------------------------------------------------

_last_shot = None
_MAX_WIDTH = 1920
_DIFF_THRESHOLD = 10
_DIFF_MIN_FRACTION = 0.001


def _capture_screen(region: tuple | None):
    # all_screens (whole virtual screen incl. negative coords) is Windows-only;
    # macOS screencapture covers the main display.
    if _IS_WIN:
        return ImageGrab.grab(all_screens=True, bbox=region).convert("RGB")
    return ImageGrab.grab(bbox=region).convert("RGB")


def _grid_size(capture: "Image.Image") -> list:
    """The click coordinate grid (pyautogui input space) for this capture.

    Windows: DPI-aware, so capture pixels == input pixels. macOS: Retina
    captures are 2x the logical point grid, which pyautogui/UIA rects use.
    """
    if not _IS_WIN:
        w, h = pyautogui.size()
        return [int(w), int(h)]
    return [capture.width, capture.height]


def _diff_vs_previous(cur: "Image.Image", grid_size: tuple):
    prev = _last_shot
    if (prev is None or prev["grid"] != tuple(grid_size)
            or prev["img"].size != cur.size):
        return None
    mask = (ImageChops.difference(prev["img"], cur)
            .convert("L")
            .point(lambda p: 255 if p > _DIFF_THRESHOLD else 0))
    bbox = mask.getbbox()
    total = cur.width * cur.height
    changed_px = mask.histogram()[255] if total else 0
    fraction = changed_px / total if total else 0.0
    if bbox is None or fraction < _DIFF_MIN_FRACTION:
        return {"changed": False, "change_fraction": round(fraction, 5),
                "changed_region": None,
                "summary": "No visible change vs previous screenshot — the action may not have taken effect."}
    # bbox is in downscaled-image pixels; rescale into the click grid
    f = grid_size[0] / cur.width
    x, y, x2, y2 = bbox
    region = [round(x * f), round(y * f), round((x2 - x) * f), round((y2 - y) * f)]
    return {"changed": True, "change_fraction": round(fraction, 5),
            "changed_region": region,
            "summary": f"{fraction:.1%} of pixels changed in screen region "
                       f"[x={region[0]}, y={region[1]}, {region[2]}x{region[3]}px]"}


def _computer_screenshot(args: dict, **kw) -> str:
    global _last_shot
    region = args.get("region")
    if region is not None and (not isinstance(region, (list, tuple)) or len(region) != 4):
        return tool_error("region must be [x, y, width, height] in screen coordinates")
    try:
        max_width = int(args.get("max_width") or _MAX_WIDTH)
    except (TypeError, ValueError):
        max_width = _MAX_WIDTH
    max_width = max(200, min(max_width, 3840))

    with _desk_lock:
        try:
            img = _capture_screen(tuple(int(v) for v in region) if region else None)
        except Exception as e:
            return tool_error(f"screen capture failed (locked screen / no interactive desktop?): {e}")
        grid_size = _grid_size(img)
        if img.width > max_width:
            s = max_width / img.width
            img = img.resize((max_width, max(1, round(img.height * s))), Image.LANCZOS)
        diff = _diff_vs_previous(img, tuple(grid_size))
        # saved-image px = grid px × scale (downscale and Retina density combined)
        scale = round(img.width / grid_size[0], 4) if grid_size[0] else 1.0
        _last_shot = {"img": img.copy(), "grid": tuple(grid_size)}
        try:
            _COMPUTER_DIR.mkdir(parents=True, exist_ok=True)
            path = _COMPUTER_DIR / f"shot_{int(time.time() * 1000)}.png"
            img.save(str(path), "PNG")
        except OSError as e:
            return tool_error(f"capture succeeded but writing the PNG failed: {e}")

    description = None
    if args.get("describe"):
        from tools.vision_tools import describe_image_sync
        description = describe_image_sync(str(path), args.get("prompt"))
        if description == "(vision not available)":
            description = None
    return tool_result(
        success=True, path=str(path),
        width=img.width, height=img.height,
        screen_size=grid_size, scale=scale,
        diff=diff, description=description,
        hint=("vision not configured — use image_ocr to read text from the image"
              if args.get("describe") and description is None else None),
    )


# ---------------------------------------------------------------------------
# computer_click / computer_type / computer_key / computer_scroll
# ---------------------------------------------------------------------------

def _computer_click(args: dict, **kw) -> str:
    try:
        x, y = args.get("x"), args.get("y")
        if x is None or y is None:
            return tool_error("x and y are required (screen coordinates in the "
                              "grid computer_screenshot reports as screen_size / "
                              "computer_uia cx/cy)")
        err = _target_required_error(args)
        if err:
            return tool_error(err)
        x, y = _clamp_to_screen(int(x), int(y))
        button = str(args.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            return tool_error("button must be left | right | middle")
        clicks = 2 if args.get("double") else max(1, int(args.get("clicks") or 1))
        with _desk_lock:
            err = _foreground_target(args)
            if err:
                return tool_error(err)
            try:
                pyautogui.click(x=x, y=y, clicks=clicks, button=button, duration=0.15)
            except pyautogui.FailSafeException:
                return _failsafe_error()
        return tool_result(success=True, x=x, y=y, button=button, clicks=clicks,
                           hint="verify with computer_screenshot diff or computer_uia list")
    except (TypeError, ValueError) as e:
        return tool_error(f"invalid arguments: {e}")


def _computer_type(args: dict, **kw) -> str:
    text = args.get("text")
    if text is None or str(text) == "":
        return tool_error("text is required")
    text = str(text)
    err = _target_required_error(args)
    if err:
        return tool_error(err)
    restore = bool(args.get("restore_clipboard", True))
    # Keystroke fast path is opt-in and ASCII-only: default is always paste.
    fast_path = args.get("mode") == "type" and text.isascii() and len(text) <= 20
    with _desk_lock:
        err = _foreground_target(args)
        if err:
            return tool_error(err)
        try:
            if fast_path:
                pyautogui.write(text, interval=0.02)
            else:
                _paste_text(text, restore=restore)
        except pyautogui.FailSafeException:
            return _failsafe_error()
    return tool_result(success=True, chars=len(text),
                       method="keystrokes" if fast_path else "clipboard-paste")


# Models routinely say esc/return/del; pyautogui wants escape/enter/delete.
_KEY_ALIASES = {
    "return": "enter", "esc": "escape", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown",
    "cmd": "win", "super": "win", "meta": "win", "windows": "win",
    "control": "ctrl", "option": "alt",
}
_MAC_ALIAS_OVERRIDES = {"cmd": "command", "super": "command", "meta": "command",
                        "win": "command", "windows": "command"}
_MODIFIERS = {"ctrl", "alt", "shift", "win", "command"}


def _key_aliases() -> dict:
    if _IS_WIN:
        return _KEY_ALIASES
    return {**_KEY_ALIASES, **_MAC_ALIAS_OVERRIDES}


def _computer_key(args: dict, **kw) -> str:
    raw = str(args.get("keys") or "").strip().lower()
    if not raw:
        return tool_error("keys is required, e.g. 'enter', 'ctrl+s', 'win+d'")
    err = _target_required_error(args)
    if err:
        return tool_error(err)
    aliases = _key_aliases()
    parts = [aliases.get(p.strip(), p.strip()) for p in raw.split("+") if p.strip()]
    if not parts:
        return tool_error("keys is empty after parsing")
    invalid = [p for p in parts
               if p not in _MODIFIERS and p not in pyautogui.KEYBOARD_KEYS]
    if invalid:
        return tool_error(f"unknown key(s): {invalid}; use pyautogui key names "
                          "(enter/escape/delete/pageup/home/end/f1..f12), combos joined with '+'")
    try:
        presses = max(1, int(args.get("presses") or 1))
    except (TypeError, ValueError):
        return tool_error("presses must be an integer")
    with _desk_lock:
        err = _foreground_target(args)
        if err:
            return tool_error(err)
        try:
            if len(parts) == 1:
                pyautogui.press(parts[0], presses=presses)
            else:
                for _ in range(presses):
                    pyautogui.hotkey(*parts)
        except pyautogui.FailSafeException:
            return _failsafe_error()
    return tool_result(success=True, keys="+".join(parts), presses=presses)


def _computer_scroll(args: dict, **kw) -> str:
    direction = str(args.get("direction") or "down").lower()
    if direction not in ("up", "down", "left", "right"):
        return tool_error("direction must be up | down | left | right")
    err = _target_required_error(args)
    if err:
        return tool_error(err)
    try:
        clicks = max(1, int(args.get("clicks") or 3))
    except (TypeError, ValueError):
        return tool_error("clicks must be an integer")
    x, y = args.get("x"), args.get("y")
    if x is not None and y is not None:
        x, y = _clamp_to_screen(int(x), int(y))
    else:
        x = y = None
    with _desk_lock:
        err = _foreground_target(args)
        if err:
            return tool_error(err)
        try:
            if direction in ("up", "down"):
                pyautogui.scroll(clicks if direction == "down" else -clicks, x=x, y=y)
            else:
                pyautogui.hscroll(clicks if direction == "right" else -clicks, x=x, y=y)
        except pyautogui.FailSafeException:
            return _failsafe_error()
    return tool_result(success=True, direction=direction, clicks=clicks)


# ---------------------------------------------------------------------------
# computer_windows — Windows: win32gui / macOS: System Events (osascript)
# ---------------------------------------------------------------------------

def _enum_windows_win(query: str = "") -> list:
    out = []
    fg = win32gui.GetForegroundWindow()

    def _cb(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return
        if query and query.lower() not in title.lower():
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        out.append({"hwnd": hwnd, "title": title,
                    "rect": [left, top, right, bottom],
                    "foreground": hwnd == fg,
                    "minimized": bool(win32gui.IsIconic(hwnd))})

    win32gui.EnumWindows(_cb, None)
    out.sort(key=lambda w: (not w["foreground"], w["title"].lower()))
    return out


# Every concatenation starts from a string literal — number & string would
# otherwise build a list, the classic AppleScript `&` trap.
_MAC_WINDOWS_SCRIPT = (
    'tell application "System Events"\n'
    '\tset out to ""\n'
    '\trepeat with p in (every application process whose background only is false)\n'
    '\t\ttry\n'
    '\t\t\tset fn to frontmost of p\n'
    '\t\t\trepeat with w in every window of p\n'
    '\t\t\t\ttry\n'
    '\t\t\t\t\tset {x, y} to position of w\n'
    '\t\t\t\t\tset {wd, ht} to size of w\n'
    '\t\t\t\t\tset t to name of w\n'
    '\t\t\t\t\tset mn to false\n'
    '\t\t\t\t\ttry\n'
    '\t\t\t\t\t\tset mn to value of attribute "AXMinimized" of w\n'
    '\t\t\t\t\tend try\n'
    '\t\t\t\t\tset out to out & (name of p) & tab & t & tab & x & "," & y & "," & (x + wd) & "," & (y + ht) & tab & fn & tab & mn & linefeed\n'
    '\t\t\t\tend try\n'
    '\t\t\tend repeat\n'
    '\t\tend try\n'
    '\tend repeat\n'
    '\treturn out\n'
    'end tell'
)


def _mac_windows_from_tsv(text: str) -> list:
    """Parse the System Events window dump: app \\t title \\t x,y,x2,y2 \\t
    frontmost \\t minimized."""
    wins = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        app, title, rect_s, fg_s, mn_s = (p.strip() for p in parts)
        try:
            rect = [int(v) for v in rect_s.split(",")]
            if len(rect) != 4:
                continue
        except ValueError:
            continue
        wins.append({"hwnd": None, "title": title or app, "app": app,
                     "rect": rect, "foreground": fg_s == "true",
                     "minimized": mn_s == "true"})
    return wins


def _mac_enum_windows(query: str = "") -> list:
    p = _run_cmd(["osascript", "-e", _MAC_WINDOWS_SCRIPT])
    if p.returncode != 0:
        raise RuntimeError(
            "System Events query failed (grant the host terminal 辅助功能/自动化 "
            f"permission?): {(p.stderr or b'').decode('utf-8', 'replace')[:160]}")
    out = []
    for i, w in enumerate(_mac_windows_from_tsv(p.stdout.decode("utf-8", "replace")), 1):
        if (query and query.lower() not in w["title"].lower()
                and query.lower() not in w["app"].lower()):
            continue
        w["hwnd"] = i
        out.append(w)
    out.sort(key=lambda w: (not w["foreground"], w["title"].lower()))
    return out


def _enum_windows(query: str = "") -> list:
    if _IS_WIN:
        return _enum_windows_win(query)
    return _mac_enum_windows(query)


def _resolve_hwnd(args: dict):
    if args.get("hwnd"):
        try:
            return int(args["hwnd"])
        except (TypeError, ValueError):
            return None
    for w in _enum_windows_win(str(args.get("query") or "")):
        return w["hwnd"]
    return None


def _mac_resolve_window(args: dict):
    q = str(args.get("query") or "").strip()
    if q:
        for w in _mac_enum_windows():
            if q.lower() in w["title"].lower() or q.lower() in w["app"].lower():
                return w
        return None
    h = args.get("hwnd")
    if h:
        try:
            idx = int(h)
        except (TypeError, ValueError):
            return None
        wins = _mac_enum_windows()
        if 1 <= idx <= len(wins):
            return wins[idx - 1]
        return None
    # No query, no hwnd: act on the frontmost app's window.
    for w in _mac_enum_windows():
        if w["foreground"]:
            return w
    return None


def _force_foreground(hwnd: int) -> str | None:
    """Raise hwnd to the foreground. Returns an error string when we can
    verify it did NOT end up in front (foreground lock / elevated app) —
    callers must then abort rather than send input to whatever is on top."""
    # Windows refuses SetForegroundWindow from a non-foreground process;
    # attaching our input to the foreground thread lifts that restriction —
    # but the attach itself is denied (error 5) when the foreground thread
    # belongs to a higher-integrity process, so degrade to a plain raise.
    fg = win32gui.GetForegroundWindow()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    attached = False
    if fg_tid and fg_tid != tid:
        try:
            attached = bool(win32process.AttachThreadInput(fg_tid, tid, True))
        except Exception:
            attached = False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() != hwnd:
            time.sleep(0.1)  # foreground switches can lag the call
            if win32gui.GetForegroundWindow() != hwnd:
                return ("could not bring the target window to the front "
                        "(Windows foreground lock — the current foreground "
                        "app may be elevated); no input was sent")
    finally:
        if attached:
            try:
                win32process.AttachThreadInput(fg_tid, tid, False)
            except Exception:
                pass
    return None


def _mac_window_action(action: str, win: dict) -> str | None:
    """Activate the app, then act on its front window. macOS has no universal
    maximize/restore — activation is the best available for those."""
    p = _run_cmd(["open", "-a", win["app"]])
    if p.returncode != 0:
        return (f"activate '{win['app']}' failed: "
                f"{(p.stderr or b'').decode('utf-8', 'replace')[:160]}")
    if action == "close":
        time.sleep(0.2)
        pyautogui.hotkey("command", "w")
    elif action == "minimize":
        time.sleep(0.2)
        pyautogui.hotkey("command", "m")
    return None


def _target_required_error(args: dict) -> str | None:
    """Input ops must name their window — real clicks land on whatever is
    topmost and keys go to the focused window, so an unnamed input is a
    wrong-window input waiting to happen."""
    if args.get("query") or args.get("hwnd") or args.get("no_window"):
        return None
    return ("query or hwnd is required — name the window to act on "
            "(computer_windows action='list' shows titles); pass "
            "no_window=true only for taskbar/desktop/global-hotkey actions "
            "with no window target")


def _foreground_target(args: dict) -> str | None:
    """Foreground the window named by args (query/hwnd) before an input op.
    no_window opts out (taskbar/desktop/global combos). Returns an error
    string when a target was given but not found."""
    if args.get("no_window") or not (args.get("query") or args.get("hwnd")):
        return None
    if _IS_WIN:
        hwnd = _resolve_hwnd(args)
        if not hwnd:
            return ("no window matched — run computer_windows action='list' "
                    "for titles, then pass query or hwnd")
        err = _force_foreground(hwnd)
        if err:
            return err
        time.sleep(0.15)  # let the target process its activation
        return None
    try:
        win = _mac_resolve_window(args)
    except RuntimeError as e:
        return str(e)
    if not win:
        return ("no window matched — run computer_windows action='list' "
                "for titles, then pass query")
    err = _mac_window_action("focus", win)
    if err:
        return err
    time.sleep(0.3)
    return None


def _computer_windows(args: dict, **kw) -> str:
    action = str(args.get("action") or "list").lower()
    with _desk_lock:
        try:
            if action == "list":
                wins = _enum_windows(str(args.get("query") or ""))
                return tool_result(windows=wins, count=len(wins))
        except RuntimeError as e:
            return tool_error(str(e))
        if _IS_WIN:
            hwnd = _resolve_hwnd(args)
            if not hwnd:
                return tool_error("no window matched — run action='list' to see hwnd/title, "
                                  "then pass hwnd or a title substring as query")
            if action == "focus":
                err = _force_foreground(hwnd)
                if err:
                    return tool_error(err)
            elif action == "close":
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            elif action in ("minimize", "maximize", "restore"):
                win32gui.ShowWindow(hwnd, getattr(win32con, f"SW_{action.upper()}"))
            else:
                return tool_error("unknown action; use list | focus | minimize | maximize | restore | close")
            return tool_result(success=True, action=action, hwnd=hwnd)
        if action not in ("focus", "close", "minimize", "maximize", "restore"):
            return tool_error("unknown action; use list | focus | minimize | maximize | restore | close")
        try:
            win = _mac_resolve_window(args)
        except RuntimeError as e:
            return tool_error(str(e))
        if not win:
            return tool_error("no window matched — run action='list' first, then pass "
                              "query (window title or app name substring)")
        try:
            err = _mac_window_action(action, win)
        except pyautogui.FailSafeException:
            return _failsafe_error()
        if err:
            return tool_error(err)
        hint = ("macOS has no universal maximize/restore — the window was activated"
                if action in ("maximize", "restore") else
                "close/minimize act on the app's front window")
        return tool_result(success=True, action=action, app=win["app"],
                           title=win["title"], hint=hint)


# ---------------------------------------------------------------------------
# computer_app
# ---------------------------------------------------------------------------

def _normalize_proc_name(name: str) -> str:
    name = str(name).strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def _mac_open_cmd(target: str, extra) -> list:
    if extra:
        parts = [str(p) for p in extra] if isinstance(extra, list) else str(extra).split()
        return ["open", "-a", target, "--args", *parts]
    if "://" in target or os.path.exists(target):
        return ["open", target]
    return ["open", "-a", target]


def _computer_app(args: dict, **kw) -> str:
    action = str(args.get("action") or "open").lower()
    if action == "open":
        target = args.get("target") or args.get("path")
        if not target:
            return tool_error("target is required (URL, file path, or registered app name)")
        extra = args.get("args")
        with _desk_lock:
            if not _IS_WIN:
                p = _run_cmd(_mac_open_cmd(str(target), extra))
                if p.returncode != 0:
                    return tool_error(f"open failed: "
                                      f"{(p.stderr or b'').decode('utf-8', 'replace')[:160]}")
            elif extra:
                parts = [str(target)] + ([str(p) for p in extra]
                                         if isinstance(extra, list) else str(extra).split())
                # Detached so the app doesn't die with the xihe process.
                creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                subprocess.Popen(parts, creationflags=creationflags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(str(target))
        return tool_result(success=True, action="open", target=str(target))
    if action == "close":
        name = _normalize_proc_name(args.get("name") or "")
        if not name:
            return tool_error("name is required (process name, e.g. 'notepad' or 'notepad.exe')")
        import psutil
        victims = []
        with _desk_lock:
            for p in psutil.process_iter(["name"]):
                try:
                    if _normalize_proc_name(p.info.get("name") or "") == name:
                        victims.append(p)
                except Exception:
                    continue
            for p in victims:
                try:
                    p.terminate()
                except Exception:
                    pass
            _, alive = psutil.wait_procs(victims, timeout=3)
            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass
        return tool_result(success=True, action="close", name=name,
                           count=len(victims), pids=[p.pid for p in victims])
    return tool_error("unknown action; use open | close")


# ---------------------------------------------------------------------------
# computer_clipboard
# ---------------------------------------------------------------------------

_CLIPBOARD_GET_LIMIT = 10000


def _computer_clipboard(args: dict, **kw) -> str:
    action = str(args.get("action") or "get").lower()
    if action not in ("get", "set", "clear"):
        return tool_error("unknown action; use get | set | clear")
    try:
        with _desk_lock:
            if action == "get":
                text = _clipboard_get()
                if len(text) > _CLIPBOARD_GET_LIMIT:
                    return tool_result(text=text[:_CLIPBOARD_GET_LIMIT], truncated=True)
                return tool_result(text=text, truncated=False)
            if action == "set":
                if args.get("text") is None:
                    return tool_error("text is required for set")
                _clipboard_set(str(args["text"]))
                return tool_result(success=True, action="set",
                                   chars=len(str(args["text"])))
            _clipboard_set("")
            return tool_result(success=True, action="clear")
    except Exception as e:
        return tool_error(f"clipboard unavailable (locked by another app?): {e}")


# ---------------------------------------------------------------------------
# computer_uia — Windows: pywinauto / macOS: System Events walk (osascript)
# ---------------------------------------------------------------------------

_UIA_MAX_ELEMENTS = 200
_UIA_NAME_LIMIT = 60
_UIA_ROLE_KEYWORDS = ("button", "edit", "check", "radio", "combo", "link",
                      "menu", "slider", "tabitem", "treeitem", "listitem",
                      "listbox", "spinner", "spin", "textarea")
_MAC_UIA_ROLE_KEYWORDS = ("button", "checkbox", "radio", "pop", "combo",
                          "textfield", "textarea", "searchfield", "link",
                          "menu", "slider", "tab", "list", "outline", "tree",
                          "table", "stepper")


# UIA calls go cross-process into the target app; a window that isn't pumping
# messages (just launched, busy, hung) makes each COM call burn its internal
# RPC timeout — and the walk makes one LIVE property call per element, so a
# half-responsive window multiplies that into hours. 2026-08-28 desktop
# session: computer_uia on a freshly launched Notepad never returned and held
# both the turn thread and _desk_lock — root cause turned out to be
# pywinauto's root-element hop (see _uia_window_wrapper), since fixed; the
# budget below
# stays as the backstop for genuinely unresponsive windows. Every UIA op runs
# under a wall-clock budget on a daemon thread; a timed-out walk is abandoned
# (the thread leaks until the window recovers or the process exits) and
# further UIA calls short-circuit while the zombie is still stuck instead of
# stacking a second call into it. Budget: healthy walks here measure ≤3.3s
# (7-268 elements); 30s default = ~10x headroom, `timeout` arg (5-120s) lets
# the agent extend for known-huge trees (visible Chrome/IDEA).
_UIA_DEFAULT = 30.0
_UIA_MIN = 5.0
_UIA_MAX = 120.0
_uia_stuck: threading.Thread | None = None


def _uia_timeout_arg(args: dict) -> float:
    try:
        t = float(args.get("timeout") or _UIA_DEFAULT)
    except (TypeError, ValueError):
        return _UIA_DEFAULT
    return min(_UIA_MAX, max(_UIA_MIN, t))


def _uia_call(fn, what: str, timeout: float):
    """Run one UIA operation with a budget. Returns (value, None) on success,
    (None, error_text) on timeout/stuck; fn's exceptions re-raise as-is."""
    global _uia_stuck
    if _uia_stuck is not None and _uia_stuck.is_alive():
        return None, ("a previous UIA query is still stuck on an unresponsive "
                      "window — use computer_screenshot (+computer_click) "
                      "instead; this recovers when the window responds or the "
                      "process restarts")
    box: dict = {}

    def _run():
        try:
            box["ok"] = fn()
        except BaseException as e:
            box["err"] = e

    t = threading.Thread(target=_run, daemon=True, name="uia-walk")
    t.start()
    t.join(timeout)
    if t.is_alive():
        _uia_stuck = t
        logger.warning("UIA %s abandoned after %.0fs (target window not "
                       "answering)", what, timeout)
        return None, (f"UIA {what} timed out after {timeout:.0f}s — the "
                      "target window is not answering accessibility queries "
                      "(freshly launched, busy, or hung), or its tree is "
                      "huge. Retry with a larger timeout (max "
                      f"{_UIA_MAX:.0f}s) if the window is known to be big, "
                      "or fall back to computer_screenshot + computer_click")
    if "err" in box:
        raise box["err"]
    return box.get("ok"), None


def _uia_window_wrapper(hwnd: int):
    # Must construct the wrapper directly from the hwnd. WindowSpecification's
    # resolve path (application.py __getattribute__) first builds a wrapper
    # over the DESKTOP ROOT element and reads its ControlType — that exact COM
    # call deadlocks on any non-main thread here (2026-08-28 probes: main
    # 0.06s / worker stuck >45s, deterministic; every raw comtypes call on the
    # same threads returns in <0.1s, so it's pywinauto's root hop, not UIA
    # itself). Direct construction never touches the root; cold-worker probe:
    # elem + wrapper + 26-element walk + describe = 0.09s.
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    return UIAWrapper(UIAElementInfo(hwnd))


def _uia_wrap(query: str):
    # Desktop(...).window(handle=...) resolves through WindowSpecification —
    # see _uia_window_wrapper for why that path is off-limits here.
    # comtypes needs an initialized apartment per worker thread
    # (already-initialized or mode-mismatch both land in except).
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    if query:
        wins = _enum_windows_win(query)
        if not wins:
            raise LookupError(f"no window matching {query!r} — run "
                              f"computer_windows action='list' for titles")
        hwnd = wins[0]["hwnd"]
    else:
        hwnd = win32gui.GetForegroundWindow()
    if win32gui.IsIconic(hwnd):
        # Minimized windows expose garbage (-32000) rects through UIA.
        raise LookupError("target window is minimized — restore it first "
                          "(computer_windows action='restore') and retry")
    return hwnd, _uia_window_wrapper(hwnd)


def _uia_describe(ctrl) -> dict:
    role = ctrl.friendly_class_name() or ""
    name = (ctrl.window_text() or "").strip()
    rect = ctrl.rectangle()
    item = {"name": name[:_UIA_NAME_LIMIT], "role": role,
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "cx": (rect.left + rect.right) // 2,
            "cy": (rect.top + rect.bottom) // 2}
    # `enabled` only carries information when False — omitting the common
    # True keeps include_all lists under the result-size spill threshold.
    try:
        if not bool(ctrl.is_enabled()):
            item["enabled"] = False
    except Exception:
        pass
    return item


def _uia_grid_ratio(hwnd: int, dlg) -> float:
    """Provider-grid → our-grid conversion factor for UIA rects.

    BoundingRectangles come straight from the target app's UIA provider
    (Electron/Chromium reports physical px) and are NOT virtualized to the
    caller; screenshots and pyautogui clicks run on this process's grid.
    At display scaling ≠100% or app zoom the grids diverge and raw provider
    coords miss by the scale factor (2026-09-04: every UIA-aimed click
    landed 1.5x off at 150% scaling). GetWindowRect IS virtualized to our
    grid, so its width vs the UIA window width is the factor.
    """
    try:
        wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
        ur = dlg.rectangle()
        uw, ww = ur.right - ur.left, wr - wl
        if uw <= 0 or ww <= 0:
            return 1.0
        ratio = ww / uw
    except Exception:
        return 1.0
    if 0.98 <= ratio <= 1.02:
        return 1.0  # same grid — don't drift on border/shadow noise
    return ratio if 0.25 <= ratio <= 4.0 else 1.0


def _uia_rescale(item: dict, ratio: float) -> dict:
    if ratio == 1.0:
        return item
    l, t, r, b = (round(v * ratio) for v in item["rect"])
    return {**item, "rect": [l, t, r, b], "cx": (l + r) // 2, "cy": (t + b) // 2}


def _apple_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _mac_uia_script(query: str = "") -> str:
    proc = (f'first application process whose name contains "{_apple_escape(query)}"'
            if query else "first application process whose frontmost is true")
    return (
        'on describeEl(e)\n'
        '\ttell application "System Events"\n'
        '\t\tset r to ""\n'
        '\t\tset n to ""\n'
        '\t\tset c to ""\n'
        '\t\tset en to true\n'
        '\t\ttry\n'
        '\t\t\tset r to role of e\n'
        '\t\tend try\n'
        '\t\ttry\n'
        '\t\t\tset n to name of e\n'
        '\t\tend try\n'
        '\t\ttry\n'
        '\t\t\tif n is "" then set n to description of e\n'
        '\t\tend try\n'
        '\t\ttry\n'
        '\t\t\tset {x, y} to position of e\n'
        '\t\t\tset {wd, ht} to size of e\n'
        '\t\t\tset c to "" & x & "," & y & "," & (x + wd) & "," & (y + ht)\n'
        '\t\tend try\n'
        '\t\ttry\n'
        '\t\t\tset en to enabled of e\n'
        '\t\tend try\n'
        '\t\treturn "" & r & tab & n & tab & c & tab & en & linefeed\n'
        '\tend tell\n'
        'end describeEl\n'
        'on walkEls(els, depth)\n'
        '\tset out to ""\n'
        '\trepeat with e in els\n'
        '\t\ttry\n'
        '\t\t\tset out to out & my describeEl(e)\n'
        '\t\tend try\n'
        '\t\tif (length of out) > 24000 then return out\n'
        '\t\tif depth < 6 then\n'
        '\t\t\ttry\n'
        '\t\t\t\tset out to out & my walkEls(UI elements of e, depth + 1)\n'
        '\t\t\tend try\n'
        '\t\tend if\n'
        '\tend repeat\n'
        '\treturn out\n'
        'end walkEls\n'
        'tell application "System Events"\n'
        '\tset frontWin to missing value\n'
        '\ttry\n'
        f'\t\tset frontWin to front window of ({proc})\n'
        '\tend try\n'
        '\tif frontWin is missing value then return ""\n'
        '\treturn my walkEls(UI elements of frontWin, 0)\n'
        'end tell'
    )


def _mac_uia_from_tsv(text: str, include_all: bool = False) -> list:
    """Parse the System Events element walk: role \\t name \\t x,y,x2,y2 \\t
    enabled — into the same element schema as the pywinauto path."""
    els = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        role, name, rect_s, en_s = (p.strip() for p in parts)
        try:
            rect = [int(v) for v in rect_s.split(",")]
            if len(rect) != 4 or rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
        except ValueError:
            continue
        if not include_all:
            if not name:
                continue
            rl = role.lower()
            if not any(k in rl for k in _MAC_UIA_ROLE_KEYWORDS):
                continue
        els.append({"name": name[:_UIA_NAME_LIMIT], "role": role, "rect": rect,
                    "cx": (rect[0] + rect[2]) // 2, "cy": (rect[1] + rect[3]) // 2,
                    "enabled": en_s != "false"})
        if len(els) >= _UIA_MAX_ELEMENTS:
            break
    return els


def _mac_uia_run(query: str, timeout: float = _UIA_DEFAULT) -> str:
    p = _run_cmd(["osascript", "-e", _mac_uia_script(query)], timeout=int(timeout))
    if p.returncode != 0:
        raise RuntimeError(
            "System Events UI query failed (grant the host terminal 辅助功能/自动化 "
            f"permission, or fall back to computer_screenshot): "
            f"{(p.stderr or b'').decode('utf-8', 'replace')[:160]}")
    return p.stdout.decode("utf-8", "replace")


def _uia_list(args: dict, query: str) -> str:
    include_all = bool(args.get("include_all"))
    timeout = _uia_timeout_arg(args)
    if _IS_WIN:
        def _walk():
            hwnd, dlg = _uia_wrap(query)  # reads work on background windows
            elements = []
            vs = _virtual_screen()
            vx2, vy2 = vs[0] + vs[2], vs[1] + vs[3]
            ratio = _uia_grid_ratio(hwnd, dlg)
            for ctrl in dlg.descendants():
                try:
                    item = _uia_describe(ctrl)
                except Exception:
                    continue
                if not include_all:
                    if not item["name"]:
                        continue
                    if not any(k in item["role"].lower() for k in _UIA_ROLE_KEYWORDS):
                        continue
                if item["rect"][2] <= item["rect"][0] or item["rect"][3] <= item["rect"][1]:
                    continue
                item = _uia_rescale(item, ratio)  # provider grid → our grid
                if item["rect"][0] > vx2 or item["rect"][1] > vy2 \
                        or item["rect"][2] < vs[0] or item["rect"][3] < vs[1]:
                    continue  # off-screen rect (minimized/hidden) — unusable
                elements.append(item)
                if len(elements) >= _UIA_MAX_ELEMENTS:
                    break
            return elements

        try:
            elements, err = _uia_call(_walk, "list", timeout)
        except LookupError as e:
            return tool_error(f"UIA connect failed (window gone? query mismatch?): {e}")
        except Exception as e:
            return tool_error(f"UIA walk failed (self-drawn UI may expose nothing; "
                              f"fall back to computer_screenshot): {e}")
        if err:
            return tool_error(err)
    else:
        try:
            text = _mac_uia_run(query, timeout)
        except RuntimeError as e:
            return tool_error(str(e))
        elements = _mac_uia_from_tsv(text, include_all)
    return tool_result(window=query or "(foreground)", elements=elements,
                       count=len(elements),
                       hint=None if elements else
                       "no interactive elements exposed — self-drawn UI? "
                       "use computer_screenshot + vision/ocr instead")


def _computer_uia(args: dict, **kw) -> str:
    action = str(args.get("action") or "list").lower()
    query = str(args.get("query") or "")
    timeout = _uia_timeout_arg(args)
    with _desk_lock:
        if action == "list":
            return _uia_list(args, query)
        if action == "click":
            target = str(args.get("name") or "").strip()
            if not target:
                return tool_error("name is required for click (element name substring "
                                  "from action='list')")
            try:
                index = max(1, int(args.get("index") or 1))
            except (TypeError, ValueError):
                return tool_error("index must be an integer (1-based match number)")
            if _IS_WIN:
                def _click():
                    hwnd, dlg = _uia_wrap(query)
                    # A real click lands on whatever is topmost at those
                    # screen coords — bring the target up first or the click
                    # hits the window covering it.
                    err = _force_foreground(hwnd)
                    if err:
                        raise RuntimeError(err)
                    matches = [c for c in dlg.descendants()
                               if target.lower() in (c.window_text() or "").lower()]
                    if len(matches) < index:
                        raise LookupError(f"no element named '{target}' (match "
                                          f"{index}/{len(matches)})")
                    m = matches[index - 1]
                    r = m.rectangle()
                    if r.right <= r.left or r.bottom <= r.top:
                        raise LookupError(f"element '{target}' exposes no usable "
                                          "rect — click a nearby element or use "
                                          "computer_click")
                    # Provider-grid coords must be converted to our click
                    # grid (see _uia_grid_ratio) — click_input would move the
                    # cursor to the raw provider rect and miss under scaling.
                    ratio = _uia_grid_ratio(hwnd, dlg)
                    cx, cy = _clamp_to_screen(
                        round((r.left + r.right) / 2 * ratio),
                        round((r.top + r.bottom) / 2 * ratio))
                    pyautogui.click(x=cx, y=cy, clicks=1,
                                    button="left", duration=0.15)

                try:
                    _, err = _uia_call(_click, "click", timeout)
                except LookupError as e:
                    return tool_error(f"{e}; run action='list' to see names")
                except pyautogui.FailSafeException:
                    return _failsafe_error()
                except Exception as e:
                    return tool_error(f"UIA click failed: {e}")
                if err:
                    return tool_error(err)
                return tool_result(success=True, name=target, index=index,
                                   hint="verify with computer_uia list or computer_screenshot diff")
            try:
                text = _mac_uia_run(query, timeout)
            except RuntimeError as e:
                return tool_error(str(e))
            matches = [e for e in _mac_uia_from_tsv(text, include_all=True)
                       if target.lower() in e["name"].lower()]
            if len(matches) < index:
                return tool_error(f"no element named '{target}' (match {index}/"
                                  f"{len(matches)}); run action='list' to see names")
            m = matches[index - 1]
            cx, cy = _clamp_to_screen(m["cx"], m["cy"])
            try:
                pyautogui.click(x=cx, y=cy, clicks=1, button="left", duration=0.15)
            except pyautogui.FailSafeException:
                return _failsafe_error()
            except Exception as e:
                return tool_error(f"UIA click failed: {e}")
            return tool_result(success=True, name=target, index=index,
                               hint="verify with computer_uia list or computer_screenshot diff")
    return tool_error("unknown action; use list | click")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="computer_screenshot",
    schema={
        "type": "function",
        "function": {
            "name": "computer_screenshot",
            "description": (
                "Capture the screen as a PNG (downscaled to max_width, "
                "default 1920). Returns the saved path, screen_size — the "
                "coordinate grid computer_click uses — scale (saved-image px "
                "per grid px), and a diff vs the previous shot to verify what "
                "changed. describe=true adds a vision caption (needs "
                "vision_model; otherwise use image_ocr)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "array", "items": {"type": "integer"},
                               "description": "Optional [x, y, width, height] in screen coordinates"},
                    "max_width": {"type": "integer",
                                  "description": "Downscale cap in pixels (default 1920)"},
                    "describe": {"type": "boolean",
                                 "description": "Also run vision analysis on the capture (default false)"},
                    "prompt": {"type": "string",
                               "description": "Vision prompt when describe=true"},
                },
            },
        },
    },
    handler=lambda args, **kw: _computer_screenshot(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
    read_only=True,
)

registry.register(
    name="computer_click",
    schema={
        "type": "function",
        "function": {
            "name": "computer_click",
            "description": (
                "Click at screen coordinates (from computer_uia cx/cy or a "
                "screenshot). query/hwnd names the target window — it is "
                "foregrounded before the click (required; no_window=true "
                "only for taskbar/desktop coordinates). Verify with "
                "computer_screenshot's diff. (Clicks that never land: "
                "elevated target or locked screen.)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Screen X"},
                    "y": {"type": "integer", "description": "Screen Y"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"],
                               "description": "Mouse button (default left)"},
                    "clicks": {"type": "integer", "description": "Number of clicks (default 1)"},
                    "double": {"type": "boolean", "description": "Shortcut for clicks=2"},
                    "query": {"type": "string",
                              "description": "Window title substring — foreground that window first"},
                    "hwnd": {"type": "integer",
                             "description": "Window handle from computer_windows — foreground it first"},
                    "no_window": {"type": "boolean",
                                  "description": "Act without a window target (taskbar/desktop coords, global hotkeys) — skip foregrounding"},
                },
                "required": ["x", "y"],
            },
        },
    },
    handler=lambda args, **kw: _computer_click(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_type",
    schema={
        "type": "function",
        "function": {
            "name": "computer_type",
            "description": (
                "Type text into a window's focused control. query/hwnd "
                "names the window — foregrounded first (required; "
                "no_window=true to type into whatever currently has focus). "
                "Text is pasted via clipboard (CJK-safe; user clipboard "
                "restored). mode='type': raw keystrokes, ASCII only, max 20 "
                "chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type (any Unicode)"},
                    "restore_clipboard": {"type": "boolean",
                                          "description": "Restore the previous clipboard after pasting (default true)"},
                    "mode": {"type": "string", "enum": ["paste", "type"],
                             "description": "paste (default) | type (ASCII keystrokes, <=20 chars)"},
                    "query": {"type": "string",
                              "description": "Window title substring — foreground that window first"},
                    "hwnd": {"type": "integer",
                             "description": "Window handle from computer_windows — foreground it first"},
                    "no_window": {"type": "boolean",
                                  "description": "Act without a window target (taskbar/desktop coords, global hotkeys) — skip foregrounding"},
                },
                "required": ["text"],
            },
        },
    },
    handler=lambda args, **kw: _computer_type(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_key",
    schema={
        "type": "function",
        "function": {
            "name": "computer_key",
            "description": (
                "Press a key or combo ('enter', 'ctrl+s', 'win+d'). "
                "query/hwnd names the window — foregrounded first (required; "
                "no_window=true for global combos like win+d / alt+tab). "
                "Aliases: esc/return/del/pgup/pgdn; cmd maps to the Windows "
                "key (Command on macOS)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string",
                             "description": "Key or '+'-joined combo, e.g. 'enter', 'ctrl+s', 'win+d'"},
                    "presses": {"type": "integer", "description": "Repeat count (default 1)"},
                    "query": {"type": "string",
                              "description": "Window title substring — foreground that window first"},
                    "hwnd": {"type": "integer",
                             "description": "Window handle from computer_windows — foreground it first"},
                    "no_window": {"type": "boolean",
                                  "description": "Act without a window target (taskbar/desktop coords, global hotkeys) — skip foregrounding"},
                },
                "required": ["keys"],
            },
        },
    },
    handler=lambda args, **kw: _computer_key(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_scroll",
    schema={
        "type": "function",
        "function": {
            "name": "computer_scroll",
            "description": (
                "Scroll up/down/left/right, optionally at screen (x, y). "
                "query/hwnd names the window — foregrounded first (required; "
                "no_window=true to scroll whatever is focused)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string",
                                  "enum": ["up", "down", "left", "right"],
                                  "description": "Scroll direction (default down)"},
                    "clicks": {"type": "integer", "description": "Scroll steps (default 3)"},
                    "x": {"type": "integer", "description": "Optional screen X to scroll at"},
                    "y": {"type": "integer", "description": "Optional screen Y to scroll at"},
                    "query": {"type": "string",
                              "description": "Window title substring — foreground that window first"},
                    "hwnd": {"type": "integer",
                             "description": "Window handle from computer_windows — foreground it first"},
                    "no_window": {"type": "boolean",
                                  "description": "Act without a window target (taskbar/desktop coords, global hotkeys) — skip foregrounding"},
                },
            },
        },
    },
    handler=lambda args, **kw: _computer_scroll(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_windows",
    schema={
        "type": "function",
        "function": {
            "name": "computer_windows",
            "description": (
                "List and manage windows. list returns visible windows "
                "(foreground first) with hwnd/title/rect. Other actions — "
                "focus/minimize/maximize/restore/close — target one window "
                "by query (title substring) or hwnd; focus also restores "
                "minimized windows; close is graceful (unsaved-data prompts "
                "still appear)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "focus", "minimize", "maximize", "restore", "close"],
                               "description": "Window operation (default list)"},
                    "query": {"type": "string",
                              "description": "Title/app substring to filter (list) or locate (other actions)"},
                    "hwnd": {"type": "integer",
                             "description": "Window handle (Windows) or list index (macOS) from a previous list"},
                },
            },
        },
    },
    handler=lambda args, **kw: _computer_windows(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_app",
    schema={
        "type": "function",
        "function": {
            "name": "computer_app",
            "description": (
                "Open or close apps. open: target is a URL, file, or app "
                "name (default handler; args adds CLI arguments). close: "
                "terminates all processes matching name — unsaved data is "
                "lost; prefer computer_windows close for a graceful close."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["open", "close"],
                               "description": "open (default) | close"},
                    "target": {"type": "string",
                               "description": "open: URL / file path / registered app name"},
                    "args": {"description": "open: string or array of arguments for the executable"},
                    "name": {"type": "string",
                             "description": "close: process name to terminate"},
                },
            },
        },
    },
    handler=lambda args, **kw: _computer_app(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_clipboard",
    schema={
        "type": "function",
        "function": {
            "name": "computer_clipboard",
            "description": (
                "Read, replace, or clear the system clipboard (text). "
                "computer_type uses the clipboard transiently when pasting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set", "clear"],
                               "description": "Clipboard operation (default get)"},
                    "text": {"type": "string", "description": "Text for set"},
                },
            },
        },
    },
    handler=lambda args, **kw: _computer_clipboard(args, **kw),
    check_fn=_check_desktop,
    toolset="computer",
)

registry.register(
    name="computer_uia",
    schema={
        "type": "function",
        "function": {
            "name": "computer_uia",
            "description": (
                "List a window's accessibility elements — the preferred way "
                "to locate controls (faster and more precise than "
                "screenshots). list returns name/role/rect plus the element "
                "center (cx, cy) to feed computer_click, and works on "
                "background windows. click clicks a named element (substring "
                "match, index for duplicates) and foregrounds its window "
                "first. Queries are bounded by timeout (default 30s, "
                "5-120). Self-drawn/canvas UI exposes no elements — use "
                "computer_screenshot instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "click"],
                               "description": "list (default) | click"},
                    "query": {"type": "string",
                              "description": "Window title substring (Windows) / app name substring (macOS); default: foreground window"},
                    "include_all": {"type": "boolean",
                                    "description": "list: return every element, not just interactive ones. "
                                   "Very verbose — on large windows the result can exceed the inline "
                                   "size cap and spill to disk (you'd see only a preview); prefer the "
                                   "default filtered list first"},
                    "name": {"type": "string",
                             "description": "click: element name substring from a previous list"},
                    "index": {"type": "integer",
                              "description": "click: 1-based match number when several elements share a name (default 1)"},
                    "timeout": {"type": "integer",
                                "description": "Wall-clock budget in seconds for the accessibility query "
                                               "(default 30, range 5-120). Raise for known-huge windows"},
                },
            },
        },
    },
    handler=lambda args, **kw: _computer_uia(args, **kw),
    check_fn=_check_uia,
    toolset="computer",
)
