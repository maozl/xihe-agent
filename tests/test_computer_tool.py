"""L1 — computer tool handlers with the desktop boundary mocked out.

No real mouse moves, no real clipboard writes, no real screen captures: each
test swaps the module globals (pyautogui / win32* / ImageGrab / comtypes) or
the _uia_window_wrapper seam for fakes, mirroring the test_terminal_tool
pattern. Pillow itself stays real so screenshot diffing is exercised on
actual pixel data.

macOS code paths are driven by flipping ct._IS_WIN — every branch reads the
flag at call time — and faking _run_cmd (pbcopy/pbpaste/osascript/open).
"""
import json
import sys
import threading
import time
from collections import namedtuple
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

import tools.computer_tool as ct

pytestmark = pytest.mark.skipif(not ct._DESKTOP_OK,
                                reason="desktop deps unavailable")

# Windows-branch tests fake win32 module globals that don't exist on a macOS
# host — skip there; the mac-branch tests run on both.
requires_win = pytest.mark.skipif(sys.platform != "win32",
                                  reason="win32 branch")

ALL_COMPUTER_TOOLS = [
    "computer_screenshot", "computer_click", "computer_type",
    "computer_key", "computer_scroll", "computer_windows",
    "computer_app", "computer_clipboard", "computer_uia",
]

Rect = namedtuple("Rect", "left top right bottom")


class FakePyautogui:
    KEYBOARD_KEYS = ["enter", "escape", "delete", "insert", "pageup",
                     "pagedown", "ctrl", "alt", "shift", "win", "winleft",
                     "winright", "s", "d", "f4", "tab", "a", "home", "end"]
    FailSafeException = ct.pyautogui.FailSafeException

    def __init__(self, size=(1920, 1080)):
        self.calls = []
        self.fail_on = None
        self._size = size

    def size(self):
        return self._size

    def _maybe_fail(self, name):
        if self.fail_on == name:
            raise self.FailSafeException()

    def click(self, x, y, clicks=1, button="left", duration=0):
        self._maybe_fail("click")
        self.calls.append(("click", x, y, clicks, button))

    def write(self, text, interval=0):
        self._maybe_fail("write")
        self.calls.append(("write", text))

    def hotkey(self, *keys):
        self._maybe_fail("hotkey")
        self.calls.append(("hotkey", keys))

    def press(self, key, presses=1):
        self._maybe_fail("press")
        self.calls.append(("press", key, presses))

    def scroll(self, amount, x=None, y=None):
        self._maybe_fail("scroll")
        self.calls.append(("scroll", amount, x, y))

    def hscroll(self, amount, x=None, y=None):
        self._maybe_fail("hscroll")
        self.calls.append(("hscroll", amount, x, y))


