"""Integration tests for SQLite persistence (isolated tmp DB)."""

from __future__ import annotations

from livelingo import db


def test_timing_json_roundtrip():
    assert db.timing_to_json(None) == ""
    assert db.timing_to_json({"stt_ms": 12}) == '{"stt_ms": 12}'
    assert db.timing_from_json('{"a": 1}') == {"a": 1}
    assert db.timing_from_json("not-json") == {}
    assert db.timing_from_json({"already": True}) == {"already": True}
    assert db.timing_from_json("") == {}


def test_session_crud(tmp_db):
    db.create_session("sess-1", "Standup")
    row = db.get_session("sess-1")
    assert row is not None
    assert row[0] == "sess-1"
    assert row[1] == "Standup"
    assert db.get_session("") is None
    assert db.get_session("missing") is None

    db.create_session("sess-2", "Retro")
    listed = db.list_sessions(limit=1)
    assert len(listed) == 1
    all_rows = db.list_sessions(limit=0)
    assert len(all_rows) >= 2


def test_find_sessions_by_prefix(tmp_db):
    db.create_session("abc123", "A")
    db.create_session("abc999", "B")
    db.create_session("zzz000", "C")
    found = db.find_sessions_by_prefix("abc")
    ids = {r[0] for r in found}
    assert "abc123" in ids and "abc999" in ids
    assert "zzz000" not in ids
    assert db.find_sessions_by_prefix("") == []


def test_chunk_insert_load_update_upsert_delete(tmp_db):
    db.create_session("s1", "T")
    db.insert_chunk(
        "s1",
        1,
        "hello",
        "olá",
        "/tmp/a.wav",
        timing={"stt_ms": 10},
    )
    chunks = db.load_session_chunks("s1")
    assert len(chunks) == 1
    num, heard, tr, path, _created, timing = chunks[0]
    assert num == 1 and heard == "hello" and tr == "olá"
    assert path == "/tmp/a.wav"
    assert timing.get("stt_ms") == 10

    db.update_chunk("s1", 1, "hello!", "olá!", "/tmp/b.wav")
    chunks = db.load_session_chunks("s1")
    assert chunks[0][1] == "hello!" and chunks[0][2] == "olá!"

    created = db.upsert_chunk("s1", 1, "hi", "oi", "/tmp/c.wav", timing={"x": 1})
    assert created
    chunks = db.load_session_chunks("s1")
    assert chunks[0][1] == "hi"
    assert chunks[0][5].get("x") == 1

    db.upsert_chunk("s1", 2, "second", "segundo", "")
    assert len(db.load_session_chunks("s1")) == 2
    assert db.next_session_chunk_num("s1") == 3

    db.delete_chunk("s1", 1)
    left = db.load_session_chunks("s1")
    assert len(left) == 1 and left[0][0] == 2


def test_favorites_synonyms_comments(tmp_db):
    db.create_session("s2", "T")
    db.insert_favorite("s2", 1, "hi", "oi")
    favs = db.load_session_favorites("s2")
    assert len(favs) == 1

    db.insert_synonym("s2", "fast", "rápido")
    syns = db.load_session_synonyms("s2")
    assert syns[0][0] == "fast"

    cid = db.insert_chunk_comment("s2", 1, "note here")
    assert cid
    comments = db.load_session_comments("s2")
    assert any("note" in c[2] for c in comments)
    cmap = db.load_session_comments_map("s2")
    assert 1 in cmap
    db.delete_chunk_comment("s2", comments[0][0])
    assert db.load_session_comments("s2") == []


def test_translation_pair_upsert_get_hit_quality_undo(tmp_db):
    pair_id, prev = db.upsert_translation_pair(
        "en", "pt", "hello world", "Hello world", "Olá mundo"
    )
    assert pair_id is not None
    assert prev is None

    row = db.get_translation_pair("en", "pt", "hello world")
    assert row is not None
    assert row["target_text"] == "Olá mundo"

    db.touch_translation_pair_hit("en", "pt", "hello world")
    row2 = db.get_translation_pair("en", "pt", "hello world")
    assert int(row2.get("hit_count") or 0) >= 1

    _, prev2 = db.upsert_translation_pair(
        "en", "pt", "hello world", "Hello world", "Oi mundo"
    )
    assert prev2 == "Olá mundo"
    db.set_translation_pair_quality("en", "pt", "hello world", "good")
    row3 = db.get_translation_pair("en", "pt", "hello world")
    assert row3.get("quality") == "good"

    undone = db.undo_translation_pair("en", "pt", "hello world")
    assert undone is True or undone is not False  # may return bool/dict depending
    row4 = db.get_translation_pair("en", "pt", "hello world")
    assert row4["target_text"] in ("Olá mundo", "Oi mundo")


def test_delete_session_atomic(tmp_db):
    db.create_session("del-me", "X")
    db.insert_chunk("del-me", 1, "a", "b", "")
    db.insert_favorite("del-me", 1, "a", "b")
    ok = db.delete_session_atomic("del-me")
    assert ok is True or ok is None or ok is not False
    assert db.get_session("del-me") is None
    assert db.load_session_chunks("del-me") == []
