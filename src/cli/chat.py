"""
Chat mode — interactive REPL (ANSI Scroll Region) and single-query.
Interactive REPL uses DECSTBM ANSI Scroll Region to pin input line at bottom.
No alternate screen, output persists to terminal native scrollback.
Support real-time steer during LLM generation, Windows VT console optimized.
"""
import atexit
import ctypes
import logging
import os
import re
import shutil
import sys
import threading
import time
from abc import ABC
from dataclasses import dataclass
from typing import Callable, Optional, List, Dict, Any

from core.config import load_config, AGENT_HOME, api_key_missing_message, seed_default_config
from gateway.commands import handle_command, is_stop_intent

class Ansi:
    # Style
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RST = "\033[0m"
    # Color
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RED = "\033[31m"

    # Scroll region / Cursor control
    SAVE_CUR = "\033[s"
    RESTORE_CUR = "\033[u"
    CLEAR_LINE = "\033[2K"
    CLEAR_CURSOR_DOWN = "\033[J"

    @staticmethod
    def move_to(row: int, col: int = 1) -> str:
        return f"\033[{row};{col}H"

    @staticmethod
    def set_scroll_region(bottom: int) -> str:
        return f"\033[1;{bottom}r"

    RESET_SCROLL_REGION = "\033[r"

_OUT_LOCK = threading.RLock()


# Match CSI sequences broadly: SGR (colors/styles) AND cursor/scroll-region/ERASE
# codes (\x1b[H \x1b[2J \x1b[2K \x1b[?25l \x1b[1;36m ...). The old pattern only
# stripped `\x1b[...m`, so Rich's non-SGR codes slipped through and left "0m 2m"
# fragments when only part of a sequence was stripped by some terminal layer.
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')


# ASCII-safe text: prevent "boxed ?" glyphs on limited console fonts.
#
# cmd.exe (and other VT-less consoles) render any Unicode codepoint their font
# lacks as a boxed "?". LLM replies are full of such glyphs: box-drawing under
# Rich tables/rules, arrows (→), math (≥ ≠ ±), bullets (• ✔), emoji (✅ ❌), etc.
# We can't detect glyph coverage, so we ASCII-fy defensively:
#   - known symbol  -> ASCII equivalent (_ASCII_MAP)
#   - ASCII + CJK ideographs / punctuation / fullwidth -> kept verbatim
#     (Chinese Windows fonts cover these)
#   - anything else (rare symbol / emoji) -> dropped, codepoint logged once
# ANSI escape codes are pure ASCII, so they survive _ascii_safe untouched.
_ASCII_MAP = {
    # box drawing — single
    '─': '-', '━': '=', '│': '|', '┃': '|',
    '┌': '+', '┐': '+', '└': '+', '┘': '+', '├': '+', '┤': '+', '┬': '+', '┴': '+', '┼': '+',
    '╭': '+', '╮': '+', '╰': '+', '╯': '+',
    # box drawing — double
    '═': '=', '║': '|', '╒': '+', '╓': '+', '╔': '+', '╕': '+', '╖': '+', '╗': '+',
    '╘': '+', '╙': '+', '╚': '+', '╛': '+', '╜': '+', '╝': '+', '╞': '+', '╟': '+', '╠': '+',
    '╡': '+', '╢': '+', '╣': '+', '╤': '+', '╥': '+', '╦': '+', '╧': '+', '╨': '+', '╩': '+',
    '╪': '+', '╫': '+', '╬': '+',
    # arrows
    '→': '->', '←': '<-', '↑': '^', '↓': 'v', '↔': '<->', '↕': '^v', '↳': '\\', '↦': '|->',
    '⇒': '=>', '⇐': '<=', '⇔': '<=>', '⇑': '^^', '⇓': 'vv', '⇕': '^v',
    '»': '>>', '«': '<<', '›': '>', '‹': '<',
    # bullets / geometric
    '•': '*', '◦': 'o', '·': '.', '▪': '#', '▫': '-', '■': '#', '□': '[]', '●': '*', '○': 'o',
    '◆': '<>', '◇': '<>', '‣': '>',
    # math / set operators
    '≥': '>=', '≤': '<=', '≠': '!=', '≈': '~', '±': '+-', '∓': '-+', '×': 'x', '÷': '/',
    '√': 'v', '∞': 'inf', '∂': 'd', '∆': 'd', 'Δ': 'D', '∑': 'sum', '∏': 'prod', '∫': 'int',
    '°': ' deg', 'µ': 'u', '‰': '%%', '′': "'", '″': '"', '∇': 'del',
    '∈': ' in ', '∉': ' notin ', '∩': '&', '∪': '|', '⊂': 'sub', '⊃': 'sup',
    '⊆': 'sube', '⊇': 'supe', '∅': '{}', '∀': 'forall', '∃': 'exists',
    # check / cross / marks
    '✓': 'v', '✗': 'x', '✔': 'v', '✕': 'x', '✖': 'x', '☑': '[x]', '☒': '[ ]', '⚠': '[!]',
    # stars
    '★': '*', '☆': '.', '✦': '*', '✧': '.',
    # general punctuation
    '…': '...', '—': '--', '–': '-', '“': '"', '”': '"', '‘': "'", '’': "'",
    '§': 'S', '¶': 'P', '†': '+', '‡': '++', '¿': '?', '¡': '!',
    # currency / marks
    '™': '(TM)', '©': '(c)', '®': '(R)', '¢': 'c', '£': 'L', '€': 'EUR', '¥': 'Y', '¤': '$',
    # superscript / subscript digits
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
    '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4', '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9',
    # common emoji in LLM replies (best-effort ASCII)
    '✅': '[v]', '❌': '[x]', '❓': '[?]', '❗': '[!]', '📌': '*', '🎯': '*', '✨': '*', '💡': '*',
    '🔥': '!', '⭐': '*', '🔒': '[lock]', '🔑': '[key]', '✋': '[stop]', '👍': '[ok]', '👎': '[no]',
}

