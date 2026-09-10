"""L1 tests for write_file's result-embedded diff (the desktop trace card's
data source — same shape as _patch's ``diff`` field)."""
import json

import pytest

from tools import file_tools


@pytest.fixture(autouse=True)
def clean_read_tracker(monkeypatch):
    """Isolate the module-global read tracker (read-before-edit gate state)."""
    monkeypatch.setattr(file_tools, "_read_tracker", {})


def _mark_read(path: str) -> None:
    file_tools._update_read_timestamp(path)


def test_write_file_overwrite_embeds_diff(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("line1\nline2\n", encoding="utf-8")
    _mark_read(str(target))

    raw = file_tools._write_file({"path": str(target), "content": "line1\nCHANGED\n"})
    result = json.loads(raw)

    assert result["success"] is True
    assert result["created"] is False
    assert "-line2" in result["diff"]
    assert "+CHANGED" in result["diff"]
    assert result["diff"].startswith("---")
    assert target.read_text(encoding="utf-8") == "line1\nCHANGED\n"


def test_write_file_new_file_created_no_diff(tmp_path):
    target = tmp_path / "new_file.py"

    raw = file_tools._write_file({"path": str(target), "content": "hello\n"})
    result = json.loads(raw)

    assert result["success"] is True
    assert result["created"] is True
    assert "diff" not in result
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_write_file_oversized_existing_skips_diff(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("x" * (512001), encoding="utf-8")
    _mark_read(str(target))

    raw = file_tools._write_file({"path": str(target), "content": "small"})
    result = json.loads(raw)

    assert result["success"] is True
    assert result["created"] is False
    assert "diff" not in result


def _make_stale(target) -> None:
    """Simulate an external edit after our read — the mtime jump is what
    the guard keys on."""
    import os
    target.write_text(target.read_text(encoding="utf-8") + "# external\n",
                      encoding="utf-8")
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_write_file_stale_overwrite_refused(tmp_path):
    # overwrite 撞上"读后被外部改过"必须硬失败——事后警告挡不住整文件
    # 覆盖冲掉外部改动（sticky coder 跨派发旧读取的高危场景）
    target = tmp_path / "a.py"
    target.write_text("v1\n", encoding="utf-8")
    _mark_read(str(target))
    _make_stale(target)

    raw = file_tools._write_file({"path": str(target), "content": "v2\n"})
    result = json.loads(raw)
    assert "error" in result
    assert "force" in result["error"]
    assert target.read_text(encoding="utf-8").endswith("# external\n")


def test_write_file_stale_force_overwrites(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("v1\n", encoding="utf-8")
    _mark_read(str(target))
    _make_stale(target)

    raw = file_tools._write_file({"path": str(target), "content": "v2\n",
                                  "force": True})
    result = json.loads(raw)
    assert result["success"] is True
    assert result["_warning"]
    assert target.read_text(encoding="utf-8") == "v2\n"


def test_write_file_stale_append_only_warns(tmp_path):
    # append 只追加不破坏既有内容——保持警告不拦截
    target = tmp_path / "log.txt"
    target.write_text("v1\n", encoding="utf-8")
    _mark_read(str(target))
    _make_stale(target)

    raw = file_tools._write_file({"path": str(target), "content": "more\n",
                                  "mode": "append"})
    result = json.loads(raw)
    assert result["success"] is True
    assert result["_warning"]
