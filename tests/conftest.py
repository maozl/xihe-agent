"""Shared pytest fixtures for xihe-agent tests.

Isolation strategy
------------------
``SessionDB`` resolves its SQLite path from the module-level constant
``core.session._DB_PATH`` (computed at import from ``AGENT_HOME``, which the
project ``config.yaml`` points at ``<repo>/.xihe-agent``). The ``isolate_db``
autouse fixture monkeypatches that constant to a tmp path per test, so the
suite never touches the real session store.
"""
import pytest

# tests/evals/ 是 eval 运行产物（gitignored）：里面是 agent 在沙盒里生成的
# 测试/代码，从 repo root 收集必然 import 失败。test_routing_eval 本身由
# XIHE_EVAL_LLM 门控，不需要被默认收集。
collect_ignore = ["evals"]


@pytest.fixture
def fake_config():
    """Minimal config dict satisfying ``XiheAgent.__init__`` + the compressor +
    the context-length lookup. Because tests inject a fake client, api_key /
    base_url are never used for real network calls."""
    return {
        "api_key": "test-key",
        "base_url": "http://test.example/v1",
        "model": "glm-test",
        "max_iterations": 30,
        "compression_threshold": 0.30,
        "models": {"glm-test": {"context_length": 128000}},
    }


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """Redirect the SQLite session DB to a per-test tmp file."""
    import core.session as session_mod
    monkeypatch.setattr(session_mod, "_DB_PATH", tmp_path / "sessions.db")
    yield


@pytest.fixture(autouse=True)
def isolate_approvals(tmp_path, monkeypatch):
    """Redirect approval-session memory (agent_home/approvals/) to per-test
    tmp and reset the in-process caches, so persisted-memory tests stay
    hermetic and never touch the real agent home."""
    import tools._approvals as ap
    monkeypatch.setattr(ap, "_MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(ap, "_SESSION_RULES", {})
    monkeypatch.setattr(ap, "_hydrated", set())
    monkeypatch.setattr(ap, "_swept", True)  # skip the per-process dir sweep
    monkeypatch.setattr(ap, "_pending_external", {})
    yield


@pytest.fixture
def make_agent(fake_config):
    """Factory: build a XiheAgent driven by an injected (fake) client.

    - ``is_subagent=True`` skips auto-title generation (which would call the
      auxiliary LLM client over the network).
    - ``system_prompt_override`` short-circuits ``_build_system_prompt`` so the
      test never loads skills / kbs preamble / project context.

    Both keep L2 tests hermetic and fast. Override either via kwargs.
    """
    from core.agent import XiheAgent

    def _make(client, **kwargs):
        return XiheAgent(
            config=fake_config,
            client=client,
            is_subagent=True,
            system_prompt_override="Test agent.",
            **kwargs,
        )

    return _make