# Ranges the Chinese Windows console font DOES cover — preserve these verbatim.
_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK symbols & punctuation: 。，！？、：；（）【】《》「」『』〈〉…—·～
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3400, 0x4DBF),  # CJK Unified Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs (the bulk of Chinese text)
    (0xA000, 0xA4CF),  # Yi
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),  # CJK Compatibility Forms
    (0xFF00, 0xFFEF),  # Fullwidth / Halfwidth forms (！？，．：；ａｂｃ￥￣ etc.)
)
_DROPPED_SEEN: set = set()


def _ascii_safe(s: str) -> str:
    """ASCII-fy glyphs a limited console font renders as boxed "?".

    Keeps ASCII (incl. ANSI escapes) and CJK text; maps known symbols to ASCII;
    drops other exotic glyphs and logs their codepoints once each (so the log
    is self-documenting — check agent.log for "ascii_safe dropped").
    """
    if not s:
        return s
    out = []
    dropped = []
    for ch in s:
        mapped = _ASCII_MAP.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        cp = ord(ch)
        if cp < 0x80:  # ASCII — includes ANSI escape sequences
            out.append(ch)
            continue
        if any(lo <= cp <= hi for lo, hi in _CJK_RANGES):
            out.append(ch)  # font covers CJK
            continue
        # exotic glyph the font can't show — drop it; record codepoint once
        if cp not in _DROPPED_SEEN:
            _DROPPED_SEEN.add(cp)
            dropped.append(ch)
    if dropped:
        try:
            logging.getLogger(__name__).info(
                "ascii_safe dropped %d new glyph(s): %s",
                len(dropped), " ".join(f"U+{ord(c):04X}" for c in dropped[:20]),
            )
        except Exception:
            pass
    return "".join(out)


def _capability_summary(agent) -> str:
    """One-line capability digest for the banner (tool count + key faces).

    Derived from the agent's actually-visible schemas (check_fn filtered),
    so a missing Playwright or an absent web roster shows up as browser:off
    instead of the user discovering it by the tool silently not existing.
    """
    from tools import registry
    try:
        schemas = registry.get_schemas(toolsets=agent.enabled_toolsets)
        names = {s["function"]["name"] for s in schemas}
    except Exception:
        return ""
    return (" | ".join([
        f"Tools: {len(names)}",
        f"browser:{'on' if any(n.startswith('browser_') for n in names) else 'off'}",
        f"vision:{'on' if 'vision_analyze' in names else 'off'}",
    ]) + " | /help=commands /tools=list")


def _detect_vt() -> bool:
    """Decide whether to emit raw ANSI to stdout or strip it first.

    Returns True only with positive evidence that the active stdout is a real,
    VT-capable console. The console mode VT-bit is necessary but NOT sufficient —
    some terminals report the bit set yet render `\x1b[0m` as literal "0m" text
    (observed: cmd.exe under certain wrappers). So we also require isatty and
    let the user override with env vars.

    Config overrides (cli section of config.yaml, highest priority):
      cli.no_ansi: true   -> always strip (force False) — use if you see 0m/2m garbage
      cli.force_ansi: true -> always emit raw ANSI (force True)
    """
    log = logging.getLogger(__name__)
    isatty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    try:
        from core.config import load_config
        _cli = load_config().get("cli") or {}
    except Exception:
        _cli = {}
    off = str(_cli.get("no_ansi") or "").strip().lower()
    on = str(_cli.get("force_ansi") or "").strip().lower()
    if off in ("1", "true", "yes", "on"):
        log.info("VT detect: forced OFF via cli.no_ansi (isatty=%s)", isatty)
        return False
    if on in ("1", "true", "yes", "on"):
        log.info("VT detect: forced ON via cli.force_ansi (isatty=%s)", isatty)
        return True
    if sys.platform != "win32":
        log.info("VT detect: non-win32 isatty=%s -> %s", isatty, isatty)
        return isatty
    try:
        k = ctypes.windll.kernel32
        k.GetStdHandle.restype = ctypes.c_void_p
        k.GetConsoleMode.restype = ctypes.c_int
        k.GetFileType.restype = ctypes.c_uint32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        m = ctypes.c_uint32()
        ok = bool(k.GetConsoleMode(h, ctypes.byref(m)))
        vt_bit = bool(m.value & 0x0004)
        capable = bool(ok and vt_bit and isatty)
        # NOTE: we intentionally do NOT call SetConsoleMode to force-enable VT.
        # Doing so destabilizes prompt_toolkit on cmd.exe's conhost — it raises
        # "[WinError 233] 管道的另一端上无任何进程" and the REPL falls back / the
        # console can die (observed: cmd window closes). VT must already be on
        # (set by the host, e.g. IDEA's PTY) or we strip ANSI. Read-only here.
        log.info("VT detect: isatty=%s handle=%s mode=%s vt_bit=%s -> emit_ansi=%s",
                 isatty, h, hex(m.value), vt_bit, capable)
        # Context (filetype / term env / parent) logged once per startup for
        # diagnosing any future "why no color / why garbage" regressions.
        try:
            ftype = k.GetFileType(h)
            ftype_name = {1: "disk", 2: "char(console)", 3: "pipe"}.get(ftype, str(ftype))
        except Exception as fe:
            ftype_name = f"err:{fe!r}"
        term_env = ",".join(
            f"{ek}={os.environ.get(ek, '')}"
            for ek in ("TERM", "MSYSTEM", "ConEmuPID", "WT_SESSION", "TERM_PROGRAM")
            if os.environ.get(ek)
        ) or "(none)"
        try:
            import psutil
            parent = psutil.Process(os.getpid()).parent().name()
        except Exception:
            parent = f"ppid={os.getppid()}"
        log.info("VT context: filetype=%s encoding=%s term_env=[%s] parent=%s",
                 ftype_name, getattr(sys.stdout, "encoding", None), term_env, parent)
        return capable
    except Exception as e:
        log.info("VT detect: exception=%r isatty=%s -> emit_ansi=False", e, isatty)
        return False


