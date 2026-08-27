"""enforce_turn_budget — aggregate spill semantics over a turn's tool rows.

The agent passes the whole turn tail (user/assistant rows included), so the
role filter is load-bearing: a non-tool row must never be replaced by a
persisted-output stub.
"""
from tools.tool_result_storage import (
    DEFAULT_TURN_BUDGET, PERSISTED_TAG, enforce_turn_budget,
    maybe_persist_tool_result,
)


def test_under_budget_is_noop():
    msgs = [{"role": "tool", "content": "x" * 500, "tool_call_id": "a"},
            {"role": "tool", "content": "y" * 500, "tool_call_id": "b"}]
    out = enforce_turn_budget(msgs, budget=10_000)
    assert out is msgs
    assert out[0]["content"] == "x" * 500


def test_spills_largest_first_until_under_budget():
    msgs = [
        {"role": "tool", "content": "s" * 3_000, "tool_call_id": "small"},
        {"role": "tool", "content": "L" * 50_000, "tool_call_id": "big"},
        {"role": "tool", "content": "m" * 20_000, "tool_call_id": "mid"},
    ]
    out = enforce_turn_budget(msgs, budget=10_000)
    assert PERSISTED_TAG in out[1]["content"]   # big spilled
    total = sum(len(m["content"]) for m in out)
    assert total <= 10_000 + 3_000              # small survives whole
    assert out[0]["content"] == "s" * 3_000


def test_non_tool_rows_are_ignored_completely():
    user_text = "user words " * 400             # 4.4K chars — over candidate size
    msgs = [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": "A" * 60_000},
        {"role": "tool", "content": "t" * 30_000, "tool_call_id": "t1"},
    ]
    out = enforce_turn_budget(msgs, budget=10_000)
    assert out[0]["content"] == user_text       # untouched
    assert out[1]["content"] == "A" * 60_000
    assert PERSISTED_TAG in out[2]["content"]   # only the tool row spilled


def test_roleless_rows_still_treated_as_tool():
    """Historical call shape passed bare dicts without a role key."""
    msgs = [{"content": "z" * 30_000, "tool_call_id": "bare"}]
    out = enforce_turn_budget(msgs, budget=10_000)
    assert PERSISTED_TAG in out[0]["content"]


def test_already_persisted_rows_not_respilled():
    persisted = maybe_persist_tool_result("R" * 40_000, "run_check", "id1")
    assert PERSISTED_TAG in persisted
    msgs = [{"role": "tool", "content": persisted, "tool_call_id": "id1"}]
    out = enforce_turn_budget(msgs, budget=100)  # far under any budget
    assert out[0]["content"] == persisted


def test_default_budget_constant_unchanged():
    assert DEFAULT_TURN_BUDGET == 80_000
