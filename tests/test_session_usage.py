"""Per-turn token usage persistence (messages.usage column).

The column must survive the transcript rewrite that happens on every turn —
rewrite_messages DELETEs + re-INSERTs the whole session, so the usage written
onto a completed turn's final assistant row is only durable if load_messages
brings it back into memory (as ``_usage``) for the next rewrite to re-persist.
"""
import json

from core.session import SessionDB, SessionSource


def _mk_db():
    return SessionDB(config={})


def _seed_turn(db) -> str:
    sid = db.get_or_create_session(SessionSource(platform="serve", chat_id="c1"))
    db.append_message(sid, "user", "hi")
    db.append_message(sid, "assistant", "hello")
    return sid


def test_set_last_assistant_usage_targets_newest_assistant_row():
    db = _mk_db()
    sid = db.get_or_create_session(SessionSource(platform="serve", chat_id="c1"))
    db.append_message(sid, "assistant", "old turn")
    db.append_message(sid, "user", "next question")
    db.append_message(sid, "assistant", "new turn")

    db.set_last_assistant_usage(sid, {"prompt": 10, "completion": 5,
                                      "total": 15, "calls": 1})

    rows = db.load_messages_with_id(sid)
    with_usage = [m for m in rows if m.get("usage")]
    assert len(with_usage) == 1
    assert with_usage[0]["content"] == "new turn"
    assert with_usage[0]["usage"]["total"] == 15


def test_usage_survives_next_turn_rewrite():
    """The load→rewrite round-trip is what keeps historical usage durable."""
    db = _mk_db()
    sid = _seed_turn(db)
    db.set_last_assistant_usage(sid, {"prompt": 100, "completion": 40,
                                      "total": 140, "calls": 2})

    loaded = db.load_messages(sid)
    assert loaded[-1]["_usage"]["total"] == 140

    loaded.append({"role": "user", "content": "again"})
    loaded.append({"role": "assistant", "content": "sure"})
    db.rewrite_messages(sid, loaded)

    rows = db.load_messages_with_id(sid)
    usage_rows = [m for m in rows if m.get("usage")]
    assert len(usage_rows) == 1
    assert usage_rows[0]["content"] == "hello"


def test_load_messages_usage_stays_internal_and_json():
    db = _mk_db()
    sid = _seed_turn(db)
    db.set_last_assistant_usage(sid, {"prompt": 1, "completion": 2,
                                      "total": 3, "calls": 1})
    loaded = db.load_messages(sid)
    assert loaded[-1]["_usage"] == {"prompt": 1, "completion": 2,
                                    "total": 3, "calls": 1}
    # nothing non-underscore beyond the OpenAI shape leaks into the model path
    assert set(loaded[-1]) <= {"role", "content", "tool_calls",
                               "tool_call_id", "_usage", "_reasoning"}


def test_rewrite_without_usage_column_data_is_fine():
    """Messages lacking _usage (e.g. a fresh in-memory list) write NULL."""
    db = _mk_db()
    sid = db.get_or_create_session(SessionSource(platform="serve", chat_id="c2"))
    db.rewrite_messages(sid, [{"role": "user", "content": "x"}])
    rows = db._execute(
        "SELECT usage FROM messages WHERE session_id = ?", (sid,)).fetchall()
    assert rows == [(None,)]


def test_reshape_history_folds_usage():
    from gateway.serve import _reshape_history

    rows = [
        {"id": 1, "role": "user", "content": "hi"},
        {"id": 2, "role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"id": 3, "role": "tool", "tool_call_id": "t1", "content": "r"},
        {"id": 4, "role": "assistant", "content": "done",
         "usage": {"prompt": 7, "completion": 3, "total": 10, "calls": 1}},
    ]
    out = _reshape_history(rows)
    assert len(out) == 2
    assert out[1]["usage"]["total"] == 10
    # turns without usage don't carry the key at all
    rows2 = [
        {"id": 1, "role": "user", "content": "hi"},
        {"id": 2, "role": "assistant", "content": "old"},
    ]
    assert "usage" not in _reshape_history(rows2)[1]