class FakeRunner:
    """Stands in for _run_cmd (pbcopy/pbpaste/osascript/open on macOS)."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, args, input_bytes=None, timeout=15):
        self.calls.append((tuple(args), input_bytes))
        return SimpleNamespace(returncode=self.returncode,
                               stdout=self.stdout.encode("utf-8"),
                               stderr=self.stderr.encode("utf-8"))


class FakeWin32Clipboard:
    def __init__(self, busy_first=0):
        self.data = ""
        self.set_history = []
        self.busy_first = busy_first  # 前N次 OpenClipboard 模拟被别的进程占用

    def OpenClipboard(self):
        if self.busy_first > 0:
            self.busy_first -= 1
            raise OSError(5, "OpenClipboard", "denied")

    def CloseClipboard(self):
        pass

    def EmptyClipboard(self):
        self.data = ""

    def SetClipboardData(self, fmt, text):
        self.data = text
        self.set_history.append(text)

    def GetClipboardData(self, fmt):
        return self.data


class FakeWin32Gui:
    def __init__(self, windows, fg_hwnd):
        self.windows = windows
        self.fg = fg_hwnd
        self.shown = []
        self.posted = []
        self.foregrounded = []
        self.attach_calls = []
        self.attach_fail = False   # AttachThreadInput → error 5 (提权前台)
        self.locked = False        # SetForegroundWindow 被前台锁挡下

    def GetForegroundWindow(self):
        return self.fg

    def IsWindowVisible(self, hwnd):
        return hwnd in self.windows

    def GetWindowText(self, hwnd):
        return self.windows[hwnd]["title"]

    def GetWindowLong(self, hwnd, idx):
        return self.windows[hwnd].get("exstyle", 0)

    def GetWindowRect(self, hwnd):
        return tuple(self.windows[hwnd]["rect"])

    def IsIconic(self, hwnd):
        return self.windows[hwnd].get("iconic", False)

    def EnumWindows(self, cb, extra):
        for hwnd in self.windows:
            cb(hwnd, extra)

    def ShowWindow(self, hwnd, cmd):
        self.shown.append((hwnd, cmd))

    def BringWindowToTop(self, hwnd):
        pass

    def SetForegroundWindow(self, hwnd):
        self.foregrounded.append(hwnd)
        if not self.locked:
            self.fg = hwnd

    def PostMessage(self, hwnd, msg, wparam, lparam):
        self.posted.append((hwnd, msg))

    def AttachThreadInput(self, fg_tid, tid, attach):
        if self.attach_fail:
            raise OSError(5, "AttachThreadInput", "denied")
        self.attach_calls.append((fg_tid, tid, attach))
        return True


class FakeWin32Process:
    def __init__(self, gui):
        self._gui = gui

    # real win32gui has no GetWindowThreadProcessId — it lives here
    def GetWindowThreadProcessId(self, hwnd):
        return (hwnd * 10,)

    def AttachThreadInput(self, fg_tid, tid, attach):
        return self._gui.AttachThreadInput(fg_tid, tid, attach)


class FakeImageGrab:
    def __init__(self, img):
        self.img = img
        self.bboxes = []

    def grab(self, all_screens=True, bbox=None):
        self.bboxes.append(bbox)
        return self.img.copy()


class FakeProc:
    def __init__(self, pid, name):
        self.pid = pid
        self.info = {"name": name}
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakePsutil:
    def __init__(self, procs):
        self.procs = procs

    def process_iter(self, attrs):
        return list(self.procs)

    def wait_procs(self, procs, timeout=None):
        return (list(procs), [])


def patch_uia(monkeypatch, ctrls, win_rect=None):
    """Fake the wrapper-construction seam: whatever hwnd is resolved, the
    walk sees `ctrls` as the descendants of the target window. win_rect is
    the dlg's own rect on the PROVIDER grid (vs the win32gui window rect on
    our grid) — omit it and grids are treated as identical."""
    class _Dlg:
        def descendants(self):
            return list(ctrls)

        def rectangle(self):
            if win_rect is None:
                raise AttributeError("no provider rect in this fake")
            return Rect(*win_rect)

    monkeypatch.setattr(ct, "_uia_window_wrapper", lambda hwnd: _Dlg())


class FakeCtrl:
    def __init__(self, name, role, rect, enabled=True):
        self._name = name
        self._role = role
        self._rect = Rect(*rect)
        self._enabled = enabled

    def friendly_class_name(self):
        return self._role

    def window_text(self):
        return self._name

    def rectangle(self):
        return self._rect

    def is_enabled(self):
        return self._enabled


class FakeComtypes:
    def __init__(self):
        self.initialized = 0

    def CoInitialize(self):
        self.initialized += 1


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_COMPUTER_DIR", tmp_path / "shots")
    monkeypatch.setattr(ct, "_last_shot", None)
    monkeypatch.setattr(ct, "_uia_stuck", None)


@pytest.fixture
def pag(monkeypatch):
    fake = FakePyautogui()
    monkeypatch.setattr(ct, "pyautogui", fake)
    return fake


@pytest.fixture
def mac(monkeypatch):
    """Drive the macOS branches on any host — branches read _IS_WIN at
    call time, so flipping the flag reroutes handlers without touching
    the import-time gates."""
    monkeypatch.setattr(ct, "_IS_WIN", False)
    return None


@pytest.fixture
def clip(monkeypatch):
    fake = FakeWin32Clipboard()
    monkeypatch.setattr(ct, "win32clipboard", fake)
    return fake


def _win_fake():
    return FakeWin32Gui(
        windows={
            1: {"title": "记事本 - a.txt", "rect": (0, 0, 800, 600)},
            2: {"title": "Chrome - browser", "rect": (100, 50, 1200, 900),
                "iconic": True},
            3: {"title": "ToolWindow hidden", "rect": (0, 0, 10, 10),
                "exstyle": 0x80},
            4: {"title": "", "rect": (0, 0, 10, 10)},
            5: {"title": "Explorer", "rect": (0, 0, 400, 300)},
        },
        fg_hwnd=1,
    )


@pytest.fixture
def wgui(monkeypatch):
    fake = _win_fake()
    monkeypatch.setattr(ct, "win32gui", fake)
    monkeypatch.setattr(ct, "win32process", FakeWin32Process(fake))
    return fake


# ---- registry / toolset wiring -------------------------------------------

def test_registry_and_toolset_alignment():
    from core.toolsets import TOOLSETS
    from tools import registry

    registered = {n for n in registry.snapshot_names()
                  if registry.get_toolset_for_tool(n) == "computer"}
    assert registered == set(ALL_COMPUTER_TOOLS)
    assert set(TOOLSETS["computer"]["tools"]) == registered


def test_check_gates(monkeypatch):
    assert ct._check_desktop() is True
    monkeypatch.setattr(ct, "_DESKTOP_OK", False)
    assert ct._check_desktop() is False
    monkeypatch.setattr(ct, "_UIA_OK", False)
    assert ct._check_uia() is False


def test_not_subagent_blocked():
    from core.toolsets import SUBAGENT_BLOCKED_TOOLS

    assert not (set(ALL_COMPUTER_TOOLS) & SUBAGENT_BLOCKED_TOOLS)


def test_read_only_flags():
    from tools import registry

    assert registry.is_read_only("computer_screenshot") is True
    for name in ALL_COMPUTER_TOOLS:
        if name != "computer_screenshot":
            assert registry.is_read_only(name) is False


# ---- mouse / keyboard ----------------------------------------------------

def test_click_clamps_to_virtual_screen(pag, monkeypatch):
    monkeypatch.setattr(ct, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    data = json.loads(ct._computer_click({"x": 5000, "y": -50,
                                          "no_window": True}))
    assert data["success"] is True
    assert pag.calls == [("click", 1919, 0, 1, "left")]


def test_click_double_and_button(pag):
    json.loads(ct._computer_click({"x": 10, "y": 10, "button": "right",
                                   "double": True, "no_window": True}))
    assert pag.calls[-1] == ("click", 10, 10, 2, "right")


def test_click_failsafe_returns_error(pag):
    pag.fail_on = "click"
    data = json.loads(ct._computer_click({"x": 10, "y": 10, "no_window": True}))
    assert "急停" in data["error"]
    assert data["failsafe"] is True


def test_click_requires_coordinates(pag):
    assert "error" in json.loads(ct._computer_click({"x": 10, "no_window": True}))


@requires_win
def test_type_cjk_uses_clipboard_paste_and_restores(pag, clip):
    clip.data = "user-data"
    data = json.loads(ct._computer_type({"text": "你好，世界",
                                         "no_window": True}))
    assert data["method"] == "clipboard-paste"
    assert ("hotkey", ("ctrl", "v")) in pag.calls
    # set(新文本) → 粘贴 → set(还原旧文本)
    assert clip.set_history == ["你好，世界", "user-data"]
    assert clip.data == "user-data"


@requires_win
def test_type_restore_opt_out(pag, clip):
    clip.data = "user-data"
    json.loads(ct._computer_type({"text": "hi", "restore_clipboard": False,
                                  "no_window": True}))
    assert clip.set_history == ["hi"]
    assert clip.data == "hi"


@requires_win
def test_type_ascii_fast_path_opt_in(pag, clip):
    data = json.loads(ct._computer_type({"text": "hello", "mode": "type",
                                         "no_window": True}))
    assert data["method"] == "keystrokes"
    assert ("write", "hello") in pag.calls
    assert clip.set_history == []          # 快路径不碰剪贴板
    # 不带 mode 时同样内容仍走粘贴（默认路径唯一）
    data = json.loads(ct._computer_type({"text": "hello", "no_window": True}))
    assert data["method"] == "clipboard-paste"


@requires_win
def test_key_alias_normalization(pag):
    json.loads(ct._computer_key({"keys": "esc", "no_window": True}))
    assert pag.calls[-1] == ("press", "escape", 1)
    json.loads(ct._computer_key({"keys": "ctrl+return", "no_window": True}))
    assert pag.calls[-1] == ("hotkey", ("ctrl", "enter"))
    json.loads(ct._computer_key({"keys": "WIN+D", "no_window": True}))
    assert pag.calls[-1] == ("hotkey", ("win", "d"))
    json.loads(ct._computer_key({"keys": "tab", "presses": 3,
                                 "no_window": True}))
    assert pag.calls[-1] == ("press", "tab", 3)


def test_key_rejects_unknown(pag):
    assert "unknown key" in json.loads(
        ct._computer_key({"keys": "bogus", "no_window": True}))["error"]


def test_key_aliases_per_platform(mac):
    aliases = ct._key_aliases()
    assert aliases["cmd"] == "command"
    assert aliases["win"] == "command" and aliases["super"] == "command"


@requires_win
def test_key_aliases_win_default():
    assert ct._key_aliases() is ct._KEY_ALIASES
    assert ct._key_aliases()["cmd"] == "win"


def test_scroll_directions(pag):
    json.loads(ct._computer_scroll({"direction": "up", "clicks": 2,
                                    "no_window": True}))
    assert pag.calls[-1][1] == -2
    json.loads(ct._computer_scroll({"direction": "right", "clicks": 4, "x": 5,
                                    "y": 5, "no_window": True}))
    assert pag.calls[-1] == ("hscroll", 4, 5, 5)


def test_input_ops_require_explicit_target(pag):
    # 不点名窗口的输入直接拒绝——前置是强制项，不靠模型自觉
    msg = "query or hwnd is required"
    assert msg in json.loads(ct._computer_click({"x": 10, "y": 10}))["error"]
    assert msg in json.loads(ct._computer_type({"text": "hi"}))["error"]
    assert msg in json.loads(ct._computer_key({"keys": "enter"}))["error"]
    assert msg in json.loads(ct._computer_scroll({"direction": "down"}))["error"]
    assert pag.calls == []  # 拒绝时不产生任何输入


# ---- screenshot ----------------------------------------------------------

@requires_win
def test_screenshot_downscales_and_reports_scale(monkeypatch):
    grab = FakeImageGrab(Image.new("RGB", (3840, 2160), "black"))
    monkeypatch.setattr(ct, "ImageGrab", grab)
    data = json.loads(ct._computer_screenshot({}))
    assert data["width"] == 1920 and data["height"] == 1080
    assert data["screen_size"] == [3840, 2160]   # Windows: 网格 == 捕获像素
    assert data["scale"] == 0.5
    from pathlib import Path
    assert Path(data["path"]).is_file()
    assert data["diff"] is None                  # 首张没有比对基准


@requires_win
def test_screenshot_diff_vs_previous(monkeypatch):
    img1 = Image.new("RGB", (200, 100), "black")
    img2 = Image.new("RGB", (200, 100), "black")
    ImageDraw.Draw(img2).rectangle([150, 50, 179, 69], fill="white")
    monkeypatch.setattr(ct, "ImageGrab", FakeImageGrab(img1))

    first = json.loads(ct._computer_screenshot({}))
    assert first["diff"] is None

    monkeypatch.setattr(ct, "ImageGrab", FakeImageGrab(img2))
    second = json.loads(ct._computer_screenshot({}))
    assert second["diff"]["changed"] is True
    assert second["diff"]["changed_region"] == [150, 50, 30, 20]

    monkeypatch.setattr(ct, "ImageGrab", FakeImageGrab(img2.copy()))
    third = json.loads(ct._computer_screenshot({}))
    assert third["diff"]["changed"] is False


@requires_win
def test_screenshot_describe_fallback(monkeypatch):
    import tools.vision_tools as vision_tools
    monkeypatch.setattr(vision_tools, "describe_image_sync",
                        lambda path, prompt=None: "(vision not available)")
    monkeypatch.setattr(ct, "ImageGrab",
                        FakeImageGrab(Image.new("RGB", (100, 80), "black")))
    data = json.loads(ct._computer_screenshot({"describe": True}))
    assert data["description"] is None
    assert "image_ocr" in data["hint"]


def test_screenshot_retina_grid_maps_to_click_space(mac, pag, monkeypatch):
    # Retina：捕获 200x100 物理像素，点击网格是 100x50 逻辑点
    pag._size = (100, 50)
    img1 = Image.new("RGB", (200, 100), "black")
    img2 = Image.new("RGB", (200, 100), "black")
    ImageDraw.Draw(img2).rectangle([150, 50, 179, 69], fill="white")
    monkeypatch.setattr(ct, "ImageGrab", FakeImageGrab(img1))
    first = json.loads(ct._computer_screenshot({}))
    assert first["screen_size"] == [100, 50]
    assert first["scale"] == 2.0

    monkeypatch.setattr(ct, "ImageGrab", FakeImageGrab(img2))
    second = json.loads(ct._computer_screenshot({}))
    # diff region 换算到点击网格（半幅），可直接喂 computer_click
    assert second["diff"]["changed_region"] == [75, 25, 15, 10]


def test_grid_size_per_platform(mac, pag):
    pag._size = (1440, 900)
    assert ct._grid_size(Image.new("RGB", (2880, 1800))) == [1440, 900]


# ---- windows (Windows) ---------------------------------------------------

@requires_win
def test_windows_list_filters_and_sorts(wgui):
    data = json.loads(ct._computer_windows({"action": "list"}))
    titles = [w["title"] for w in data["windows"]]
    # 前台优先；TOOLWINDOW 与无标题窗口被过滤
    assert titles[0] == "记事本 - a.txt"
    assert set(titles[1:]) == {"Chrome - browser", "Explorer"}
    assert data["count"] == 3
    assert data["windows"][0]["foreground"] is True
    assert data["windows"][1]["minimized"] is True


@requires_win
def test_windows_focus_uses_attach_thread_input(wgui):
    data = json.loads(ct._computer_windows({"action": "focus", "query": "Chrome"}))
    assert data["success"] is True
    assert wgui.foregrounded == [2]
    assert (10, 20, True) in wgui.attach_calls and (10, 20, False) in wgui.attach_calls


@requires_win
def test_windows_close_and_show(wgui):
    import win32con
    json.loads(ct._computer_windows({"action": "close", "hwnd": 1}))
    assert wgui.posted == [(1, win32con.WM_CLOSE)]
    json.loads(ct._computer_windows({"action": "minimize", "hwnd": 5}))
    assert wgui.shown == [(5, win32con.SW_MINIMIZE)]
    assert "error" in json.loads(ct._computer_windows({"action": "bogus", "hwnd": 1}))


@requires_win
def test_windows_no_match_errors(wgui):
    assert "error" in json.loads(ct._computer_windows({"action": "focus",
                                                       "query": "不存在"}))


# ---- windows (macOS) -----------------------------------------------------

MAC_WIN_TSV = ("Safari\tGitHub — Safari\t10,20,1210,920\tfalse\tfalse\n"
               "Code\tmain.py — Code\t0,0,1000,800\ttrue\ttrue\n")


def test_mac_windows_tsv_parse():
    wins = ct._mac_windows_from_tsv(MAC_WIN_TSV)
    assert wins[0]["app"] == "Safari"
    assert wins[0]["rect"] == [10, 20, 1210, 920]
    assert wins[1]["foreground"] is True and wins[1]["minimized"] is True
    assert ct._mac_windows_from_tsv("garbage line\n1\t2\t3") == []


def test_mac_windows_list(mac, monkeypatch):
    runner = FakeRunner(stdout=MAC_WIN_TSV)
    monkeypatch.setattr(ct, "_run_cmd", runner)
    data = json.loads(ct._computer_windows({"action": "list"}))
    assert data["count"] == 2
    assert data["windows"][0]["app"] == "Code"    # 前台优先
    assert runner.calls[0][0][0] == "osascript"


def test_mac_windows_focus_and_close(mac, pag, monkeypatch):
    runner = FakeRunner(stdout=MAC_WIN_TSV)
    monkeypatch.setattr(ct, "_run_cmd", runner)
    data = json.loads(ct._computer_windows({"action": "focus", "query": "Safari"}))
    assert data["success"] is True
    assert runner.calls[-1][0] == ("open", "-a", "Safari")

    data = json.loads(ct._computer_windows({"action": "close", "query": "main.py"}))
    assert data["success"] is True
    assert runner.calls[-1][0] == ("open", "-a", "Code")   # 先激活
    assert ("hotkey", ("command", "w")) in pag.calls       # 再 cmd+w


def test_mac_windows_no_match_and_permission_error(mac, monkeypatch):
    runner = FakeRunner(stdout=MAC_WIN_TSV)
    monkeypatch.setattr(ct, "_run_cmd", runner)
    assert "error" in json.loads(ct._computer_windows({"action": "focus",
                                                       "query": "不存在"}))
    monkeypatch.setattr(ct, "_run_cmd",
                        FakeRunner(returncode=1,
                                   stderr="Not authorized to send Apple events"))
    data = json.loads(ct._computer_windows({"action": "list"}))
    assert "辅助功能" in data["error"]


# ---- app -----------------------------------------------------------------

def test_app_close_matches_name(monkeypatch):
    procs = [FakeProc(101, "notepad.exe"), FakeProc(202, "Notepad.EXE"),
             FakeProc(303, "explorer.exe")]
    fake = FakePsutil(procs)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    data = json.loads(ct._computer_app({"action": "close", "name": "notepad"}))
    assert data["count"] == 2 and data["pids"] == [101, 202]
    assert procs[0].terminated and procs[1].terminated and not procs[2].terminated


def test_app_close_requires_name():
    assert "error" in json.loads(ct._computer_app({"action": "close"}))


def test_mac_open_cmd_builder():
    assert ct._mac_open_cmd("https://example.com", None) == ["open", "https://example.com"]
    assert ct._mac_open_cmd("Safari", None) == ["open", "-a", "Safari"]
    assert ct._mac_open_cmd("Terminal", ["echo", "hi"]) == \
        ["open", "-a", "Terminal", "--args", "echo", "hi"]
    assert ct._mac_open_cmd("echo hi", None) == ["open", "-a", "echo hi"]


def test_mac_app_open_uses_open(mac, monkeypatch, tmp_path):
    runner = FakeRunner()
    monkeypatch.setattr(ct, "_run_cmd", runner)
    json.loads(ct._computer_app({"action": "open", "target": "Safari"}))
    assert runner.calls[-1][0] == ("open", "-a", "Safari")
    json.loads(ct._computer_app({"action": "open", "target": str(tmp_path)}))
    assert runner.calls[-1][0] == ("open", str(tmp_path))


# ---- clipboard -----------------------------------------------------------

@requires_win
def test_clipboard_roundtrip(clip):
    json.loads(ct._computer_clipboard({"action": "set", "text": "abc中文123"}))
    data = json.loads(ct._computer_clipboard({"action": "get"}))
    assert data["text"] == "abc中文123" and data["truncated"] is False
    json.loads(ct._computer_clipboard({"action": "clear"}))
    assert json.loads(ct._computer_clipboard({"action": "get"}))["text"] == ""


@requires_win
def test_clipboard_get_truncates(clip):
    clip.data = "x" * 15000
    data = json.loads(ct._computer_clipboard({"action": "get"}))
    assert data["truncated"] is True and len(data["text"]) == ct._CLIPBOARD_GET_LIMIT


def test_mac_clipboard_uses_pbpaste_pbcopy(mac, monkeypatch):
    runner = FakeRunner(stdout="abc")
    monkeypatch.setattr(ct, "_run_cmd", runner)
    json.loads(ct._computer_clipboard({"action": "set", "text": "你好"}))
    assert runner.calls[-1] == (("pbcopy",), "你好".encode("utf-8"))
    data = json.loads(ct._computer_clipboard({"action": "get"}))
    assert runner.calls[-1][0] == ("pbpaste",)
    assert data["text"] == "abc"


# ---- UIA (Windows: pywinauto) --------------------------------------------

@requires_win
def test_uia_list_filters_and_computes_centers(monkeypatch, wgui):
    monkeypatch.setattr(ct, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    ctrls = [
        FakeCtrl("登录", "Button", (100, 200, 220, 240)),
        FakeCtrl("", "Edit", (0, 0, 50, 50)),            # 无名 → 过滤
        FakeCtrl("说明", "Static", (0, 300, 100, 320)),   # 非交互 → 过滤
        FakeCtrl("取消", "Button", (300, 200, 380, 240), enabled=False),
        FakeCtrl("离屏", "Button", (-32000, -32000, -31900, -31950)),  # 最小化垃圾 rect
    ]
    patch_uia(monkeypatch, ctrls)
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "list"}))
    assert data["count"] == 2
    by_name = {e["name"]: e for e in data["elements"]}
    assert by_name["登录"]["cx"] == 160 and by_name["登录"]["cy"] == 220
    assert by_name["取消"]["enabled"] is False
    assert "enabled" not in by_name["登录"]  # True 省略——压 include_all 体积


@requires_win
def test_uia_list_rescales_provider_grid(monkeypatch, wgui):
    # 2026-09-04 事故现场：150% 缩放，provider 报 2880 物理网格，本进程在
    # 1920 网格。换算后坐标才对，且屏外过滤必须按换算后的坐标判。
    monkeypatch.setattr(ct, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    wgui.windows[1]["rect"] = (0, 0, 1920, 1048)
    ctrls = [
        FakeCtrl("新对话", "Button", (84, 147, 329, 192)),
        FakeCtrl("折叠侧栏", "Button", (8, 1488, 64, 1542)),
    ]
    patch_uia(monkeypatch, ctrls, win_rect=(0, 35, 2880, 1583))
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "list"}))
    by_name = {e["name"]: e for e in data["elements"]}
    assert by_name["新对话"]["rect"] == [56, 98, 219, 128]
    assert (by_name["新对话"]["cx"], by_name["新对话"]["cy"]) == (137, 113)
    assert "折叠侧栏" in by_name  # 未换算(y=992..1028 前=1488..)会被离屏过滤器误杀


@requires_win
def test_uia_rejects_minimized_window(monkeypatch, wgui):
    wgui.windows[6] = {"title": "MinWin", "rect": (0, 0, 100, 100),
                       "iconic": True}
    patch_uia(monkeypatch, [])
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "list", "query": "MinWin"}))
    assert "minimized" in data["error"]


@requires_win
def test_uia_click_by_name(monkeypatch, wgui, pag):
    ok_btn = FakeCtrl("确定", "Button", (10, 10, 110, 50))
    cancel = FakeCtrl("取消", "Button", (10, 60, 110, 100))
    patch_uia(monkeypatch, [ok_btn, cancel])
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "click", "name": "取消"}))
    assert data["success"] is True
    assert pag.calls[-1] == ("click", 60, 80, 1, "left")
    resp = json.loads(ct._computer_uia({"action": "click", "name": "不存在"}))
    assert "no element named" in resp["error"]


@requires_win
def test_uia_click_foregrounds_covered_window(monkeypatch, wgui, pag):
    # 真点击落在屏幕坐标的最顶层窗口上——目标被遮挡时必须先带到前台
    btn = FakeCtrl("确定", "Button", (10, 10, 110, 50))
    patch_uia(monkeypatch, [btn])
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    wgui.fg = 5  # 前台是别的窗口（Explorer），目标 hwnd=1 被遮挡
    json.loads(ct._computer_uia({"action": "click", "name": "确定",
                                 "query": "记事本"}))
    assert wgui.foregrounded == [1]
    assert pag.calls[-1] == ("click", 60, 30, 1, "left")


@requires_win
def test_uia_click_index_disambiguates(monkeypatch, wgui, pag):
    a = FakeCtrl("列表项", "ListItem", (0, 0, 10, 10))
    b = FakeCtrl("列表项", "ListItem", (0, 20, 10, 30))
    patch_uia(monkeypatch, [a, b])
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    json.loads(ct._computer_uia({"action": "click", "name": "列表项", "index": 2}))
    assert pag.calls[-1] == ("click", 5, 25, 1, "left")


@requires_win
def test_uia_click_rescales_provider_grid(monkeypatch, wgui, pag):
    # 2026-09-04 事故：150% 缩放下 UIA provider 报物理像素（2880 网格），
    # 本进程点击/截图在 1920 网格——未换算时每个点击都偏 1.5 倍。
    # win32 窗口 rect（0,0,800,600）在我们网格；dlg 自报 1200 宽 → 比例 2/3。
    wgui.windows[1]["rect"] = (0, 0, 800, 600)
    btn = FakeCtrl("确定", "Button", (300, 300, 600, 600))
    patch_uia(monkeypatch, [btn], win_rect=(0, 0, 1200, 900))
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "click", "name": "确定"}))
    assert data["success"] is True
    assert pag.calls[-1] == ("click", 300, 300, 1, "left")


@requires_win
def test_uia_click_rejects_degenerate_rect(monkeypatch, wgui, pag):
    btn = FakeCtrl("关闭", "Button", (0, 0, 0, 0))
    patch_uia(monkeypatch, [btn])
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "click", "name": "关闭"}))
    assert "no usable rect" in data["error"]
    assert not pag.calls


# ---- input ops: deterministic target foregrounding ------------------------

@requires_win
def test_input_ops_foreground_query_target(monkeypatch, wgui, pag, clip):
    # 传了 query 的输入操作必须先前置目标窗口——不依赖模型记得先 focus
    wgui.fg = 5  # 前台是 Explorer，目标 hwnd=1 被遮挡
    q = {"query": "记事本"}
    assert json.loads(ct._computer_click({"x": 10, "y": 10, **q}))["success"]
    assert json.loads(ct._computer_type({"text": "hi", **q}))["success"]
    assert json.loads(ct._computer_key({"keys": "enter", **q}))["success"]
    assert json.loads(ct._computer_scroll({"direction": "down", **q}))["success"]
    assert wgui.foregrounded == [1, 1, 1, 1]
    assert pag.calls  # 输入确实发生了


@requires_win
def test_input_op_unresolved_target_errors(wgui, pag):
    data = json.loads(ct._computer_click({"x": 10, "y": 10, "query": "不存在"}))
    assert "no window matched" in data["error"]
    assert pag.calls == []


@requires_win
def test_foreground_attach_denied_degrades(wgui, pag, clip):
    # 前台属于提权进程时 AttachThreadInput 直接 error 5 —— 不能让整个工具
    # 抛未捕获异常，降级为普通 SetForegroundWindow 照常完成输入
    wgui.fg = 5
    wgui.attach_fail = True
    data = json.loads(ct._computer_type({"text": "hi", "query": "记事本"}))
    assert data["success"] is True
    assert wgui.foregrounded == [1]
    assert pag.calls


@requires_win
def test_input_op_aborts_when_foreground_locked(wgui, pag, clip):
    # SetForegroundWindow 被前台锁挡下且复核确认没到前台 —— 必须报错并
    # 保持零输入，而不是把键盘敲进当前顶层的窗口
    wgui.fg = 5
    wgui.locked = True
    data = json.loads(ct._computer_type({"text": "hi", "query": "记事本"}))
    assert "could not bring" in data["error"]
    assert pag.calls == []
    assert clip.set_history == []
    data = json.loads(ct._computer_windows({"action": "focus", "hwnd": 1}))
    assert "could not bring" in data["error"]


@requires_win
def test_type_retries_when_clipboard_busy(pag, clip):
    # 剪贴板被别的进程短暂占用（OpenClipboard error 5）→ 重试窗口内恢复
    clip.busy_first = 2
    clip.data = "user-data"
    data = json.loads(ct._computer_type({"text": "hi", "no_window": True}))
    assert data["success"] is True
    assert clip.set_history == ["hi", "user-data"]


def test_mac_input_op_foregrounds_target(mac, pag, monkeypatch):
    runner = FakeRunner(stdout=MAC_WIN_TSV)
    monkeypatch.setattr(ct, "_run_cmd", runner)
    monkeypatch.setattr(ct, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    data = json.loads(ct._computer_click({"x": 100, "y": 200, "query": "Safari"}))
    assert data["success"] is True
    assert ("open", "-a", "Safari") in [c[0] for c in runner.calls]
    assert pag.calls  # 激活之后才点击


# ---- UIA (macOS: System Events osascript) --------------------------------

MAC_UIA_TSV = ("AXButton\t登录\t100,200,220,240\ttrue\n"
               "AXGroup\t\t0,0,50,50\ttrue\n"
               "AXStatic\t说明\t0,300,100,320\ttrue\n"
               "AXButton\t取消\t300,200,380,240\tfalse\n")


def test_mac_uia_tsv_parse():
    els = ct._mac_uia_from_tsv(MAC_UIA_TSV)
    assert [e["name"] for e in els] == ["登录", "取消"]
    assert els[0]["cx"] == 160 and els[0]["cy"] == 220
    assert els[1]["enabled"] is False
    # include_all 保留全部非零面积元素
    assert len(ct._mac_uia_from_tsv(MAC_UIA_TSV, include_all=True)) == 4


def test_mac_uia_list(mac, monkeypatch):
    runner = FakeRunner(stdout=MAC_UIA_TSV)
    monkeypatch.setattr(ct, "_run_cmd", runner)
    data = json.loads(ct._computer_uia({"action": "list"}))
    assert data["count"] == 2
    assert data["elements"][0]["name"] == "登录"
    # osascript 失败 → 授权指引错误
    monkeypatch.setattr(ct, "_run_cmd",
                        FakeRunner(returncode=1,
                                   stderr="Not authorized to send Apple events"))
    data = json.loads(ct._computer_uia({"action": "list"}))
    assert "辅助功能" in data["error"]


def test_mac_uia_click_uses_pyautogui(mac, pag, monkeypatch):
    runner = FakeRunner(stdout=MAC_UIA_TSV)
    monkeypatch.setattr(ct, "_run_cmd", runner)
    monkeypatch.setattr(ct, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    data = json.loads(ct._computer_uia({"action": "click", "name": "登录"}))
    assert data["success"] is True
    assert ("click", 160, 220, 1, "left") in pag.calls
    resp = json.loads(ct._computer_uia({"action": "click", "name": "不存在"}))
    assert "no element named" in resp["error"]


@requires_win
def test_uia_walk_timeout_abandoned_with_error(monkeypatch, wgui):
    # 2026-08-28 实测：爬一个不响应的窗口（刚启动的记事本）时 UIA COM 调用
    # 永久阻塞 → 工具永不返回。预算耗尽后必须报错并放弃，且卡死期间后续
    # 调用快速失败，不再往同一个死窗口叠加第二发。
    calls = {"n": 0}

    class _SlowDlg:
        def descendants(self):
            calls["n"] += 1
            time.sleep(1.5)
            return []

    monkeypatch.setattr(ct, "_uia_window_wrapper", lambda hwnd: _SlowDlg())
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    monkeypatch.setattr(ct, "_UIA_DEFAULT", 0.3)
    monkeypatch.setattr(ct, "_UIA_MIN", 0.1)  # 绕过下限，让测试预算生效

    data = json.loads(ct._computer_uia({"action": "list"}))
    assert "timed out" in data["error"]
    data2 = json.loads(ct._computer_uia({"action": "list"}))
    assert "still stuck" in data2["error"]
    assert calls["n"] == 1


@requires_win
def test_uia_stuck_guard_clears_when_thread_exits(monkeypatch, wgui):
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    monkeypatch.setattr(ct, "_uia_stuck", t)  # 已退出的僵尸 → 放行
    patch_uia(monkeypatch, [])
    monkeypatch.setattr(ct, "comtypes", FakeComtypes())
    data = json.loads(ct._computer_uia({"action": "list"}))
    assert data["count"] == 0


def test_uia_timeout_arg_clamps():
    assert ct._uia_timeout_arg({}) == 30.0
    assert ct._uia_timeout_arg({"timeout": 3}) == 5.0
    assert ct._uia_timeout_arg({"timeout": 999}) == 120.0
    assert ct._uia_timeout_arg({"timeout": "abc"}) == 30.0
    assert ct._uia_timeout_arg({"timeout": 60}) == 60.0
    assert ct._uia_timeout_arg({"timeout": None}) == 30.0


def test_uia_gate(monkeypatch):
    monkeypatch.setattr(ct, "_UIA_OK", False)
    assert ct._check_uia() is False
