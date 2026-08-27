"""L1 tests for the capability store (core/store.py) — hermetic, local sources only.

Isolation: ledger + skills dir are monkeypatched into tmp_path; config.yaml is
a stub dict; mcp_tool's connect/status functions are stubs (no network, no SDK).
"""

import json
import zipfile
from pathlib import Path

import pytest

import core.store as store_mod


@pytest.fixture(autouse=True)
def isolate_store(tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(store_mod, "STORE_DIR", store_dir)
    monkeypatch.setattr(store_mod, "_LEDGER_PATH", store_dir / "installed.json")
    monkeypatch.setattr(store_mod, "_USER_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(store_mod, "_BUNDLED_SKILLS_DIR", tmp_path / "bundled")
    monkeypatch.setattr(store_mod, "_catalog_cache", {"at": 0.0, "data": None})
    yield


@pytest.fixture
def config_holder(monkeypatch):
    import core.config as config_mod
    holder = {"config": {"store": {"sources": []}, "mcp_servers": {}}}
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: holder["config"])
    return holder


@pytest.fixture
def stub_mcp(monkeypatch):
    import tools.mcp_tool as mcp_mod
    state = {"status": []}
    monkeypatch.setattr(mcp_mod, "discover_mcp_tools", lambda: [])
    monkeypatch.setattr(mcp_mod, "get_mcp_status", lambda: state["status"])
    monkeypatch.setattr(mcp_mod, "remove_mcp_server", lambda name: True)
    return state


def make_skill_pkg(base: Path, name="demo-skill", frontmatter=None) -> Path:
    d = base / name
    (d / "scripts").mkdir(parents=True)
    fm = frontmatter or (
        "---\nname: demo-skill\ndescription: A demo skill.\n---\n")
    (d / "SKILL.md").write_text(fm + "\nDo the thing.\n", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return d


def make_zip(path: Path, entries: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


SKILL_ITEM = {"id": "demo-skill", "type": "skill", "title": "Demo", "version": "1.0.0"}

MCP_ITEM = {
    "id": "wecom-docs", "type": "mcp", "title": "企微文档", "version": "2.0.0",
    "mcp": {"type": "streamable-http", "url": "https://x.invalid/mcp?k={api_key}"},
    "config_schema": [{"key": "api_key", "label": "API Key",
                       "type": "password", "required": True}],
}


# --- ledger -------------------------------------------------------------------

def test_ledger_roundtrip_and_corrupt_fallback():
    led = store_mod._empty_ledger()
    led["skill"]["x"] = {"version": "1"}
    store_mod._save_ledger(led)
    assert store_mod._load_ledger()["skill"]["x"]["version"] == "1"
    store_mod._LEDGER_PATH.write_text("{not json", encoding="utf-8")
    fresh = store_mod._load_ledger()
    assert fresh["skill"] == {} and fresh["mcp"] == {} and fresh["mounts"] == {}


# --- skill install ------------------------------------------------------------

def test_skill_install_from_local_path(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    assert result["success"], result
    dest = store_mod._USER_SKILLS_DIR / "demo-skill"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "run.py").is_file()
    assert store_mod._load_ledger()["skill"]["demo-skill"]["name"] == "demo-skill"


def test_skill_install_from_zip_strips_single_top_dir(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    zip_path = make_zip(tmp_path / "pkg.zip", {
        f"wrapper/{p.relative_to(pkg).as_posix()}": p.read_text(encoding="utf-8")
        for p in pkg.rglob("*") if p.is_file()
    })
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"zip": str(zip_path)}})
    assert result["success"], result
    assert (store_mod._USER_SKILLS_DIR / "demo-skill" / "SKILL.md").is_file()


def test_zip_slip_rejected(tmp_path):
    zip_path = make_zip(tmp_path / "evil.zip", {
        "SKILL.md": "---\nname: demo-skill\ndescription: x\n---\nbody",
        "../evil.txt": "escaped",
    })
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"zip": str(zip_path)}})
    assert not result["success"]
    assert "unsafe" in result["error"]
    assert not (tmp_path / "evil.txt").exists()


def test_skill_install_blocked_by_nested_hand_install(tmp_path):
    # hand-installed grouped layout (skills/<group>/<name>/) dodges the
    # one-level dest check but not the frontmatter-name check
    make_skill_pkg(store_mod._USER_SKILLS_DIR / "grp")
    pkg = make_skill_pkg(tmp_path / "origin")
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    assert not result["success"]
    assert "already taken" in result["error"]
    assert "grp" in result["error"]
    assert not (store_mod._USER_SKILLS_DIR / "demo-skill").exists()


