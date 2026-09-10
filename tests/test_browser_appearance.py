"""L0/L1 tests for the desktop-pushed browser appearance state."""

import json

import tools.browser_tool as bt


class TestThemeLaunchArgs:
    def test_dark_forces_dark_mode(self):
        assert bt._theme_launch_args(True) == ["--force-dark-mode"]

    def test_light_follows_os(self):
        # no force-light flag exists — light means "follow the OS"
        assert bt._theme_launch_args(False) == []


class TestAppearancePersistence:
    def test_missing_file_defaults_os(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_APPEARANCE_FILE", tmp_path / "appearance.json")
        assert bt._appearance_dark() is False

    def test_set_then_read_roundtrip(self, monkeypatch, tmp_path):
        f = tmp_path / "appearance.json"
        monkeypatch.setattr(bt, "_APPEARANCE_FILE", f)
        res = bt.set_appearance(True)
        assert res == {"ok": True, "dark": True}
        assert json.loads(f.read_text(encoding="utf-8")) == {"dark": True}
        assert bt._appearance_dark() is True

    def test_set_coerces_to_bool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_APPEARANCE_FILE", tmp_path / "appearance.json")
        bt.set_appearance(1)
        assert bt._appearance_dark() is True

    def test_corrupt_file_defaults_os(self, monkeypatch, tmp_path):
        f = tmp_path / "appearance.json"
        f.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(bt, "_APPEARANCE_FILE", f)
        assert bt._appearance_dark() is False
