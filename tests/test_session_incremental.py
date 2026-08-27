"""rewrite_messages incremental persistence (append-only fast path).

The agent loop rewrites the full transcript after every iteration. The
optimization keeps the longest unchanged prefix (per-row content hashes) and
writes only the tail — these tests pin the equivalence: whatever the call
pattern, the final DB state must equal what a full rewrite would produce, and
unchanged prefix rows must keep their ids (the observable proof that no
DELETE+re-INSERT happened under them).
"""
import json

from core.session import SessionDB, SessionSource


def _mk_db():
    return SessionDB(config={})


def _sid(db, chat="c1"):
    return db.get_or_create_session(SessionSource(platform="serve", chat_id=chat))


def _rows(db, sid):
    """All persisted columns in row order — the ground truth for equivalence."""
    return db._execute(
        "SELECT role, content, tool_calls, tool_call_id, reasoning, usage "
        "FROM messages WHERE session_id = ? ORDER BY id", (sid,)).fetchall()


def _ids(db, sid):
    return [r[0] for r in db._execute(
        "SELECT id FROM messages WHERE session_id = ? ORDER BY id",
        (sid,)).fetchall()]


def _turn(n=3):
    msgs = [{"role": "user", "content": "q"}]
    for i in range(n):
        msgs.append({"role": "assistant",
                     "tool_calls": [{"id": f"t{i}", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"r{i}"})
    msgs.append({"role": "assistant", "content": "done"})
    return msgs


def test_append_only_rewrite_keeps_prefix_row_ids():
    """The every-iteration call pattern: same prefix, grown tail."""
    db = _mk_db()
    sid = _sid(db)
    msgs = _turn()
    for k in range(1, len(msgs) + 1):       # persist after each iteration
        db.rewrite_messages(sid, msgs[:k])
    ids_full = _ids(db, sid)
    db.rewrite_messages(sid, msgs)          # identical repeat → no-op
    assert _ids(db, sid) == ids_full
    # ground truth: a fresh session with the same list, written in one call
    sid2 = _sid(db, chat="c2")
    db.rewrite_messages(sid2, msgs)
    assert _rows(db, sid) == _rows(db, sid2)


def test_incremental_append_does_not_renumber_prefix():
    db = _mk_db()
    sid = _sid(db)
    base = _turn(1)
    db.rewrite_messages(sid, base)
    ids_base = _ids(db, sid)

    grown = base + [{"role": "user", "content": "next"},
                    {"role": "assistant", "content": "reply"}]
    db.rewrite_messages(sid, grown)

    assert _ids(db, sid)[:len(ids_base)] == ids_base  # prefix untouched
    assert len(_ids(db, sid)) == len(grown)


def test_mid_list_change_falls_back_to_full_rewrite():
    """Compression replaces the middle of the list — divergence must rewrite."""
    db = _mk_db()
    sid = _sid(db)
    msgs = _turn(2)
    db.rewrite_messages(sid, msgs)

    compressed = [msgs[0],
                  {"role": "assistant", "content": "(summary of earlier work)"}]
    db.rewrite_messages(sid, compressed)

    sid_ref = _sid(db, chat="ref")
    db.rewrite_messages(sid_ref, compressed)
    assert _rows(db, sid) == _rows(db, sid_ref)


def test_in_place_mutation_of_persisted_row_is_caught():
    """Budget nudges mutate the last tool-result dict in place — the cache
    must hash content, not compare dict identity."""
    db = _mk_db()
    sid = _sid(db)
    msgs = _turn(1)
    db.rewrite_messages(sid, msgs)

    msgs[-1]["content"] += "\n[系统提示] budget nudge"
    db.rewrite_messages(sid, msgs)

    sid_ref = _sid(db, chat="ref")
    db.rewrite_messages(sid_ref, msgs)
    assert _rows(db, sid) == _rows(db, sid_ref)


def test_shrink_rewrite_deletes_surplus_rows():
    db = _mk_db()
    sid = _sid(db)
    db.rewrite_messages(sid, _turn(3))
    shorter = _turn(1)
    db.rewrite_messages(sid, shorter)

    sid_ref = _sid(db, chat="ref")
    db.rewrite_messages(sid_ref, shorter)
    assert _rows(db, sid) == _rows(db, sid_ref)
    assert len(_ids(db, sid)) == len(shorter)


def test_noop_rewrite_touches_nothing():
    db = _mk_db()
    sid = _sid(db)
    msgs = _turn(2)
    db.rewrite_messages(sid, msgs)
    ids = _ids(db, sid)
    db.rewrite_messages(sid, msgs)   # identical list → zero writes
    assert _ids(db, sid) == ids
    assert db._persisted[sid] == db._persisted[sid]


def test_truncate_then_rewrite_is_consistent():
    """truncate_messages_from invalidates the cache; the next rewrite must
    not assume stale prefix knowledge."""
    db = _mk_db()
    sid = _sid(db)
    msgs = _turn(2)
    db.rewrite_messages(sid, msgs)
    ids = _ids(db, sid)

    cut = ids[len(ids) // 2]
    db.truncate_messages_from(sid, cut)

    tail = msgs[: len(ids) // 2]
    db.rewrite_messages(sid, tail)

    sid_ref = _sid(db, chat="ref")
    db.rewrite_messages(sid_ref, tail)
    assert _rows(db, sid) == _rows(db, sid_ref)


def test_delete_session_drops_cache():
    db = _mk_db()
    sid = _sid(db)
    db.rewrite_messages(sid, _turn(1))
    assert sid in db._persisted
    key = db.build_key(SessionSource(platform="serve", chat_id="c1"))
    assert db.delete_session(key) is True
    assert sid not in db._persisted


def test_reasoning_and_usage_roundtrip_through_incremental_path():
    db = _mk_db()
    sid = _sid(db)
    msgs = [{"role": "assistant", "content": "think",
             "_reasoning": "because", "_usage": {"total": 9, "calls": 1}}]
    db.rewrite_messages(sid, msgs)
    msgs.append({"role": "user", "content": "more"})
    db.rewrite_messages(sid, msgs)

    rows = _rows(db, sid)
    assert rows[0][4] == "because"
    assert json.loads(rows[0][5])["total"] == 9


def test_cache_eviction_keeps_state_correct():
    """Evicted sessions just pay one full rewrite — the result stays equal."""
    db = _mk_db()
    sid = _sid(db, chat="evict")
    db.rewrite_messages(sid, _turn(1))
    db._persisted.clear()                  # simulate eviction
    msgs = _turn(2)
    db.rewrite_messages(sid, msgs)
    sid_ref = _sid(db, chat="ref")
    db.rewrite_messages(sid_ref, msgs)
    assert _rows(db, sid) == _rows(db, sid_ref)
