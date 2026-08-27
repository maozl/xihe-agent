"""First-start seeding of config.yaml from the annotated template.

Pins: seeding only when absent (never clobbers a user file), the seeded
file is the real annotated template (not a stub), and the api_key gate
message points first-start users at the seeded file instead of teaching
the YAML format from scratch.
"""
import core.config as c


def test_seeds_when_absent_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "AGENT_HOME", tmp_path)
    cfg = tmp_path / "config.yaml"
    assert c.seed_default_config(str(cfg)) is True
    text = cfg.read_text(encoding="utf-8")
    assert "api_key" in text and "toolsets" in text  # real template, not a stub

    cfg.write_text("user edits", encoding="utf-8")
    assert c.seed_default_config(str(cfg)) is False
    assert cfg.read_text(encoding="utf-8") == "user edits"


def test_no_repo_template_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_REPO_ROOT", tmp_path)  # no template here
    assert c.seed_default_config(str(tmp_path / "config.yaml")) is False


def test_gate_message_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "AGENT_HOME", tmp_path)
    cfg = str(tmp_path / "config.yaml")

    seeded_msg = c.api_key_missing_message(cfg, seeded=True)
    assert cfg in seeded_msg and "just created" in seeded_msg

    # non-seeded message keeps the paste-ready snippet + template pointer
    plain_msg = c.api_key_missing_message(cfg)
    for needle in ("api_key:", "base_url:", "toolsets:", "config.example.yaml"):
        assert needle in plain_msg
