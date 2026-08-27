"""First-run behavior of `xihe serve` with an unconfigured model connection.

The desktop drives serve before config.yaml has an api_key. Under openai>=2
the OpenAI client raises at construction (inside ctx.create_agent), which used
to escape the turn task silently — the client waited on turn_start forever.
These tests pin the error-event contract and the api_error reason forwarding
without touching a real OpenAI client (works under openai 1.x and 2.x alike).
"""
import asyncio

from gateway.serve import ServeApp


class FakeWs:
    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


class FakeDb:
    def build_key(self, source):
        return f"agent:main:serve:dm:{source.chat_id}"


class FakeCtx:
    """Stands in for SharedContext: create_agent raises or returns a stub."""

    def __init__(self, config, create_agent=None):
        self.config = config
        self.db = FakeDb()
        self.main_toolsets = []
        self.main_skills = []
        if create_agent is not None:
            self.create_agent = create_agent

    def create_agent(self, **kwargs):
        raise Exception("the api_key client must be set")


class StubAgent:
    """Mimics what the turn worker reads off a real XiheAgent."""

    def __init__(self, reply, exit_reason):
        self._reply = reply
        self._last_exit_reason = exit_reason
        self._turn_usage = {}

    def chat(self, **kwargs):
        return self._reply

    def _drain_steer(self):
        return None


def _make_app(config, create_agent=None):
    return ServeApp(FakeCtx(config, create_agent), version="test")


def _events(ws, etype):
    return [e for e in ws.sent if e.get("type") == etype]


def test_send_missing_api_key_emits_ws_error():
    app = _make_app({"api_key": "", "model": "m"})  # create_agent raises
    ws = FakeWs()
    asyncio.run(app._handle_send(ws, {"conv_id": "c1", "text": "hi"}))
    errors = _events(ws, "error")
    assert len(errors) == 1
    assert errors[0]["conv_id"] == "c1"
    assert errors[0]["turn_id"]
    assert "api_key" in errors[0]["message"]
    assert "未配置" in errors[0]["message"]
    assert errors[0]["code"] == "api_key_missing"
    assert app._active == {}


def test_send_agent_creation_failure_generic():
    app = _make_app({"api_key": "sk-x", "model": "m"})
    ws = FakeWs()
    asyncio.run(app._handle_send(ws, {"conv_id": "c1", "text": "hi"}))
    errors = _events(ws, "error")
    assert len(errors) == 1
    assert "创建 agent 失败" in errors[0]["message"]
    assert errors[0]["code"] == "agent_create_failed"
    assert app._active == {}


def test_complete_carries_api_error_reason():
    # openai 1.x path: construction succeeds, the first call 401s, chat()
    # RETURNS the error text with _last_exit_reason="api_error".
    app = _make_app(
        {"api_key": "sk-x", "model": "m"},
        create_agent=lambda **kw: StubAgent("API error: Error code: 401 - bad key",
                                            "api_error"),
    )
    ws = FakeWs()
    asyncio.run(app._handle_send(ws, {"conv_id": "c1", "text": "hi"}))
    dones = _events(ws, "complete")
    assert len(dones) == 1
    assert dones[0]["reason"] == "api_error"
    assert dones[0]["text"].startswith("API error")
    assert app._active == {}


def test_api_key_missing_message():
    from core.config import api_key_missing_message

    msg = api_key_missing_message("/x/y/config.yaml")
    for needle in ("api_key:", "base_url:", "toolsets:", "config.example.yaml",
                   "/x/y/config.yaml"):
        assert needle in msg
