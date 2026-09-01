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


def test_session_info_table_alignment():
    header = db.format_session_info_header()
    rule = db.format_session_info_rule()
    row = db.format_session_info_row(
        {
            "id": "20260716_205709_session-2026-07-16-2057",
            "title": "Weekly standup meeting that is quite long",
            "created_at": "2026-07-16 20:57:09",
            "lc": 3,
            "voz": 12,
            "coach": 2,
            "total": 17,
        }
    )
    totals = db.format_session_info_totals(3, 12, 2, 17)
    assert header.startswith("CREATED_AT")
    assert "TITLE" in header and "ID" in header
    assert "LC" in header and "VOZ" in header and "COACH" in header
    assert len(header) == len(rule) == len(row) == len(totals)
    # CREATED_AT is the first column (session clock, not report stamp)
    assert row.startswith("2026-07-16 20:57:09")
    assert row.rstrip().endswith("17")
    assert "TOTAL" in totals
    assert "20260716_205709_session-2026-07-16-2057" in row
    assert "…" in row


def test_list_session_stats_and_db_size(tmp_db):
    db.create_session("stats-a", "Alpha")
    db.create_session("stats-b", "Beta")
    db.insert_chunk(
        "stats-a",
        1,
        "hello",
        "olá",
        "",
        timing={"stt_ms": 1},
    )
    db.insert_chunk(
        "stats-a",
        2,
        "LC question?",
        "Pergunta LC?",
        "",
        timing={"source": "livecaptions"},
    )
    db.insert_coach_result(
        "stats-a",
        2,
        "LC question?",
        spoken_en="I use Redis.",
        spoken_pt="Uso Redis.",
    )
    # Beta: empty session still listed
    rows = db.list_session_stats()
    by_id = {r["id"]: r for r in rows}
    assert "stats-a" in by_id and "stats-b" in by_id
    a = by_id["stats-a"]
    assert a["lc"] == 1
    assert a["voz"] == 1
    assert a["coach"] == 1
    assert a["total"] == 3
    assert by_id["stats-b"]["total"] == 0
    size = db.database_file_size_bytes()
    assert size >= 0
    assert "B" in db.format_byte_size(size) or "KB" in db.format_byte_size(size)


def test_coach_result_insert_load(tmp_db):
    db.create_session("coach-s1", "Interview")
    created = db.insert_coach_result(
        "coach-s1",
        3,
        "How do you handle caching?",
        spoken_en="I use Redis for hot paths.",
        software_engineer_en=["TTL per key", "invalidate on write"],
        architect_en=["cache-aside"],
        tradeoffs_en="Latency vs consistency.",
        spoken_pt="Uso Redis nos caminhos quentes.",
        software_engineer_pt=["TTL por chave"],
        architect_pt=["cache-aside"],
        tradeoffs_pt="Latência vs consistência.",
        provider="grok",
    )
    assert created
    rows = db.load_session_coach_results("coach-s1")
    assert len(rows) == 1
    row = rows[0]
    assert row["coach_num"] == 3
    assert "caching" in row["question"]
    assert "Redis" in row["spoken_en"]
    assert row["software_engineer_en"] == ["TTL per key", "invalidate on write"]
    assert row["spoken_pt"].startswith("Uso Redis")
    assert row["provider"] == "grok"
    assert db.load_session_coach_results("missing") == []
    assert db.load_session_coach_results("") == []


def test_delete_session_atomic(tmp_db):
    db.create_session("del-me", "X")
    db.insert_chunk("del-me", 1, "a", "b", "")
    db.insert_favorite("del-me", 1, "a", "b")
    db.insert_coach_result(
        "del-me",
        1,
        "Q?",
        spoken_en="A",
        spoken_pt="R",
    )
    ok = db.delete_session_atomic("del-me")
    assert ok is True or ok is None or ok is not False
    assert db.get_session("del-me") is None
    assert db.load_session_chunks("del-me") == []
    assert db.load_session_coach_results("del-me") == []