def cprint(text: str = "", color: str = "", end: str = "\n", emit_func: Optional[Callable[[str], None]] = None):
    """Thread-safe colored print. Route to scroll region emitter or stdout.

    Output is always run through _ascii_safe so box/symbol glyphs never appear
    as boxed "?" — regardless of whether ANSI color is being stripped or kept.
    """
    buf = f"{color}{text}{Ansi.RST}"
    if end == "\n":
        buf += "\n"
    buf = _ascii_safe(buf)

    if emit_func is not None:
        with _OUT_LOCK:
            emit_func(buf)
    else:
        with _OUT_LOCK:
            sys.stdout.write(buf)
            sys.stdout.flush()

_HIST_PATH = AGENT_HOME / "cli_history"


def load_input_history() -> List[str]:
    if not _HIST_PATH.exists():
        return []
    try:
        lines = [line.rstrip("\n") for line in _HIST_PATH.read_text(encoding="utf-8").splitlines()]
        return [item for item in lines if item.strip()]
    except Exception:
        return []


def append_input_history(text: str, history: List[str]):
    if not text:
        return
    if history and history[-1] == text:
        return
    history.append(text)
    try:
        with open(_HIST_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        logging.getLogger(__name__).debug("input history append failed", exc_info=True)

def render_markdown_str(text: str) -> str:
    """Render Markdown to an ANSI string via Rich.

    Box/symbol glyph safety is handled centrally by _ascii_safe (called from
    cprint on the result), so we just render with safe_box=True and return.
    CJK text and ANSI styling are preserved.
    """
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        from io import StringIO

        width = shutil.get_terminal_size().columns
        sio = StringIO()
        # safe_box=True -> Rich prefers ASCII box chars when it can't verify
        # Unicode support; _ascii_safe then cleans up anything that slips through.
        cap = Console(file=sio, force_terminal=True, width=width, safe_box=True)
        cap.print(Markdown(text))
        return sio.getvalue()
    except Exception:
        return text

def pick_session(agent, default_name: str) -> str:
    try:
        rows = [r for r in agent.db.list_sessions(limit=30, platform="cli") if r.get("chat_id")]
    except Exception:
        rows = []
    if not rows:
        return default_name

    cprint("Recent CLI sessions - pick number to resume (Enter=cancel):", Ansi.CYAN + Ansi.BOLD)
    for idx, rec in enumerate(rows, 1):
        title = rec.get("title") or "(未命名)"
        ts = (rec.get("updated_at", ""))[:16].replace("T", " ")
        chat_id = rec.get("chat_id", "")
        msg_cnt = rec.get("msg_count", 0)
        cprint(f"  {idx:2d}. {chat_id} — {title}  {Ansi.DIM}({ts}, {msg_cnt} msgs){Ansi.RST}")

    try:
        choice = input(f"{Ansi.BOLD}{Ansi.CYAN}resume> {Ansi.RST}").strip()
    except (KeyboardInterrupt, EOFError):
        return default_name
    if not choice:
        return default_name
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1]["chat_id"]
    for rec in rows:
        if rec.get("chat_id") == choice:
            return choice
    return default_name