def test_skill_install_blocked_by_bundled_name(tmp_path):
    # a user install shadowed by a bundled name would be invisible
    # (_scan_skills dedupes bundled-first) — refuse instead of installing
    make_skill_pkg(store_mod._BUNDLED_SKILLS_DIR)
    pkg = make_skill_pkg(tmp_path / "origin")
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    assert not result["success"]
    assert "bundled" in result["error"]
    assert not (store_mod._USER_SKILLS_DIR / "demo-skill").exists()


def test_skill_layout_rejects_unexpected_root_file(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    (pkg / "extra.bin").write_bytes(b"\x00")
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    assert not result["success"]
    assert "not allowed in a skill package" in result["error"]


def test_skill_install_refuses_non_store_conflict(tmp_path):
    dest = store_mod._USER_SKILLS_DIR / "demo-skill"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("hand-made", encoding="utf-8")
    pkg = make_skill_pkg(tmp_path / "origin")
    result = store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    assert not result["success"]
    assert "not store-installed" in result["error"]


def test_skill_upgrade_overwrites(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    item = {**SKILL_ITEM, "source": {"path": str(pkg)}}
    assert store_mod.install_skill(item)["success"]
    (pkg / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: v2\n---\n\nNew body.\n", encoding="utf-8")
    result = store_mod.install_skill({**item, "version": "2.0.0"})
    assert result["success"], result
    body = (store_mod._USER_SKILLS_DIR / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "New body" in body


def test_skill_uninstall(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    store_mod.set_mount("skill", "demo-skill", ["main"])
    result = store_mod.uninstall("skill", "demo-skill")
    assert result["success"], result
    assert not (store_mod._USER_SKILLS_DIR / "demo-skill").exists()
    led = store_mod._load_ledger()
    assert "demo-skill" not in led["skill"]
    assert store_mod.mount_targets("skill", "demo-skill") == []
    assert store_mod.uninstall("skill", "demo-skill")["success"] is False


def test_uninstall_refuses_unknown_kind():
    assert store_mod.uninstall("plugin", "x")["success"] is False


# --- MCP install ----------------------------------------------------------------

def test_mcp_install_renders_secret(config_holder, stub_mcp):
    result = store_mod.install_mcp(dict(MCP_ITEM), {"api_key": "S3CR3T"})
    assert result["success"], result
    installed = store_mod.store_installed_mcp()
    assert "k=S3CR3T" in installed["wecom-docs"]["url"]
    # secrets never leak through the catalog view — only which keys are filled
    view = store_mod.catalog_view({"items": [dict(MCP_ITEM)], "sources": []})
    entry = next(e for e in view["items"] if e["id"] == "wecom-docs")
    assert entry["secrets_set"] == {"api_key": True}
    assert "S3CR3T" not in json.dumps(view)


def test_mcp_required_secret_missing(config_holder, stub_mcp):
    result = store_mod.install_mcp(dict(MCP_ITEM), {})
    assert not result["success"]
    assert "api_key" in result["error"]


def test_mcp_secret_inherited_on_upgrade(config_holder, stub_mcp):
    assert store_mod.install_mcp(dict(MCP_ITEM), {"api_key": "OLD"})["success"]
    upgraded = {**MCP_ITEM, "version": "2.1.0"}
    result = store_mod.install_mcp(upgraded, {})  # no values re-supplied
    assert result["success"], result
    assert "k=OLD" in store_mod.store_installed_mcp()["wecom-docs"]["url"]


def test_mcp_refuses_manual_config_collision(config_holder, stub_mcp):
    config_holder["config"]["mcp_servers"]["wecom-docs"] = {"url": "https://manual"}
    result = store_mod.install_mcp(dict(MCP_ITEM), {"api_key": "x"})
    assert not result["success"]
    assert "config.yaml" in result["error"]


def test_mcp_stdio_entry_unsupported(config_holder, stub_mcp):
    stdio_item = {"id": "fs", "type": "mcp", "mcp": {"command": "npx"}}
    result = store_mod.install_mcp(stdio_item, {})
    assert not result["success"]
    assert "streamable-http" in result["error"]


def test_load_mcp_config_merges_with_manual_priority(config_holder, stub_mcp):
    from tools.mcp_tool import _load_mcp_config
    config_holder["config"]["mcp_servers"] = {"wecom-docs": {"url": "https://manual"}}
    store_mod.install_mcp(dict(MCP_ITEM), {"api_key": "S"})
    store_mod.install_mcp(
        {"id": "other", "type": "mcp", "version": "1",
         "mcp": {"url": "https://other.invalid/mcp"}}, {})
    merged = _load_mcp_config()
    assert merged["wecom-docs"]["url"] == "https://manual"   # hand-written wins
    assert merged["other"]["url"] == "https://other.invalid/mcp"


# --- catalog -------------------------------------------------------------------

def test_catalog_local_source_and_view(tmp_path, config_holder):
    index_dir = tmp_path / "src"
    index_dir.mkdir()
    pkg = make_skill_pkg(tmp_path / "origin")
    (index_dir / "index.json").write_text(json.dumps({
        "version": 1,
        "items": [
            {"id": "demo-skill", "type": "skill", "title": "Demo",
             "version": "1.0.0", "source": {"path": str(pkg)}},
            dict(MCP_ITEM),
        ],
    }), encoding="utf-8")
    config_holder["config"]["store"]["sources"] = [
        {"name": "local", "url": str(index_dir / "index.json")}]

    catalog = store_mod.fetch_catalog(force=True)
    assert len(catalog["items"]) == 2
    assert catalog["sources"][0]["ok"] is True

    view = store_mod.catalog_view(catalog)
    entry = next(e for e in view["items"] if e["id"] == "demo-skill")
    assert entry["installed"] is False and entry.get("orphan") is not True

    store_mod.install_skill(next(
        i for i in catalog["items"] if i["id"] == "demo-skill"))
    (index_dir / "index.json").write_text(json.dumps({
        "version": 1,
        "items": [
            {"id": "demo-skill", "type": "skill", "title": "Demo",
             "version": "2.0.0", "source": {"path": str(pkg)}},
        ],
    }), encoding="utf-8")
    view = store_mod.catalog_view(store_mod.fetch_catalog(force=True))
    entry = next(e for e in view["items"] if e["id"] == "demo-skill")
    assert entry["installed"] is True and entry["upgradable"] is True
    # wecom-docs vanished from the source but was never installed → just gone
    assert all(e["id"] != "wecom-docs" for e in view["items"])


def test_catalog_orphan_installed_but_source_dropped(tmp_path, config_holder):
    pkg = make_skill_pkg(tmp_path / "origin")
    store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    config_holder["config"]["store"]["sources"] = []
    view = store_mod.catalog_view(store_mod.fetch_catalog(force=True))
    orphan = next(e for e in view["items"] if e["id"] == "demo-skill")
    assert orphan["orphan"] is True and orphan["installed"] is True


def test_stdio_entry_flagged_unsupported_in_catalog(tmp_path, config_holder):
    index_dir = tmp_path / "src"
    index_dir.mkdir()
    (index_dir / "index.json").write_text(json.dumps({"items": [
        {"id": "fs", "type": "mcp", "version": "1", "mcp": {"command": "npx"}},
    ]}), encoding="utf-8")
    config_holder["config"]["store"]["sources"] = [
        {"name": "local", "url": str(index_dir / "index.json")}]
    catalog = store_mod.fetch_catalog(force=True)
    assert catalog["items"][0].get("unsupported")


# --- mounts -------------------------------------------------------------------

def test_mount_flow_and_merge_semantics(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    store_mod.install_mcp(dict(MCP_ITEM), {"api_key": "S"})

    result = store_mod.set_mount("skill", "demo-skill", ["main", "itsm"])
    assert result["success"] and sorted(result["mounted"]) == ["itsm", "main"]
    store_mod.set_mount("mcp", "wecom-docs", ["itsm"])

    # skill token lands in the skills set; mcp token in the toolsets set
    ts_main, sk_main = store_mod.mounted_extra("main")
    assert sk_main == {"demo-skill"} and ts_main == set()
    ts_itsm, sk_itsm = store_mod.mounted_extra("itsm")
    assert ts_itsm == {"mcp-wecom-docs"} and sk_itsm == {"demo-skill"}

    # None (unrestricted) stays None; a fixed roster gets the union
    assert store_mod.merge_mounts("itsm", None, None) == (None, None)
    ts, sk = store_mod.merge_mounts("itsm", {"files"}, ["other-skill"])
    assert ts == {"files", "mcp-wecom-docs"}
    assert sk == {"other-skill", "demo-skill"}

    # re-mount replaces (no duplicate tokens on other agents)
    store_mod.set_mount("skill", "demo-skill", ["main"])
    assert store_mod.mounted_extra("itsm")[1] == set()


def test_uninstall_purges_mount_tokens(tmp_path):
    pkg = make_skill_pkg(tmp_path / "origin")
    store_mod.install_skill({**SKILL_ITEM, "source": {"path": str(pkg)}})
    store_mod.set_mount("skill", "demo-skill", ["main", "itsm"])
    store_mod.uninstall("skill", "demo-skill")
    assert store_mod._load_ledger()["mounts"] == {}
