"""L0 — pure-function glyph safety in ``cli/chat._ascii_safe``.

These are the cheapest, most stable tests in the suite: deterministic, no IO,
no model. They guard the CJK-preserving / known-symbol-mapping behavior that
keeps the Windows console free of boxed-"?" glyphs.
"""
from cli.chat import _ascii_safe


def test_known_symbols_mapped_to_ascii():
    out = _ascii_safe("a → b ✓ c ❌ d")
    assert "a" in out
    assert "->" in out    # arrow
    assert "v" in out     # check mark
    assert "[x]" in out   # cross emoji


def test_cjk_preserved_verbatim():
    # Chinese text must survive — the console font covers CJK ranges.
    assert _ascii_safe("你好，世界") == "你好，世界"


def test_ansi_escapes_survive():
    # ANSI escape codes are pure ASCII and must pass through untouched.
    s = "\033[32mgreen\033[0m"
    assert _ascii_safe(s) == s


def test_empty_and_ascii_passthrough():
    assert _ascii_safe("") == ""
    assert _ascii_safe("plain ascii 123") == "plain ascii 123"