def cli_resume(agent, cmd_ctx: Dict[str, Any], arg: str):
    try:
        rows = [r for r in agent.db.list_sessions(limit=30, platform="cli") if r.get("chat_id")]
    except Exception:
        rows = []

    def resolve_choice(sel: str):
        if sel.isdigit() and 1 <= int(sel) <= len(rows):
            return rows[int(sel) - 1].get("chat_id")
        return next((r["chat_id"] for r in rows if r.get("chat_id") == sel), None)

    arg = arg.strip()
    if not arg:
        if not rows:
            cprint("No CLI sessions to resume.", Ansi.DIM)
            return
        lines = ["最近的 CLI 会话 — 用 /resume <序号或名字> 切换:"]
        for i, r in enumerate(rows, 1):
            cid = r.get("chat_id")
            title = r.get("title") or "(未命名)"
            lines.append(f"  {i:2d}. {cid} — {title}")
        cprint("\n".join(lines))
        return

    target_chat_id = resolve_choice(arg)
    if not target_chat_id:
        cprint(f"Not found: '{arg}'. Use /resume <number/name>", Ansi.DIM)
        return

    from core.session import SessionSource
    new_source = SessionSource(platform="cli", chat_id=target_chat_id, chat_type="dm")
    new_key = agent.db.build_key(new_source)
    cmd_ctx["cli_source"] = new_source
    cmd_ctx["session_key"] = new_key
    entry = agent.db.get_entry(new_key)
    title = agent.db.get_session_title(entry.session_id) if entry else None
    cprint(f"[OK] switched to [{target_chat_id}] {title or '(unnamed)'}", Ansi.GREEN)


def handle_during_turn(text: str, agent, cmd_ctx: Dict[str, Any]):
    text = text.strip()
    if not text:
        return
    head = ""
    if text.startswith("/"):
        head = text.split(maxsplit=1)[0].lower()

    if head in ("/stop", "/cancel") or (not head and is_stop_intent(text)):
        agent.interrupt()
        cprint("[STOP] interrupt requested", Ansi.YELLOW)
    elif head == "/resume":
        cli_resume(agent, cmd_ctx, text[len("/resume"):])
    elif head:
        resp = handle_command(text, cmd_ctx)
        if resp and resp not in ("__CLEAR__", "__QUIT__"):
            cprint(resp)
    else:
        from tools._approvals import try_resolve_steer
        if try_resolve_steer(agent, text):
            # 批复结果由 approval_result 回调打印（已批准/未批准）
            cprint("[approval] 批复已送达", Ansi.DIM)
            return
        agent.steer(text)
        preview = text[:60]
        cprint(f"[steer] received: {preview}", Ansi.DIM)

def run_turns_threaded(
        agent,
        cmd_ctx: Dict[str, Any],
        first_msg: str,
        set_running: Callable[[bool], None],
        on_content: Optional[Callable[[str, str], None]] = None,
        on_turn_done: Optional[Callable[[Optional[str]], None]] = None,
        emit_func: Optional[Callable[[str], None]] = None
):
    src = cmd_ctx["cli_source"]

    def worker():
        set_running(True)
        message_queue = [first_msg]
        final_result: Optional[str] = None

        def flush_buffer(sbuf: dict):
            if not sbuf["text"]:
                return
            if on_content and sbuf["kind"] != "reasoning":
                on_content(sbuf["text"], "content")
            else:
                # Reasoning prints live even in buffered (markdown-render) mode:
                # it precedes the tool calls it produced, so holding it for the
                # end-of-turn render would invert the visible order (tools
                # first, thinking after). Plain text, no Rich rendering needed.
                color = Ansi.DIM if sbuf["kind"] == "reasoning" else Ansi.GREEN
                cprint(sbuf["text"], color, emit_func=emit_func)
            sbuf["text"] = ""

        def stream_delta(text: Optional[str], kind="content", by=None):
            if text is None:
                flush_buffer(sbuf)
                return
            if sbuf["kind"] is not None and sbuf["kind"] != kind:
                flush_buffer(sbuf)
            sbuf["kind"] = kind
            if on_content:
                sbuf["text"] += text
            else:
                parts = text.split("\n")
                for idx, part in enumerate(parts):
                    if idx > 0:
                        flush_buffer(sbuf)
                    sbuf["text"] += part

        def tool_start(name: str, args_summary: str, by=None):
            flush_buffer(sbuf)
            short_args = args_summary[:80] + ("..." if len(args_summary) > 80 else "")
            tag = f"[{by}] " if by else ""
            cprint(f"  * {tag}{name}({short_args})", Ansi.DIM, emit_func=emit_func)

        def tool_finish(name: str, args_summary: str, elapsed: float, by=None):
            flush_buffer(sbuf)
            tag = f"[{by}] " if by else ""
            cprint(f"  OK {tag}{name} ({elapsed:.1f}s)", Ansi.DIM, emit_func=emit_func)

        # 审批回调在工具线程触发（dispatch 内）；提示走 cprint 直出。
        # 批复本身经主线程 REPL 的 handle_during_turn → try_resolve_steer 进来。
        def approval_request(info: dict):
            flush_buffer(sbuf)
            cprint("⚠️ 危险操作待确认", Ansi.YELLOW + Ansi.BOLD, emit_func=emit_func)
            cprint(f"  {info.get('summary', '')}", Ansi.YELLOW, emit_func=emit_func)
            cprint("  输入 y 批准 / n 拒绝 / a 本会话不再询问", Ansi.DIM, emit_func=emit_func)

        def approval_result(info: dict, approved: bool, reason: str):
            verdict = "已批准" if approved else f"未批准（{reason}）"
            cprint(f"  [approval] {verdict}", Ansi.DIM, emit_func=emit_func)

        while message_queue:
            sbuf = {"text": "", "kind": None}
            current_msg = message_queue.pop(0)
            try:
                final_result = agent.chat(
                    source=src,
                    user_message=current_msg,
                    stream_delta_callback=stream_delta,
                    tool_call_start_callback=tool_start,
                    tool_call_callback=tool_finish,
                    approval_request_callback=approval_request,
                    approval_result_callback=approval_result,
                )
            except Exception as e:
                cprint(f"Error: {e}", Ansi.RED, emit_func=emit_func)
                final_result = None
            flush_buffer(sbuf)

            if on_turn_done:
                on_turn_done(final_result)

            # Non-completed exits carry a closing message in final_result that
            # wasn't part of the streamed deltas — surface it so the user sees
            # the hand-off rather than the turn ending silently.
            exit_reason = getattr(agent, "_last_exit_reason", "completed")
            if exit_reason == "max_iterations" and final_result:
                cprint(final_result, Ansi.YELLOW, emit_func=emit_func)
            elif exit_reason in ("api_timeout", "api_error") and final_result:
                cprint(final_result, Ansi.RED, emit_func=emit_func)

            if exit_reason == "interrupted":
                leftover_steer = agent._drain_steer()
                if leftover_steer:
                    cprint(f"(dropped {len(leftover_steer)} steer: interrupted)", Ansi.DIM, emit_func=emit_func)
                break

            leftover_steer = agent._drain_steer()
            for steer_text in leftover_steer:
                cprint(f"\n> {steer_text}", Ansi.CYAN, emit_func=emit_func)
                cprint("... processing", Ansi.DIM, emit_func=emit_func)
                message_queue.append(steer_text)

            usage = getattr(agent, "_turn_usage", {})
            if usage.get("total"):
                token_line = (
                    f"  {Ansi.DIM}{usage['prompt']}->{usage['completion']} tokens, {usage['calls']} calls{Ansi.RST}"
                )
                cprint(token_line, Ansi.DIM, emit_func=emit_func)

        if getattr(agent, "_last_exit_reason", None) == "interrupted":
            cprint("[interrupted]", Ansi.YELLOW, emit_func=emit_func)
        elif not final_result:
            cprint("(empty response)", Ansi.DIM, emit_func=emit_func)
        set_running(False)

    threading.Thread(target=worker, daemon=True).start()

