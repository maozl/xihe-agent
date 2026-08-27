"""Unit probes for core.diagnostics (no network, no registry load needed)."""
from core.diagnostics import (capability_matrix, check_connectivity,
                              platform_missing_fields)


def test_platform_missing_fields():
    assert platform_missing_fields("wecom", {"bot_id": "x"}) == ["secret"]
    assert platform_missing_fields("wecom", {}) == ["bot_id", "secret"]
    assert platform_missing_fields("feishu", {"app_id": "a", "app_secret": "s"}) == []
    assert platform_missing_fields("unknown-platform", {}) == []
    # placeholder-looking values count as present (truthy) — preflight only
    # reports genuinely absent fields
    assert platform_missing_fields("wecom", {"bot_id": " ", "secret": None}) == ["bot_id", "secret"]


def test_check_connectivity_refused():
    r = check_connectivity("http://127.0.0.1:1/v1", "k", timeout=2)
    assert r["ok"] is False and r["error"]


def test_capability_matrix_off_reasons_name_the_fix():
    caps = capability_matrix({}, toolsets=None, names=set())
    assert caps["vision"]["ready"] is False
    assert "vision_model" in caps["vision"]["reason"]
    assert caps["web_search"]["ready"] is False
    assert "搜索 key" in caps["web_search"]["reason"]


def test_capability_matrix_all_on():
    names = {"browser_navigate", "vision_analyze", "image_ocr",
             "web_search", "run_sandbox_code"}
    caps = capability_matrix({}, toolsets=None, names=names)
    assert all(c["ready"] for c in caps.values())
    assert all(c["reason"] is None for c in caps.values())


def test_capability_matrix_roster_reason():
    caps = capability_matrix({}, toolsets=["files"], names=set())
    assert "toolsets 未包含 web" in caps["browser"]["reason"]
