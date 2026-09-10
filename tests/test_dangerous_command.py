"""L0 — dangerous-command detection in ``tools/terminal._detect_dangerous_command``.

Guards the regex gate that blocks destructive shell commands in gateway mode.
Pure function, no subprocess, no model.
"""
from tools.terminal import _detect_dangerous_command


def test_recursive_delete_root_flagged():
    flagged, _key, desc = _detect_dangerous_command("rm -rf /")
    assert flagged is True
    assert desc


def test_sql_drop_flagged():
    flagged, _key, _desc = _detect_dangerous_command("DROP TABLE users")
    assert flagged is True


def test_sql_delete_without_where_flagged():
    flagged, _key, _desc = _detect_dangerous_command("DELETE FROM users")
    assert flagged is True
    # ...but a DELETE with WHERE is allowed:
    flagged_where, _, _ = _detect_dangerous_command("DELETE FROM users WHERE id=1")
    assert flagged_where is False


def test_benign_command_not_flagged():
    flagged, key, desc = _detect_dangerous_command("ls -la")
    assert flagged is False
    assert key is None and desc is None


def test_powershell_recursive_remove_item_flagged():
    # 2026-08-20 实际事故形态：从 $RECYCLE.BIN 递归删除回收站项
    cmd = ("powershell -Command \"Remove-Item -LiteralPath "
           "'E:\\$RECYCLE.BIN\\S-1-5-21\\$R8TD6EO' -Recurse -Force\"")
    flagged, _key, _desc = _detect_dangerous_command(cmd)
    assert flagged is True


def test_windows_recursive_delete_variants_flagged():
    for cmd in ("rd /s /q C:\\temp\\build", "rmdir /s build",
                "del /s /q *.log", "erase /s junk",
                "Clear-RecycleBin -Force", "format e:"):
        assert _detect_dangerous_command(cmd)[0] is True, cmd


def test_single_file_delete_not_flagged():
    # 与 Unix `rm file` 对称：非递归单文件删除不拦（避免审批淹没日常操作）
    for cmd in ("Remove-Item -LiteralPath 'C:\\tmp\\a.txt'",
                "del a.txt", "rm a.txt"):
        assert _detect_dangerous_command(cmd)[0] is False, cmd


def test_recycle_bin_enumerate_then_delete_flagged():
    # 2026-08-20 拒绝后的改写绕过形态：去掉 -Recurse 的非递归 Remove-Item，
    # 靠「枚举回收站签名 × 删除动词」定向兜住
    cmd = ("powershell -Command \"$shell = New-Object -ComObject Shell.Application; "
           "$rb = $shell.Namespace(0xA); "
           "$item = $rb.Items() | Where-Object { $_.Name -eq 'logo.png' }; "
           "$path = $item.Path; Remove-Item -LiteralPath $path -Force\"")
    assert _detect_dangerous_command(cmd)[0] is True


def test_recycle_bin_delete_before_enumerate_flagged():
    cmd = "Remove-Item -LiteralPath $path -Force; $shell.Namespace(0xA).Items().Count"
    assert _detect_dangerous_command(cmd)[0] is True


def test_recycle_bin_listing_only_not_flagged():
    cmd = ("powershell -Command \"$shell = New-Object -ComObject Shell.Application; "
           "$rb = $shell.Namespace(0xA); $rb.Items().Count\"")
    assert _detect_dangerous_command(cmd)[0] is False