# Scroll Region REPL State Class (封装所有状态，消灭全局游离变量)
@dataclass
class ScrollRegionState:
    input_buf: List[str]
    history: List[str]
    hist_idx: int
    running: List[bool]
    last_ctrl_c: float
    content_buf: List[str]
    term_rows: int
    term_cols: int
    model_name: str
    emit_func: Optional[Callable[[str], None]] = None


def run_scroll_region(agent, cmd_ctx: Dict[str, Any], version: str, session_key: str):
    import msvcrt
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.SetConsoleMode.restype = ctypes.c_int
    kernel32.GetConsoleMode.restype = ctypes.c_int
    STD_OUTPUT_HANDLE = -11
    out_handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(out_handle, ctypes.byref(mode)):
        raise OSError("GetConsoleMode failed - not a real console")

    ENABLE_VT = 0x0004
    ENABLE_PROCESSED = 0x0001
    ENABLE_WRAP = 0x0002
    original_mode = mode.value
    new_mode = original_mode | ENABLE_VT | ENABLE_PROCESSED | ENABLE_WRAP
    if not kernel32.SetConsoleMode(out_handle, new_mode):
        raise OSError("SetConsoleMode failed - cannot enable VT")

    def restore_console():
        try:
            sys.stdout.write(Ansi.RESET_SCROLL_REGION)
            sys.stdout.flush()
            kernel32.SetConsoleMode(out_handle, original_mode)
        except Exception:
            pass
    atexit.register(restore_console)

    verify = ctypes.c_uint32()
    kernel32.GetConsoleMode(out_handle, ctypes.byref(verify))
    if not (verify.value & ENABLE_VT):
        raise OSError("VT processing verified OFF after SetConsoleMode")

    term_size = shutil.get_terminal_size()
    scroll_bottom_row = term_size.lines - 1
    input_line_row = term_size.lines

    # State init
    state = ScrollRegionState(
        input_buf=[],
        history=load_input_history(),
        hist_idx=len(load_input_history()),
        running=[False],
        last_ctrl_c=0.0,
        content_buf=[""],
        term_rows=term_size.lines,
        term_cols=term_size.columns,
        model_name=agent._effective_model(session_key)
    )

    def emit_to_scroll(s: str):
        with _OUT_LOCK:
            sys.stdout.write(Ansi.move_to(scroll_bottom_row))
            sys.stdout.write(s)
            sys.stdout.flush()
    state.emit_func = emit_to_scroll

    def redraw_input():
        with _OUT_LOCK:
            sys.stdout.write(Ansi.move_to(input_line_row))
            sys.stdout.write(Ansi.CLEAR_LINE)
            text = "".join(state.input_buf)
            if not state.running[0]:
                prompt = f"{Ansi.BOLD}{Ansi.CYAN}xihe ({state.model_name}) > {Ansi.RST}{text}"
            else:
                prompt = f"{Ansi.DIM}... {Ansi.RST}{text}"
            sys.stdout.write(prompt)
            sys.stdout.flush()

    def submit_input() -> str:
        nonlocal scroll_bottom_row, input_line_row
        text = "".join(state.input_buf).strip()
        state.input_buf.clear()
        with _OUT_LOCK:
            sys.stdout.write(Ansi.move_to(scroll_bottom_row))
            prefix = "> " if not state.running[0] else "...> "
            sys.stdout.write(f"{Ansi.CYAN}{prefix}{text}{Ansi.RST}\n")
            sys.stdout.flush()
        redraw_input()
        return text

    # Init scroll region
    sys.stdout.write(Ansi.set_scroll_region(scroll_bottom_row))
    sys.stdout.flush()

    # Print banner
    def print_banner():
        banner = f"""{Ansi.BOLD}{Ansi.CYAN}X   X IIIII H   H EEEEE
 X X    I   HHHHH E
  X     I   H   H EEEE
 X X    I   H   H E
X   X IIIII H   H EEEEE
{Ansi.RST}{Ansi.DIM}  Xihe Agent v{version} — Interactive CLI{Ansi.RST}
  Up/Dn=history; type during turn=steer; ESC/Ctrl+C=interrupt; /quit=exit."""
        cprint(banner, emit_func=state.emit_func)
        cprint(f"Model: {state.model_name}", Ansi.DIM, emit_func=state.emit_func)
        cprint(_capability_summary(agent), Ansi.DIM, emit_func=state.emit_func)
    print_banner()
    redraw_input()

    last_term_size = term_size
    prev_running_flag = False

    try:
        while True:
            cur_size = shutil.get_terminal_size()
            if cur_size.lines != last_term_size.lines or cur_size.columns != last_term_size.columns:
                last_term_size = cur_size
                sys.stdout.write(Ansi.RESET_SCROLL_REGION)
                sys.stdout.flush()
                scroll_bottom_row = cur_size.lines - 1
                input_line_row = cur_size.lines
                sys.stdout.write(Ansi.set_scroll_region(scroll_bottom_row))
                sys.stdout.flush()
                redraw_input()

            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                key_name: Optional[str] = None

                # Extended keys 0x00 / 0xE0 prefix
                if ch in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        ext = msvcrt.getwch()
                        mapping = {"H": "up", "P": "down", "K": "left", "M": "right",
                                   "G": "home", "O": "end", "S": "delete"}
                        key_name = mapping.get(ext)
                elif ch == "\x1b":
                    # ESC sequence
                    if msvcrt.kbhit() and msvcrt.getwch() == "[":
                        a = msvcrt.getwch()
                        mapping = {"A": "up", "B": "down", "C": "right", "D": "left",
                                   "H": "home", "F": "end"}
                        key_name = mapping.get(a)
                        if a == "3" and msvcrt.kbhit():
                            msvcrt.getwch()
                            key_name = "delete"
                    else:
                        key_name = "esc"

                if key_name:
                    if key_name == "up" and state.history and state.hist_idx > 0:
                        state.hist_idx -= 1
                        state.input_buf.clear()
                        state.input_buf.extend(list(state.history[state.hist_idx]))
                        redraw_input()
                    elif key_name == "down":
                        state.hist_idx += 1
                        state.input_buf.clear()
                        if state.hist_idx < len(state.history):
                            state.input_buf.extend(list(state.history[state.hist_idx]))
                        redraw_input()
                    elif key_name == "esc" and state.running[0]:
                        agent.interrupt()
                    continue

                # Enter submit
                if ch == "\r":
                    input_text = submit_input()
                    if not input_text:
                        continue
                    append_input_history(input_text, state.history)
                    state.hist_idx = len(state.history)

                    if state.running[0]:
                        handle_during_turn(input_text, agent, cmd_ctx)
                    elif input_text.startswith("/"):
                        head = input_text.split(maxsplit=1)[0].lower()
                        if head == "/resume":
                            cli_resume(agent, cmd_ctx, input_text[len("/resume"):])
                        else:
                            resp = handle_command(input_text, cmd_ctx)
                            if resp == "__QUIT__":
                                cprint("Bye!", Ansi.DIM)
                                return
                            elif resp == "__CLEAR__":
                                os.system("cls" if sys.platform == "win32" else "clear")
                                sys.stdout.write(Ansi.set_scroll_region(scroll_bottom_row))
                                sys.stdout.flush()
                                redraw_input()
                            elif resp:
                                cprint(resp, emit_func=state.emit_func)
                    else:
                        cprint("... processing", Ansi.DIM, emit_func=state.emit_func)

                        def buffer_content(t: str, kind: str):
                            state.content_buf[0] += t

                        def render_result(_):
                            if state.content_buf[0].strip():
                                rendered = render_markdown_str(state.content_buf[0])
                                cprint(rendered, emit_func=state.emit_func)
                            state.content_buf[0] = ""

                        run_turns_threaded(
                            agent, cmd_ctx, input_text,
                            lambda b: state.running.__setitem__(0, b),
                            on_content=buffer_content,
                            on_turn_done=render_result,
                            emit_func=state.emit_func
                        )

                elif ch == "\x08":
                    if state.input_buf:
                        state.input_buf.pop()
                        redraw_input()

                elif ch == "\x03":
                    now = time.time()
                    if now - state.last_ctrl_c < 1.0:
                        if state.running[0]:
                            agent.interrupt()
                            cprint("[STOP]", Ansi.YELLOW, emit_func=state.emit_func)
                        else:
                            cprint("Bye!", Ansi.DIM)
                            return
                    else:
                        state.last_ctrl_c = now
                        state.input_buf.clear()
                        cprint("(Ctrl+C again to quit)", Ansi.DIM, emit_func=state.emit_func)

                elif ch >= " ":
                    state.input_buf.append(ch)
                    redraw_input()

            # Running state change refresh
            if state.running[0] != prev_running_flag:
                prev_running_flag = state.running[0]
                state.input_buf.clear()
                redraw_input()
            if not state.running[0]:
                state.hist_idx = len(state.history)
            time.sleep(0.02)
    except (KeyboardInterrupt, EOFError):
        if state.running[0]:
            agent.interrupt()
    finally:
        # atexit 还会兜底执行一次，这里主动清理
        sys.stdout.write(Ansi.RESET_SCROLL_REGION)
        sys.stdout.write(Ansi.move_to(input_line_row))
        sys.stdout.write(Ansi.CLEAR_LINE + "\n")
        sys.stdout.flush()
        try:
            from tools.browser_tool import shutdown_browser
            shutdown_browser()
        except Exception:
            pass

