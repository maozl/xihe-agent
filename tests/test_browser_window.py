"""L0/L1 tests for the browser-panel snap core (gateway/browser_window.py).

All Win32 access goes through a monkeypatched user32 shim that simulates
window/pid/z-order state; no real window is ever touched. The shim's
SetWindowPos honors the documented hWndInsertAfter polarity ("doc") or the
opposite one ("mirror") — the snap code must produce a correct z-order under
either, which is the point of the post-condition + mirror-fallback design.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="win32 snap core")

import gateway.browser_window as bw


class ShimUser32:
    def __init__(self):
        self.calls = []
        self.windows = {}   # hwnd -> {"pid": int, "rect": (l,t,r,b), "visible": bool}
        self.zorder = []    # top-most first
        self.foreground = None
        self.polarity = "doc"
        self.fail_setwindowpos = False
        self.fail_owner = False

    def add_window(self, hwnd, pid, rect=(100, 100, 900, 700), visible=True):
        self.windows[hwnd] = {"pid": pid, "rect": rect, "visible": visible}
        if hwnd not in self.zorder:
            self.zorder.append(hwnd)
        return self

    def IsWindow(self, hwnd):
        return hwnd in self.windows

    def IsWindowVisible(self, hwnd):
        return self.windows.get(hwnd, {}).get("visible", False)

    def GetWindowLongW(self, hwnd, index):
        return 0

    def IsIconic(self, hwnd):
        return False

    def IsZoomed(self, hwnd):
        return False

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, out):
        out._obj.value = self.windows.get(hwnd, {}).get("pid", 0)
        return 1

    def EnumWindows(self, cb, lparam):
        for hwnd in list(self.windows):
            if not cb(hwnd, lparam):
                break
        return True

    def GetWindow(self, hwnd, cmd):
        if cmd == bw.GW_OWNER:
            return self.windows.get(hwnd, {}).get("owner", 0)
        if hwnd not in self.zorder or cmd != bw.GW_HWNDPREV:
            return 0
        i = self.zorder.index(hwnd)
        return self.zorder[i - 1] if i > 0 else 0

    def SetWindowLongPtrW(self, hwnd, index, value):
        self.calls.append(("SetWindowLongPtrW", hwnd, index, value))
        if self.fail_owner:
            return 0
        old = self.windows.get(hwnd, {}).get("owner", 0)
        self.windows[hwnd]["owner"] = value
        return old

    def GetWindowRect(self, hwnd, out):
        w = self.windows.get(hwnd)
        if not w:
            return False
        r = out._obj
        r.left, r.top, r.right, r.bottom = w["rect"]
        return True

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        if cmd == bw.SW_HIDE:
            self.windows[hwnd]["visible"] = False
        elif cmd in (bw.SW_RESTORE, bw.SW_SHOWNOACTIVATE):
            self.windows[hwnd]["visible"] = True
        return True

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        self.calls.append(("SetWindowPos", hwnd, after, x, y, w, h, flags))
        if self.fail_setwindowpos or hwnd not in self.windows:
            return False
        if flags & bw.SWP_SHOWWINDOW:
            self.windows[hwnd]["visible"] = True
        if hwnd in self.zorder:
            self.zorder.remove(hwnd)
        if after == bw.HWND_TOP or after not in self.zorder:
            self.zorder.insert(0, hwnd)
        else:
            # doc polarity: `after` sits ABOVE hwnd; mirror: below
            below = 1 if self.polarity == "doc" else 0
            self.zorder.insert(self.zorder.index(after) + below, hwnd)
        return True

    def MonitorFromWindow(self, hwnd, flags):
        return 1

    def GetMonitorInfoW(self, hmon, out):
        rc = out._obj.rcWork
        rc.left, rc.top, rc.right, rc.bottom = 0, 0, 1920, 1040
        return True

    def swp(self):
        return [c for c in self.calls if c[0] == "SetWindowPos"]

    def shown(self):
        return [c for c in self.calls if c[0] == "ShowWindow"]

    def setlp(self):
        return [c for c in self.calls if c[0] == "SetWindowLongPtrW"]


@pytest.fixture
def shim(monkeypatch):
    s = ShimUser32()
    monkeypatch.setattr(bw, "_user32", s)
    return s


@pytest.fixture
def state(monkeypatch):
    st = {"hwnd": None, "pid": None, "snapped": None, "desktop_hwnd": None,
          "hidden": False, "owned": False}
    monkeypatch.setattr(bw, "_state", st)
    return st


def _patch_resolve(monkeypatch, pid=100, image="chrome.exe", wins=None):
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: pid)
    monkeypatch.setattr(bw, "_pid_image_basename", lambda p: image)
    monkeypatch.setattr(
        bw, "_windows_for_pid",
        lambda p, include_hidden=False: [(1, 50000)] if wins is None else wins)


# ---- resolve -----------------------------------------------------------------

def test_resolve_cached_hwnd_short_circuits(shim, state, monkeypatch):
    shim.add_window(1, pid=100)
    state.update(hwnd=1, pid=100)
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: (_ for _ in ()).throw(AssertionError()))
    assert bw._resolve_hwnd() == 1


def test_resolve_pid_reuse_guard(shim, state, monkeypatch):
    _patch_resolve(monkeypatch, pid=1234, image="notepad.exe")
    assert bw._resolve_hwnd() is None
    assert state["pid"] is None


def test_resolve_picks_largest_window(shim, state, monkeypatch):
    _patch_resolve(monkeypatch, wins=[(5, 10000), (7, 90000), (9, 40000)])
    assert bw._resolve_hwnd() == 7
    assert state["hwnd"] == 7 and state["pid"] == 100


def test_resolve_no_windows(shim, state, monkeypatch):
    _patch_resolve(monkeypatch, wins=[])
    assert bw._resolve_hwnd() is None


def test_resolve_resurfaces_hidden_chrome(shim, state, monkeypatch):
    # serve restart lost hidden/snapped state; the SW_HIDE'd frame is
    # invisible to the visible-only enumeration — resolve must fall back to
    # all-windows enumeration and re-show the main frame. _patch_resolve is
    # NOT used: the real shim enumeration is what the fallback exercises.
    shim.add_window(1, pid=100, visible=False)
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: 100)
    monkeypatch.setattr(bw, "_pid_image_basename", lambda p: "chrome.exe")
    assert bw._resolve_hwnd() == 1
    assert shim.shown() == [("ShowWindow", 1, bw.SW_SHOWNOACTIVATE)]
    assert shim.windows[1]["visible"] is True


# ---- snap --------------------------------------------------------------------

def test_snap_clamps_to_chrome_minimum(shim, state, monkeypatch):
    # desktop (2) above chrome (1) in z-order, so snap must actually re-place
    shim.add_window(2, pid=55, rect=(0, 0, 1280, 800)).add_window(1, pid=100)
    _patch_resolve(monkeypatch)
    res = bw.snap(10, 20, 100, 50, 2)
    assert res == {"ok": True, "rect": [10, 20, bw.MIN_W, bw.MIN_H]}
    assert state["snapped"] == (10, 20, bw.MIN_W, bw.MIN_H)
    hwnd, after, x, y, w, h, flags = shim.swp()[0][1:]
    assert (hwnd, x, y, w, h) == (1, 10, 20, bw.MIN_W, bw.MIN_H)
    assert flags == bw.SWP_NOACTIVATE | bw.SWP_SHOWWINDOW


def test_snap_rejects_non_int_args(shim, state, monkeypatch):
    _patch_resolve(monkeypatch)
    for bad in ("10", 10.5, True, None):
        assert bw.snap(bad, 0, 600, 400, 2)["reason"] == "bad-args"
        assert bw.snap(0, 0, 600, 400, bad)["reason"] == "bad-args"
    assert shim.swp() == []


def test_snap_without_chrome_window(shim, state, monkeypatch):
    shim.add_window(2, pid=55)
    _patch_resolve(monkeypatch, wins=[])
    assert bw.snap(0, 0, 600, 400, 2)["reason"] == "no-chrome-window"


def test_snap_bad_desktop_hwnd(shim, state, monkeypatch):
    _patch_resolve(monkeypatch)
    assert bw.snap(0, 0, 600, 400, 424242)["reason"] == "bad-desktop-hwnd"


# ---- z-order polarity (post-condition + mirror fallback) ----------------------
# Owner binding is the primary snap path now; these pin the ANCHOR fallback
# for when the cross-process own doesn't take — hence fail_owner=True.

def test_snap_doc_polarity_one_call(shim, state, monkeypatch):
    shim.polarity = "doc"
    shim.fail_owner = True
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100)
    _patch_resolve(monkeypatch)
    assert bw.snap(0, 0, 600, 400, 2)["ok"] is True
    assert len(shim.swp()) == 1
    assert shim.GetWindow(2, bw.GW_HWNDPREV) == 1


def test_snap_mirror_polarity_recovers(shim, state, monkeypatch):
    shim.polarity = "mirror"
    shim.fail_owner = True
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100)
    _patch_resolve(monkeypatch)
    assert bw.snap(0, 0, 600, 400, 2)["ok"] is True
    assert len(shim.swp()) == 2  # anchor attempt failed the post-check → mirror call
    assert shim.GetWindow(2, bw.GW_HWNDPREV) == 1


def test_snap_place_failure_reports(shim, state, monkeypatch):
    shim.fail_setwindowpos = True
    shim.fail_owner = True
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100)
    _patch_resolve(monkeypatch)
    assert bw.snap(0, 0, 600, 400, 2)["reason"] == "place-failed"
    assert len(shim.swp()) == 2
    assert state["snapped"] is None


def test_place_above_in_place_still_positions(shim, monkeypatch):
    # zorder=[1,2]: chrome already directly above → no re-anchoring (self-
    # insertion is undefined), but the rect may have changed so it still
    # positions at HWND_TOP
    shim.add_window(1, pid=100).add_window(2, pid=55)
    assert bw._place_above(1, 2, 10, 20, 600, 400) is True
    hwnd, after, x, y, w, h, _ = shim.swp()[0][1:]
    assert (hwnd, after, x, y, w, h) == (1, bw.HWND_TOP, 10, 20, 600, 400)


def test_place_above_hidden_chrome_in_place_reshown(shim, monkeypatch):
    # hidden chrome keeps its z-order slot (SW_HIDE doesn't reorder), so the
    # anchor resolves to chrome itself — SWP_SHOWWINDOW reveals it in place
    shim.add_window(1, pid=100, visible=False).add_window(2, pid=55)
    assert bw._place_above(1, 2, 0, 0, 600, 400) is True
    assert len(shim.swp()) == 1
    assert shim.swp()[0][7] & bw.SWP_SHOWWINDOW
    assert shim.windows[1]["visible"] is True


def test_place_above_hidden_chrome_out_of_place_repositioned(shim, monkeypatch):
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100, visible=False)
    assert bw._place_above(1, 2, 0, 0, 600, 400) is True
    assert len(shim.swp()) >= 1  # hidden chrome must go through SWP_SHOWWINDOW


# ---- hide foreground guard -----------------------------------------------------

def test_hide_when_other_window_foreground(shim, state, monkeypatch):
    shim.add_window(1, pid=100)
    shim.foreground = 999
    _patch_resolve(monkeypatch)
    res = bw.hide()
    assert res == {"ok": True, "hidden": True}
    assert state["hidden"] is True
    assert shim.shown() == [("ShowWindow", 1, bw.SW_HIDE)]


def test_hide_refuses_while_chrome_is_foreground(shim, state, monkeypatch):
    shim.add_window(1, pid=100)
    shim.foreground = 1
    _patch_resolve(monkeypatch)
    res = bw.hide()
    assert res["hidden"] is False and res["reason"] == "chrome-foreground"
    assert shim.shown() == []
    assert state["hidden"] is False


def test_hide_refuses_while_chrome_popup_foreground(shim, state, monkeypatch):
    shim.add_window(1, pid=100).add_window(3, pid=100, rect=(0, 0, 200, 100))
    shim.foreground = 3  # same pid → still "user is inside chrome"
    _patch_resolve(monkeypatch)
    assert bw.hide()["hidden"] is False


# ---- owner binding (primary snap path) -------------------------------------------

def test_snap_owns_chrome_to_desktop(shim, state, monkeypatch):
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100)
    _patch_resolve(monkeypatch)
    assert bw.snap(0, 0, 600, 400, 2)["ok"] is True
    assert shim.GetWindow(1, bw.GW_OWNER) == 2
    assert state["owned"] is True
    # owned path needs no anchor math — a single HWND_TOP positioning
    assert len(shim.swp()) == 1
    assert shim.swp()[0][2] == bw.HWND_TOP


def test_snap_owner_failure_falls_back_to_anchor(shim, state, monkeypatch):
    shim.fail_owner = True
    shim.polarity = "doc"
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100)
    _patch_resolve(monkeypatch)
    assert bw.snap(0, 0, 600, 400, 2)["ok"] is True
    assert state["owned"] is False
    assert shim.GetWindow(2, bw.GW_HWNDPREV) == 1


def test_release_unowns_chrome(shim, state, monkeypatch):
    shim.add_window(2, pid=55).add_window(1, pid=100)
    state.update(snapped=(0, 0, 600, 400), desktop_hwnd=2, owned=True, pid=100)
    _patch_resolve(monkeypatch)
    bw.release()
    assert shim.setlp() == [("SetWindowLongPtrW", 1, bw.GWLP_HWNDPARENT, 0)]
    assert shim.GetWindow(1, bw.GW_OWNER) == 0
    assert state["owned"] is False


def test_status_heals_orphaned_owner(shim, state, monkeypatch):
    import tools.browser_tool as bt
    shim.add_window(1, pid=100)
    monkeypatch.setattr(bt, "_cdp_port_open", lambda timeout=0.5: True)
    _patch_resolve(monkeypatch)
    # desktop window destroyed without a release (crash): IsWindow(2) is False
    state.update(desktop_hwnd=2, owned=True, pid=100)
    bw.status()
    assert shim.setlp() == [("SetWindowLongPtrW", 1, bw.GWLP_HWNDPARENT, 0)]
    assert state["owned"] is False


# ---- show / release -------------------------------------------------------------

def test_show_replaces_at_cached_rect(shim, state, monkeypatch):
    shim.add_window(9, pid=77).add_window(2, pid=55).add_window(1, pid=100)
    state.update(snapped=(10, 20, 600, 400), desktop_hwnd=2)
    _patch_resolve(monkeypatch)
    assert bw.show()["ok"] is True
    assert shim.swp()[0][3:7] == (10, 20, 600, 400)
    assert state["hidden"] is False


def test_show_without_ever_snapping(shim, state, monkeypatch):
    _patch_resolve(monkeypatch)
    assert bw.show()["reason"] == "never-snapped"


def test_release_floats_beside_desktop(shim, state, monkeypatch):
    shim.add_window(2, pid=55, rect=(1200, 100, 1900, 700))
    shim.add_window(1, pid=100, rect=(100, 100, 900, 700), visible=False)
    state.update(snapped=(1200, 100, 700, 600), desktop_hwnd=2, hidden=True, pid=100)
    _patch_resolve(monkeypatch)
    res = bw.release()
    assert res == {"ok": True, "was_snapped": True}
    assert state["snapped"] is None and state["hidden"] is False
    # hidden chrome is re-shown, then placed to the LEFT of the desktop window
    # (right side overflows the 1920-wide work area), top aligned
    assert shim.shown() == [("ShowWindow", 1, bw.SW_SHOWNOACTIVATE)]
    hwnd, after, x, y, w, h, _ = shim.swp()[-1][1:]
    assert (hwnd, after, x, y, w, h) == (1, bw.HWND_TOP, 1200 - 800 - 16, 100, 800, 600)


# ---- status (port probe stubbed; never touches the real 9222) -------------------

def test_status_not_running(shim, state, monkeypatch):
    import tools.browser_tool as bt
    monkeypatch.setattr(bt, "_cdp_port_open", lambda timeout=0.5: False)
    res = bw.status()
    assert res["ok"] is True and res["running"] is False and res["hwnd"] is None


def test_status_running_reports_rect(shim, state, monkeypatch):
    import tools.browser_tool as bt
    shim.add_window(1, pid=100)
    monkeypatch.setattr(bt, "_cdp_port_open", lambda timeout=0.5: True)
    _patch_resolve(monkeypatch)
    res = bw.status()
    assert res["running"] is True and res["hwnd"] == 1
    assert res["rect"] == [100, 100, 900, 700]


# ---- restart (kill + relaunch; everything mocked, no real chrome) --------------

class _Port:
    """Fake _cdp_port_open: open until close_after checks have passed."""

    def __init__(self, open_=True, close_after=0):
        self.open, self.calls, self.close_after = open_, 0, close_after

    def __call__(self, timeout=0.25):
        self.calls += 1
        return self.open and not (self.close_after and self.calls > self.close_after)


@pytest.fixture
def restart_env(monkeypatch):
    import types

    import tools.browser_tool as bt
    killed, cleaned, relaunched = [], [], []
    monkeypatch.setattr(bw, "time", types.SimpleNamespace(sleep=lambda s: None))
    monkeypatch.setattr(bw.subprocess, "run",
                        lambda cmd, **kw: killed.append(tuple(cmd)))
    monkeypatch.setattr(bt, "_cleanup_browser", lambda: cleaned.append(1))
    monkeypatch.setattr(bt, "_launch_cdp_chrome",
                        lambda: relaunched.append(1) or True)
    return killed, cleaned, relaunched


def test_restart_graceful_close(restart_env, monkeypatch):
    killed, cleaned, relaunched = restart_env
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: 100)
    monkeypatch.setattr(bw, "_pid_image_basename", lambda p: "chrome.exe")
    monkeypatch.setattr("tools.browser_tool._cdp_port_open",
                        _Port(open_=True, close_after=1))
    res = bw.restart()
    assert res == {"restarted": True}
    assert killed == [("taskkill", "/PID", "100")]   # no /F escalation
    assert cleaned and relaunched


def test_restart_escalates_to_force(restart_env, monkeypatch):
    killed, cleaned, relaunched = restart_env
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: 100)
    monkeypatch.setattr(bw, "_pid_image_basename", lambda p: "chrome.exe")
    monkeypatch.setattr("tools.browser_tool._cdp_port_open", _Port(open_=True))
    res = bw.restart()
    assert res == {"restarted": True}
    assert killed[0] == ("taskkill", "/PID", "100")
    assert killed[-1] == ("taskkill", "/F", "/PID", "100")


def test_restart_refuses_unowned_port(restart_env, monkeypatch):
    killed, cleaned, relaunched = restart_env
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: None)
    monkeypatch.setattr("tools.browser_tool._cdp_port_open", _Port(open_=True))
    res = bw.restart()
    assert res == {"restarted": False, "reason": "no-owned-pid"}
    assert not killed and not cleaned and not relaunched


def test_restart_pid_reuse_guard(restart_env, monkeypatch):
    killed, cleaned, relaunched = restart_env
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: 100)
    monkeypatch.setattr(bw, "_pid_image_basename", lambda p: "notepad.exe")
    monkeypatch.setattr("tools.browser_tool._cdp_port_open", _Port(open_=True))
    res = bw.restart()
    assert res == {"restarted": False, "reason": "no-owned-pid"}
    assert not killed


def test_restart_chrome_already_dead(restart_env, monkeypatch):
    killed, cleaned, relaunched = restart_env
    monkeypatch.setattr(bw, "_read_cdp_pid", lambda: 100)
    monkeypatch.setattr(bw, "_pid_image_basename", lambda p: "chrome.exe")
    monkeypatch.setattr("tools.browser_tool._cdp_port_open", _Port(open_=False))
    res = bw.restart()
    assert res == {"restarted": True}
    # guarded pid still killed even with the port closed: a zombie chrome.exe
    # holds the cdp-profile lock and a relaunch would just signal it and exit
    assert killed == [("taskkill", "/PID", "100")]
    assert cleaned and relaunched
