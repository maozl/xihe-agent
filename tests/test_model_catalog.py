"""Tests for core/model_catalog.py — layered context-length lookup + discovery
cache, and the list_models() merge in XiheAgent.

All network paths are stubbed: _fetch_models is monkeypatched (never a real
OpenAI client), and an autouse fixture neutralizes the discovery cache so tests
can't inherit each other's entries.
"""

import pytest

import core.model_catalog as mc


@pytest.fixture(autouse=True)
def clean_cache():
    mc._discovery_cache.clear()
    yield
    mc._discovery_cache.clear()


# ---- lookup_context_length ------------------------------------------------


def test_config_entry_wins_over_table():
    assert mc.lookup_context_length(
        "glm-4.6", {"glm-4.6": {"context_length": 64000}}) == 64000


def test_known_exact():
    assert mc.lookup_context_length("glm-4.6") == 200_000
    assert mc.lookup_context_length("deepseek-chat") == 64_000
    assert mc.lookup_context_length("gpt-4.1") == 1_000_000


def test_embedded_suffix():
    assert mc.lookup_context_length("doubao-pro-32k") == 32_000
    assert mc.lookup_context_length("moonshot-v1-128k") == 128_000
    assert mc.lookup_context_length("some-model-1m") == 1_000_000


def test_suffix_outranks_family():
    # Without suffix-first ordering the doubao- family default (128k) would
    # mask the literal 32k in the name.
    assert mc.lookup_context_length("doubao-pro-32k") == 32_000


def test_family_prefix():
    assert mc.lookup_context_length("doubao-seed-1-6-250615") == 256_000
    assert mc.lookup_context_length("glm-something-new") == 128_000
    assert mc.lookup_context_length("deepseek-v3.2-exp") == 128_000


def test_absurd_suffix_rejected():
    # -2k parses to 2000; below the 4k sanity floor it must fall through to
    # family/default instead of a nonsense window.
    assert mc.lookup_context_length("widget-2k") is None


def test_prefix_shadowing_resolution():
    # Longer keys must win over their own family prefix — the import-time sort
    # exists for exactly these cases.
    assert mc.lookup_context_length("deepseek-v3.1") == 128_000
    assert mc.lookup_context_length("deepseek-v3") == 64_000
    assert mc.lookup_context_length("glm-4.6-250123") == 200_000
    assert mc.lookup_context_length("glm-4.5v") == 64_000
    assert mc.lookup_context_length("glm-4.5-air") == 128_000


def test_unknown_returns_none():
    assert mc.lookup_context_length("mystery-model") is None
    assert mc.lookup_context_length("") is None


def test_agent_static_falls_back_to_128k():
    from core.agent import XiheAgent
    assert XiheAgent._get_context_length_static({}, "mystery-model") == 128000
    assert XiheAgent._get_context_length_static({}, "glm-4.6") == 200_000


# ---- discover_models ------------------------------------------------------


def test_discovery_caches_success(monkeypatch):
    calls = []

    def fake_fetch(base_url, api_key):
        calls.append((base_url, api_key))
        return ["glm-4.6", "glm-4.5"]

    monkeypatch.setattr(mc, "_fetch_models", fake_fetch)
    assert mc.discover_models("http://x/v1", "sk-abc") == ["glm-4.6", "glm-4.5"]
    assert mc.discover_models("http://x/v1", "sk-abc") == ["glm-4.6", "glm-4.5"]
    assert len(calls) == 1  # second call served from cache


def test_discovery_failure_returns_empty_and_caches(monkeypatch):
    calls = []

    def boom(base_url, api_key):
        calls.append(1)
        raise RuntimeError("no /models here")

    monkeypatch.setattr(mc, "_fetch_models", boom)
    assert mc.discover_models("http://x/v1", "sk-abc") == []
    assert mc.discover_models("http://x/v1", "sk-abc") == []
    assert len(calls) == 1


def test_discovery_force_bypasses_cache(monkeypatch):
    monkeypatch.setattr(mc, "_fetch_models", lambda *a: ["a"])
    mc.discover_models("http://x/v1", "sk-abc")
    monkeypatch.setattr(mc, "_fetch_models", lambda *a: ["a", "b"])
    assert mc.discover_models("http://x/v1", "sk-abc", force=True) == ["a", "b"]


def test_discovery_without_credentials():
    assert mc.discover_models("", "sk") == []
    assert mc.discover_models("http://x/v1", "") == []


# ---- XiheAgent.list_models merge -----------------------------------------


@pytest.fixture
def no_discovery(monkeypatch):
    monkeypatch.setattr(mc, "discover_models", lambda *a, **k: [])


def test_list_models_config_only(make_agent, no_discovery):
    agent = make_agent(None)
    names = [m["name"] for m in agent.list_models()]
    assert names == ["glm-test"]
    entry = agent.list_models()[0]
    assert entry["current"] is True
    assert entry["context_length"] == 128000
    assert entry["source"] == "config"


def test_list_models_merges_discovered(make_agent, monkeypatch):
    monkeypatch.setattr(mc, "discover_models",
                        lambda *a, **k: ["glm-4.6", "glm-test"])
    agent = make_agent(None)
    models = {m["name"]: m for m in agent.list_models()}
    # config entry survives with its metadata; discovered ID gets table length
    assert models["glm-test"]["source"] == "config"
    assert models["glm-4.6"]["context_length"] == 200_000
    assert models["glm-4.6"]["current"] is False
    assert models["glm-test"]["current"] is True


def test_list_models_default_entry_when_nothing(make_agent, no_discovery,
                                                fake_config):
    fake_config.pop("models", None)
    agent = make_agent(None)
    models = agent.list_models()
    assert len(models) == 1
    assert models[0]["name"] == "glm-test"
    assert models[0]["source"] == "default"