def run_hybrid(agent, cmd_ctx: Dict[str, Any], version: str, session_key: str):
    import msvcrt
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.formatted_text import HTML

    try:
        prompt_session = PromptSession(
            history=FileHistory(str(_HIST_PATH)),
            auto_suggest=AutoSuggestFromHistory(),
        )
    except Exception as e:
        raise ImportError(f"prompt toolkit init failed: {e}")

    model_name = agent._effective_model(session_key)
    running = [False]
    prompt_html = HTML(f'<ansicyan><b>xihe ({model_name}) > </b></ansicyan>')

    # Detect if ANSI works; if not, strip color codes to avoid "0m 2m" garbage.
    # Full diagnostic logging lives inside _detect_vt(). If you still see stray
    # "0m"/"2m" fragments, set cli.no_ansi: true in config.yaml to force plain.
    _ansi_ok = _detect_vt()
    logging.getLogger(__name__).info("REPL mode=hybrid emit_ansi=%s", _ansi_ok)
    def _plain_emit(s):
        with _OUT_LOCK:
            # Strip ANSI codes, then ASCII-fy any glyph the font can't render.
            cleaned = _ascii_safe(_ANSI_RE.sub('', s))
            sys.stdout.write(cleaned)
            sys.stdout.flush()
    _emit = None if _ansi_ok else _plain_emit

    def print_banner():
        banner = f"""{Ansi.BOLD}{Ansi.CYAN}X   X IIIII H   H EEEEE
 X X    I   HHHHH E
  X     I   H   H EEEE
 X X    I   H   H E
X   X IIIII H   H EEEEE
{Ansi.RST}{Ansi.DIM}  Xihe Agent v{version} — Hybrid CLI{Ansi.RST}
 Up/Dn=history; type during turn=steer; ESC/Ctrl+C=interrupt; /quit=exit"""
        cprint(banner, emit_func=_emit)
        cprint(f"Model: {model_name}", Ansi.DIM, emit_func=_emit)
        cprint(_capability_summary(agent), Ansi.DIM, emit_func=_emit)
    print_banner()

    try:
        while True:
            try:
                line = prompt_session.prompt(prompt_html)
            except (EOFError, KeyboardInterrupt):
                cprint("\nBye!", Ansi.DIM, emit_func=_emit)
                break
            text = line.strip()
            if not text:
                continue

            if text.startswith("/"):
                head = text.split(maxsplit=1)[0].lower()
                if head == "/resume":
                    cli_resume(agent, cmd_ctx, text[len("/resume"):])
                else:
                    resp = handle_command(text, cmd_ctx)
                    if resp == "__QUIT__":
                        cprint("Bye!", Ansi.DIM, emit_func=_emit); break
                    elif resp == "__CLEAR__":
                        os.system("cls" if sys.platform == "win32" else "clear")
                    elif resp:
                        cprint(resp, emit_func=_emit)
                continue

            cprint("... processing", Ansi.DIM, emit_func=_emit)
            content_buf = [""]

            def buffer_content(t, kind):
                content_buf[0] += t

            def render_result(_):
                if content_buf[0].strip():
                    cprint(render_markdown_str(content_buf[0]), emit_func=_emit)
                content_buf[0] = ""

            run_turns_threaded(
                agent, cmd_ctx, text,
                lambda b: running.__setitem__(0, b),
                on_content=buffer_content,
                on_turn_done=render_result,
                emit_func=_emit
            )

            steer_buf = []
            while running[0]:
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\r":
                        cprint("\n", emit_func=_emit)
                        steer_text = "".join(steer_buf).strip()
                        steer_buf.clear()
                        if steer_text:
                            handle_during_turn(steer_text, agent, cmd_ctx)
                    elif ch == "\x08":
                        if steer_buf:
                            steer_buf.pop()
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                    elif ch in ("\x03", "\x1b"):
                        agent.interrupt()
                        cprint("\n[STOP] interrupting...", Ansi.YELLOW, emit_func=_emit)
                    elif ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                    elif ch >= " ":
                        steer_buf.append(ch)
                        sys.stdout.write(ch)
                        sys.stdout.flush()
                time.sleep(0.02)
            cprint(emit_func=_emit)
    finally:
        try:
            from tools.browser_tool import shutdown_browser
            shutdown_browser()
        except Exception:
            pass

