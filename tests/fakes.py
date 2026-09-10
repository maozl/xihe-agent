"""Fake OpenAI-compatible client for deterministic agent-loop tests (layer L2).

Mimics the *exact* attributes ``core/agent.py`` reads from the SDK
(verified against ``_non_streaming_call`` / ``_streaming_call`` / ``_record_usage``):

- non-streaming: ``client.chat.completions.create(...)`` -> a response with
  ``.choices[0].message.{content, tool_calls[]}`` and ``.usage``
- streaming: the same call with ``stream=True`` -> an iterable of chunks with
  ``.choices[0].delta.{content, tool_calls[]}`` plus a trailing usage chunk

Built with ``types.SimpleNamespace`` — zero SDK dependency, no network. A
scripted list of turns drives the agent loop so invariants (max_iterations,
tool dispatch) are reproducible without a real model.
"""
from types import SimpleNamespace


def _tool_call_ns(tc: dict) -> SimpleNamespace:
    """Non-streaming tool_call object: loop reads .id / .function.name / .function.arguments."""
    return SimpleNamespace(
        id=tc["id"],
        function=SimpleNamespace(name=tc["name"], arguments=tc.get("arguments", "{}")),
    )


def _tool_call_delta_ns(tc: dict, index: int) -> SimpleNamespace:
    """Streaming tool_call delta: loop accumulates by .index, reads .id / .function.*."""
    return SimpleNamespace(
        index=index,
        id=tc["id"],
        function=SimpleNamespace(name=tc["name"], arguments=tc.get("arguments", "{}")),
    )


class FakeChatClient:
    """A scripted stand-in for ``openai.OpenAI``.

    ``script`` is a list of turn dicts, consumed one per ``create()`` call:

        {"content": "final answer"}                  -> terminal turn (no tool calls)
        {"tool_calls": [{"id","name","arguments"}]}  -> tool-call turn (no content)
        {"content": "...", "tool_calls": [...]}      -> both

    With ``never_finish=True`` the client ignores the script once exhausted and
    keeps emitting a tool-call turn on every call — this forces the agent loop
    to run until ``max_iterations``, which is how the撞墙 exit path is tested.

    ``self.calls`` records every ``create()`` invocation for assertions.
    """

    def __init__(self, script=None, never_finish: bool = False):
        self._script = list(script or [])
        self._never_finish = never_finish
        self.calls: list[dict] = []

        owner = self

        class _Completions:
            def create(_self, **kwargs):  # noqa: N805 (matches SDK calling style)
                return owner._create(**kwargs)

        class _Chat:
            def __init__(_self):
                _self.completions = _Completions()

        self.chat = _Chat()

    def _next_turn(self) -> dict:
        if self._script:
            return self._script.pop(0)
        if self._never_finish:
            # Keep the loop busy so it must hit the iteration cap.
            return {"tool_calls": [{"id": "call_stuck", "name": "test_echo",
                                    "arguments": '{"text": "again"}'}]}
        # Scripted runs that run past their script default to a terminal turn.
        return {"content": ""}

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        turn = self._next_turn()
        content = turn.get("content")
        tcs = turn.get("tool_calls")
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)

        if kwargs.get("stream"):
            return self._build_stream(content, tcs, usage)
        return self._build_response(content, tcs, usage)

    @staticmethod
    def _build_response(content, tcs, usage):
        tool_calls = [_tool_call_ns(tc) for tc in tcs] if tcs else None
        message = SimpleNamespace(content=content, tool_calls=tool_calls,
                                  reasoning_content=None)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice], usage=usage)

    @staticmethod
    def _build_stream(content, tcs, usage):
        chunks = []
        delta_tcs = [_tool_call_delta_ns(tc, i) for i, tc in enumerate(tcs)] if tcs else None
        delta = SimpleNamespace(content=content, tool_calls=delta_tcs, reasoning_content=None)
        chunks.append(SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None))
        # Trailing usage-only chunk (empty choices) — matches include_usage behavior.
        chunks.append(SimpleNamespace(choices=[], usage=usage))
        return chunks
