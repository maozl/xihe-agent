"""ContextCompressor per-session summary isolation.

SharedContext shares one compressor across every chat — the progressive
summary (PREVIOUS SUMMARY) memory must be keyed by session or chat A's
compaction prompt carries chat B's summary.
"""
from core.compressor import ContextCompressor


class FakeAux:
    def __init__(self):
        self.prompts = []

    def call_llm(self, task, messages, max_tokens):
        from types import SimpleNamespace
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=f"summary#{len(self.prompts)}"))])


def _msgs(n=20):
    out = [{"role": "system", "content": "s"},
           {"role": "user", "content": "hi"}]
    for i in range(n):
        out.append({"role": "assistant", "content": f"answer {i} " * 50})
        out.append({"role": "user", "content": f"next {i}"})
    return out


def test_previous_summary_isolated_per_session():
    aux = FakeAux()
    c = ContextCompressor(context_length=4000, threshold_percent=0.05, aux=aux)

    c.compress(_msgs(), session_key="chat:A")
    assert "PREVIOUS SUMMARY" not in aux.prompts[0]

    c.compress(_msgs(), session_key="chat:B")
    # B's first compaction must NOT see A's summary
    assert "PREVIOUS SUMMARY" not in aux.prompts[1]

    c.compress(_msgs(), session_key="chat:A")
    # A's second compaction resumes from A's own summary only
    assert "summary#1" in aux.prompts[2]
    assert "summary#2" not in aux.prompts[2]


def test_default_bucket_keeps_legacy_behaviour():
    aux = FakeAux()
    c = ContextCompressor(context_length=4000, threshold_percent=0.05, aux=aux)
    c.compress(_msgs())
    c.compress(_msgs())
    assert "PREVIOUS SUMMARY:\nsummary#1" in aux.prompts[1]