def run_fallback(agent, cmd_ctx: Dict[str, Any], version: str, session_key: str):
    model_name = agent._effective_model(session_key)
    running = [False]

    def print_banner():
        banner = f"""{Ansi.BOLD}{Ansi.CYAN}Xihe Agent v{version} (Fallback Mode){Ansi.RST}
Model: {model_name}
 Up/Dn=history; /quit=exit; Ctrl+C=interrupt. NOTE: no steer in fallback mode!"""
        cprint(banner)
        cprint(_capability_summary(agent), Ansi.DIM)
    print_banner()

    try:
        while True:
            try:
                line = input(f"{Ansi.BOLD}{Ansi.CYAN}xihe> {Ansi.RST}")
            except (EOFError, KeyboardInterrupt):
                cprint("\nBye!", Ansi.DIM)
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                head = text.split(maxsplit=1)[0].lower()
                if head == "/resume":
                    cli_resume(agent, cmd_ctx, text[len("/resume"):])
                else:
                    resp = handle_command(text, cmd_ctx)
                    if resp == "__QUIT__":
                        cprint("Bye!", Ansi.DIM); break
                    elif resp == "__CLEAR__":
                        os.system("cls" if sys.platform == "win32" else "clear")
                    elif resp:
                        cprint(resp)
                continue
            run_turns_threaded(
                agent, cmd_ctx, text,
                lambda b: running.__setitem__(0, b)
            )
            while running[0]:
                time.sleep(0.05)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        try:
            from tools.browser_tool import shutdown_browser
            shutdown_browser()
        except Exception:
            pass

