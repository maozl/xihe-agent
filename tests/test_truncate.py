"""L1: SessionDB.truncate_messages_from — the rollback primitive behind the
desktop's 重新发送 (POST /convs/{id}/truncate)."""


def test_truncate_drops_from_given_row_inclusive(tmp_path, monkeypatch):
    import core.session as session_mod
    from core.session import SessionDB, SessionSource

    monkeypatch.setattr(session_mod, "_DB_PATH", tmp_path / "sessions.db")
    db = SessionDB()
    session_id = db.get_or_create_session(
        SessionSource(platform="serve", chat_id="c1", user_id="u", chat_type="dm"))
    for role, text in (("user", "q1"), ("assistant", "a1"),
                       ("user", "q2"), ("assistant", "a2")):
        db.append_message(session_id, role, text)

    rows = db.load_messages_with_id(session_id)
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]

    deleted = db.truncate_messages_from(session_id, rows[2]["id"])
    assert deleted == 2
    rest = db.load_messages_with_id(session_id)
    assert [r["content"] for r in rest] == ["q1", "a1"]


def test_truncate_first_user_message_empties_session(tmp_path, monkeypatch):
    import core.session as session_mod
    from core.session import SessionDB, SessionSource

    monkeypatch.setattr(session_mod, "_DB_PATH", tmp_path / "sessions.db")
    db = SessionDB()
    session_id = db.get_or_create_session(
        SessionSource(platform="serve", chat_id="c2", user_id="u", chat_type="dm"))
    db.append_message(session_id, "user", "q1")
    db.append_message(session_id, "assistant", "a1")

    first = db.load_messages_with_id(session_id)[0]
    assert db.truncate_messages_from(session_id, first["id"]) == 2
    assert db.load_messages_with_id(session_id) == []
    # other sessions untouched
    other_id = db.get_or_create_session(
        SessionSource(platform="serve", chat_id="c3", user_id="u", chat_type="dm"))
    db.append_message(other_id, "user", "keep")
    db.truncate_messages_from(session_id, first["id"])
    assert len(db.load_messages_with_id(other_id)) == 1