def run_chat(args):
    from cli import init_agent, setup_logging, VERSION

    config = load_config(getattr(args, "config", None))
    seeded = seed_default_config(getattr(args, "config", None))
    if not config.get("api_key"):
        print(api_key_missing_message(getattr(args, "config", None), seeded=seeded),
              file=sys.stderr)
        return 1

    setup_logging(level=logging.WARNING, also_file=True, file_level=logging.INFO)
    agent = init_agent(config)

    from datetime import datetime
    fresh_session_id = f"auto_{datetime.now():%Y%m%d_%H%M%S}"
    if getattr(args, "session", None):
        session_name = args.session
    elif getattr(args, "resume", False) and getattr(args, "query", None) is None:
        session_name = pick_session(agent, fresh_session_id)
    else:
        session_name = fresh_session_id

    from core.session import SessionSource
    cli_source = SessionSource(platform="cli", chat_id=session_name, chat_type="dm")
    session_key = agent.db.build_key(cli_source)

    query = getattr(args, "query", None)
    if query:
        # One-shot turns exist for this single reply — MCP tools discovering
        # in the background would otherwise always miss it. The interactive
        # REPL doesn't wait: human typing latency covers discovery.
        from tools import wait_for_mcp
        wait_for_mcp()
        last_kind = ["content"]
        def delta_handler(text, kind="content", by=None):
            if text is None:
                return
            if kind != last_kind[0]:
                # reasoning→content boundary: newline so the reply doesn't
                # glue onto the thinking text (color alone won't separate
                # them when ANSI is stripped/piped)
                cprint()
                last_kind[0] = kind
            cprint(text, Ansi.DIM if kind == "reasoning" else Ansi.GREEN, end="")
        def tool_start(name, args_summary, by=None):
            short = args_summary[:80] + ("..." if len(args_summary) > 80 else "")
            tag = f"[{by}] " if by else ""
            cprint(f"[tool] {tag}{name}({short})", Ansi.DIM)
        def tool_end(name, args_summary, elapsed, by=None):
            tag = f"[{by}] " if by else ""
            cprint(f"OK {tag}{name} ({elapsed:.1f}s)", Ansi.DIM)
        try:
            result = agent.chat(
                source=cli_source,
                user_message=query,
                stream_delta_callback=delta_handler,
                tool_call_start_callback=tool_start,
                tool_call_callback=tool_end
            )
            # Non-completed exits carry a closing message in the return value
            # that wasn't streamed — surface it so -q never ends silently on a
            # max_iterations / api exit.
            exit_reason = getattr(agent, "_last_exit_reason", "completed")
            if exit_reason != "completed" and result:
                color = Ansi.RED if exit_reason in ("api_timeout", "api_error") else Ansi.YELLOW
                cprint(result, color)
            cprint()
            return 0
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Interactive REPL — hybrid mode as default (works on all terminals)
    cmd_ctx = {
        "agent": agent,
        "session_key": session_key,
        "platform_adapter": None,
        "cli_source": cli_source
    }
    try:
        run_hybrid(agent, cmd_ctx, VERSION, session_key)
    except Exception as e:
        logging.getLogger(__name__).warning("Hybrid unavailable: %s, fallback to plain input", e)
        run_fallback(agent, cmd_ctx, VERSION, session_key)
    return 0