"""
db.py
=====
Database manager for LiveLingo session and chunk persistence.
Uses sqlite3 (standard library) to store session logs and audio file paths.
"""

import datetime
import json
import os
import shutil
import sqlite3

DB_PATH = "livelingo.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_column(cursor, table, column, col_def):
    cursor.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cursor.fetchall()}
    if column not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def timing_to_json(timing):
    if not timing:
        return ""
    try:
        return json.dumps(timing, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def timing_from_json(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def is_livecaptions_timing(timing) -> bool:
    """True when chunk timing marks Windows LiveCaptions (same rule as list `l`)."""
    if not isinstance(timing, dict):
        return False
    src = (timing.get("source") or timing.get("origin") or "").strip().lower()
    return src in ("livecaptions", "lc", "captions")


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        chunk_num INTEGER NOT NULL,
        heard_text TEXT NOT NULL,
        translated_text TEXT NOT NULL,
        audio_path TEXT NOT NULL,
        created_at TEXT,
        timing_json TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    # Migrate older DBs that predate performance columns.
    _ensure_column(cursor, "chunks", "created_at", "TEXT")
    _ensure_column(cursor, "chunks", "timing_json", "TEXT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS synonyms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        word TEXT NOT NULL,
        explanation TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        chunk_num INTEGER NOT NULL,
        heard_text TEXT NOT NULL,
        translated_text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chunk_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        chunk_num INTEGER NOT NULL,
        comment_text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_comments_session
        ON chunk_comments(session_id, chunk_num, id)
        """
    )

    # Full-sentence translation memory (cross-session phrase cache).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS translation_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_lang TEXT NOT NULL,
        target_lang TEXT NOT NULL,
        source_norm TEXT NOT NULL,
        source_text TEXT NOT NULL,
        target_text TEXT NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 1,
        quality TEXT,
        created_at TEXT NOT NULL,
        last_used_at TEXT NOT NULL,
        UNIQUE(source_lang, target_lang, source_norm)
    )
    """)
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_pairs_lang
        ON translation_pairs(source_lang, target_lang, hit_count DESC)
        """
    )
    # Previous target texts when a pair is overwritten (undo / review).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS translation_pairs_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair_id INTEGER,
        source_lang TEXT NOT NULL,
        target_lang TEXT NOT NULL,
        source_norm TEXT NOT NULL,
        source_text TEXT NOT NULL,
        old_target_text TEXT NOT NULL,
        new_target_text TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """)
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_pairs_history_norm
        ON translation_pairs_history(source_lang, target_lang, source_norm, id DESC)
        """
    )

    # Interview Coach answers (EN + pt-BR fields per response).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS coach_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        coach_num INTEGER NOT NULL,
        question TEXT NOT NULL,
        spoken_en TEXT NOT NULL DEFAULT '',
        software_engineer_en TEXT NOT NULL DEFAULT '[]',
        architect_en TEXT NOT NULL DEFAULT '[]',
        tradeoffs_en TEXT NOT NULL DEFAULT '',
        spoken_pt TEXT NOT NULL DEFAULT '',
        software_engineer_pt TEXT NOT NULL DEFAULT '[]',
        architect_pt TEXT NOT NULL DEFAULT '[]',
        tradeoffs_pt TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_coach_results_session
        ON coach_results(session_id, coach_num, id)
        """
    )

    conn.commit()
    conn.close()


def create_session(session_id, title):
    conn = get_connection()
    cursor = conn.cursor()
    created_at = _now()
    cursor.execute(
        "INSERT INTO sessions (id, title, created_at) VALUES (?, ?, ?)",
        (session_id, title, created_at),
    )
    conn.commit()
    conn.close()


def list_sessions(limit=5):
    """
    Return list of (id, title, created_at) ordered by created_at DESC.

    limit: max rows (default 5). None or <= 0 → all sessions.
    """
    conn = get_connection()
    cursor = conn.cursor()
    if limit is None or int(limit) <= 0:
        cursor.execute(
            "SELECT id, title, created_at FROM sessions ORDER BY created_at DESC"
        )
    else:
        cursor.execute(
            "SELECT id, title, created_at FROM sessions "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_session(session_id):
    """
    Return (id, title, created_at) for an exact session id, or None.
    """
    if not session_id:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, created_at FROM sessions WHERE id = ?",
        (str(session_id).strip(),),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def find_sessions_by_prefix(prefix, limit=20):
    """
    Return list of (id, title, created_at) whose id starts with prefix
    (case-sensitive, SQLite LIKE). Empty prefix → [].
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return []
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, title, created_at FROM sessions
        WHERE id = ? OR id LIKE ? ESCAPE '\\'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (
            prefix,
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
            int(limit),
        ),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def list_session_stats():
    """
    All sessions with LC / VOZ / Coach counts (newest first).

    Returns list of dicts:
      id, title, created_at, lc, voz, coach, total
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, created_at FROM sessions ORDER BY created_at DESC"
    )
    sessions = cursor.fetchall()
    cursor.execute("SELECT session_id, timing_json FROM chunks")
    chunk_rows = cursor.fetchall()
    cursor.execute(
        """
        SELECT session_id, COUNT(*) FROM coach_results
        GROUP BY session_id
        """
    )
    coach_counts = {str(sid): int(n) for sid, n in cursor.fetchall()}
    conn.close()

    lc_map = {}
    voz_map = {}
    for sid, timing_json in chunk_rows:
        sid = str(sid)
        timing = timing_from_json(timing_json)
        if is_livecaptions_timing(timing):
            lc_map[sid] = lc_map.get(sid, 0) + 1
        else:
            voz_map[sid] = voz_map.get(sid, 0) + 1

    out = []
    for sid, title, created_at in sessions:
        sid = str(sid)
        lc = int(lc_map.get(sid, 0))
        voz = int(voz_map.get(sid, 0))
        coach = int(coach_counts.get(sid, 0))
        out.append(
            {
                "id": sid,
                "title": title or "",
                "created_at": created_at or "",
                "lc": lc,
                "voz": voz,
                "coach": coach,
                "total": lc + voz + coach,
            }
        )
    return out


def database_file_size_bytes():
    """Size of livelingo.db in bytes, or 0 if missing."""
    try:
        if os.path.isfile(DB_PATH):
            return int(os.path.getsize(DB_PATH))
    except OSError:
        pass
    return 0


def format_byte_size(n):
    """Human-readable size for session-info footer."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


# Fixed-width columns for `session-info` (name, width, align).
# CREATED_AT first (session clock) — no repeated report-time prefix on rows.
# ID ≈ `YYYYMMDD_HHMMSS_session-YYYY-MM-DD-HHMM` (~44 chars).
SESSION_INFO_COLS = (
    ("CREATED_AT", 19, "<"),
    ("ID", 44, "<"),
    ("TITLE", 24, "<"),
    ("LC", 5, ">"),
    ("VOZ", 5, ">"),
    ("COACH", 5, ">"),
    ("TOTAL", 6, ">"),
)


def _session_info_cell(text, width: int, align: str = "<") -> str:
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > width:
        if width <= 1:
            s = s[:width]
        else:
            s = s[: max(1, width - 1)] + "…"
    if align == ">":
        return s.rjust(width)
    return s.ljust(width)


def _session_info_join(values) -> str:
    parts = []
    for (_name, width, align), val in zip(SESSION_INFO_COLS, values):
        parts.append(_session_info_cell(val, width, align))
    return "  ".join(parts)


def format_session_info_header() -> str:
    return _session_info_join(name for name, _w, _a in SESSION_INFO_COLS)


def format_session_info_rule() -> str:
    return "  ".join("-" * width for _name, width, _align in SESSION_INFO_COLS)


def format_session_info_row(row: dict) -> str:
    """One data row for session-info table."""
    title = " ".join((row.get("title") or "").split()) or "(sem título)"
    created = (row.get("created_at") or "").strip().replace("T", " ")
    if len(created) >= 19:
        created = created[:19]
    if not created:
        created = "—"
    return _session_info_join(
        (
            created,
            row.get("id") or "",
            title,
            int(row.get("lc") or 0),
            int(row.get("voz") or 0),
            int(row.get("coach") or 0),
            int(row.get("total") or 0),
        )
    )


def format_session_info_totals(sum_lc, sum_voz, sum_coach, sum_all) -> str:
    return _session_info_join(("", "TOTAL", "", sum_lc, sum_voz, sum_coach, sum_all))


def insert_chunk(
    session_id,
    chunk_num,
    heard_text,
    translated_text,
    audio_path,
    timing=None,
    created_at=None,
):
    conn = get_connection()
    cursor = conn.cursor()
    created_at = created_at or _now()
    cursor.execute(
        """
    INSERT INTO chunks (
        session_id, chunk_num, heard_text, translated_text, audio_path,
        created_at, timing_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            session_id,
            chunk_num,
            heard_text,
            translated_text,
            audio_path or "",
            created_at,
            timing_to_json(timing),
        ),
    )
    conn.commit()
    conn.close()
    return created_at


def load_session_chunks(session_id):
    """
    Return list of
    (chunk_num, heard_text, translated_text, audio_path, created_at, timing_dict).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT chunk_num, heard_text, translated_text, audio_path,
           created_at, timing_json
    FROM chunks
    WHERE session_id = ?
    ORDER BY chunk_num ASC
    """,
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    out = []
    for chunk_num, heard, translated, audio_path, created_at, timing_json in rows:
        out.append(
            (
                chunk_num,
                heard,
                translated,
                audio_path or "",
                created_at or "",
                timing_from_json(timing_json),
            )
        )
    return out


def update_chunk(
    session_id,
    chunk_num,
    heard_text,
    translated_text,
    audio_path,
    timing=None,
    update_timing=False,
):
    """
    Update chunk text/audio. If update_timing is True, also overwrite timing_json
    (pass timing=None to clear). If False, leave timing_json unchanged.
    """
    conn = get_connection()
    cursor = conn.cursor()
    if update_timing:
        cursor.execute(
            """
        UPDATE chunks
        SET heard_text = ?, translated_text = ?, audio_path = ?, timing_json = ?
        WHERE session_id = ? AND chunk_num = ?
        """,
            (
                heard_text,
                translated_text,
                audio_path or "",
                timing_to_json(timing),
                session_id,
                chunk_num,
            ),
        )
    else:
        cursor.execute(
            """
        UPDATE chunks
        SET heard_text = ?, translated_text = ?, audio_path = ?
        WHERE session_id = ? AND chunk_num = ?
        """,
            (
                heard_text,
                translated_text,
                audio_path or "",
                session_id,
                chunk_num,
            ),
        )
    conn.commit()
    conn.close()


def upsert_chunk(
    session_id,
    chunk_num,
    heard_text,
    translated_text,
    audio_path,
    timing=None,
    created_at=None,
):
    """
    Insert chunk or update if (session_id, chunk_num) already exists.
    Returns created_at (existing stamp kept on update).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT created_at FROM chunks
        WHERE session_id = ? AND chunk_num = ?
        """,
        (session_id, chunk_num),
    )
    row = cursor.fetchone()
    if row:
        kept_created = row[0] or created_at or _now()
        cursor.execute(
            """
            UPDATE chunks
            SET heard_text = ?, translated_text = ?, audio_path = ?, timing_json = ?
            WHERE session_id = ? AND chunk_num = ?
            """,
            (
                heard_text,
                translated_text,
                audio_path or "",
                timing_to_json(timing),
                session_id,
                chunk_num,
            ),
        )
        conn.commit()
        conn.close()
        return kept_created

    created_at = created_at or _now()
    cursor.execute(
        """
        INSERT INTO chunks (
            session_id, chunk_num, heard_text, translated_text, audio_path,
            created_at, timing_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            chunk_num,
            heard_text,
            translated_text,
            audio_path or "",
            created_at,
            timing_to_json(timing),
        ),
    )
    conn.commit()
    conn.close()
    return created_at


def delete_chunk(session_id, chunk_num):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM chunks WHERE session_id = ? AND chunk_num = ?",
        (session_id, chunk_num),
    )
    cursor.execute(
        "DELETE FROM favorites WHERE session_id = ? AND chunk_num = ?",
        (session_id, chunk_num),
    )
    cursor.execute(
        "DELETE FROM chunk_comments WHERE session_id = ? AND chunk_num = ?",
        (session_id, chunk_num),
    )
    conn.commit()
    conn.close()


def insert_synonym(session_id, word, explanation):
    conn = get_connection()
    cursor = conn.cursor()
    created_at = _now()
    cursor.execute(
        """
    INSERT INTO synonyms (session_id, word, explanation, created_at)
    VALUES (?, ?, ?, ?)
    """,
        (session_id, word, explanation, created_at),
    )
    conn.commit()
    conn.close()


def load_session_synonyms(session_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT word, explanation
    FROM synonyms
    WHERE session_id = ?
    ORDER BY id ASC
    """,
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def insert_favorite(session_id, chunk_num, heard_text, translated_text):
    conn = get_connection()
    cursor = conn.cursor()
    created_at = _now()
    cursor.execute(
        """
    INSERT INTO favorites (session_id, chunk_num, heard_text, translated_text, created_at)
    VALUES (?, ?, ?, ?, ?)
    """,
        (session_id, chunk_num, heard_text, translated_text, created_at),
    )
    conn.commit()
    conn.close()


def load_session_favorites(session_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT chunk_num, heard_text, translated_text
    FROM favorites
    WHERE session_id = ?
    ORDER BY id ASC
    """,
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def insert_chunk_comment(session_id, chunk_num, comment_text):
    """
    Insert one free-text comment for a chunk.
    Returns (id, created_at).
    """
    conn = get_connection()
    cursor = conn.cursor()
    created_at = _now()
    cursor.execute(
        """
    INSERT INTO chunk_comments (session_id, chunk_num, comment_text, created_at)
    VALUES (?, ?, ?, ?)
    """,
        (session_id, int(chunk_num), (comment_text or "").strip(), created_at),
    )
    comment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return int(comment_id), created_at


def load_session_comments(session_id):
    """
    Return list of (id, chunk_num, comment_text, created_at) for a session,
    ordered by id (chronological per insert).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT id, chunk_num, comment_text, created_at
    FROM chunk_comments
    WHERE session_id = ?
    ORDER BY id ASC
    """,
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def load_session_comments_map(session_id):
    """
    chunk_num → list of (id, comment_text, created_at) for list/export helpers.
    """
    out = {}
    for cid, chunk_num, text, created_at in load_session_comments(session_id):
        out.setdefault(int(chunk_num), []).append(
            (int(cid), text or "", created_at or "")
        )
    return out


def delete_chunk_comment(session_id, comment_id):
    """
    Delete one comment by primary key id, scoped to session.
    Returns (chunk_num, comment_text) of deleted row, or None if not found.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT chunk_num, comment_text FROM chunk_comments
        WHERE id = ? AND session_id = ?
        """,
        (int(comment_id), session_id),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    cursor.execute(
        "DELETE FROM chunk_comments WHERE id = ? AND session_id = ?",
        (int(comment_id), session_id),
    )
    conn.commit()
    conn.close()
    return int(row[0]), row[1] or ""


def _coach_list_to_json(items) -> str:
    if not items:
        return "[]"
    try:
        cleaned = [str(x).strip() for x in items if str(x).strip()]
        return json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def _coach_list_from_json(raw) -> list:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return []


def insert_coach_result(
    session_id,
    coach_num,
    question,
    *,
    spoken_en="",
    software_engineer_en=None,
    architect_en=None,
    tradeoffs_en="",
    spoken_pt="",
    software_engineer_pt=None,
    architect_pt=None,
    tradeoffs_pt="",
    provider="",
    error="",
    created_at=None,
):
    """
    Persist one Interview Coach answer (EN + pt-BR fields).
    Returns created_at string.
    """
    conn = get_connection()
    cursor = conn.cursor()
    created_at = created_at or _now()
    cursor.execute(
        """
    INSERT INTO coach_results (
        session_id, coach_num, question,
        spoken_en, software_engineer_en, architect_en, tradeoffs_en,
        spoken_pt, software_engineer_pt, architect_pt, tradeoffs_pt,
        provider, error, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            session_id,
            int(coach_num),
            (question or "").strip(),
            (spoken_en or "").strip(),
            _coach_list_to_json(software_engineer_en),
            _coach_list_to_json(architect_en),
            (tradeoffs_en or "").strip(),
            (spoken_pt or "").strip(),
            _coach_list_to_json(software_engineer_pt),
            _coach_list_to_json(architect_pt),
            (tradeoffs_pt or "").strip(),
            (provider or "").strip(),
            (error or "").strip(),
            created_at,
        ),
    )
    conn.commit()
    conn.close()
    return created_at


def update_coach_result_pt(
    session_id,
    coach_num,
    *,
    spoken_pt="",
    software_engineer_pt=None,
    architect_pt=None,
    tradeoffs_pt="",
):
    """
    Fill pt-BR fields on the latest coach_results row for ``coach_num``.

    Used when EN is persisted first and pt-BR arrives from a background LLM call.
    Returns True if a row was updated.
    """
    if not session_id:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT id FROM coach_results
    WHERE session_id = ? AND coach_num = ?
    ORDER BY id DESC LIMIT 1
    """,
        (session_id, int(coach_num)),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    row_id = row[0]
    cursor.execute(
        """
    UPDATE coach_results SET
        spoken_pt = ?,
        software_engineer_pt = ?,
        architect_pt = ?,
        tradeoffs_pt = ?
    WHERE id = ?
    """,
        (
            (spoken_pt or "").strip(),
            _coach_list_to_json(software_engineer_pt),
            _coach_list_to_json(architect_pt),
            (tradeoffs_pt or "").strip(),
            row_id,
        ),
    )
    conn.commit()
    conn.close()
    return True


def load_session_coach_results(session_id):
    """
    Return list of dicts for coach answers in a session, chronological by id.

    Keys: id, coach_num, question, spoken_en, software_engineer_en, architect_en,
    tradeoffs_en, spoken_pt, software_engineer_pt, architect_pt, tradeoffs_pt,
    provider, error, created_at.
    """
    if not session_id:
        return []
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT id, coach_num, question,
           spoken_en, software_engineer_en, architect_en, tradeoffs_en,
           spoken_pt, software_engineer_pt, architect_pt, tradeoffs_pt,
           provider, error, created_at
    FROM coach_results
    WHERE session_id = ?
    ORDER BY id ASC
    """,
        (str(session_id),),
    )
    rows = cursor.fetchall()
    conn.close()
    out = []
    for row in rows:
        (
            rid,
            coach_num,
            question,
            spoken_en,
            se_en,
            arch_en,
            trade_en,
            spoken_pt,
            se_pt,
            arch_pt,
            trade_pt,
            provider,
            error,
            created_at,
        ) = row
        out.append(
            {
                "id": int(rid),
                "coach_num": int(coach_num),
                "question": question or "",
                "spoken_en": spoken_en or "",
                "software_engineer_en": _coach_list_from_json(se_en),
                "architect_en": _coach_list_from_json(arch_en),
                "tradeoffs_en": trade_en or "",
                "spoken_pt": spoken_pt or "",
                "software_engineer_pt": _coach_list_from_json(se_pt),
                "architect_pt": _coach_list_from_json(arch_pt),
                "tradeoffs_pt": trade_pt or "",
                "provider": provider or "",
                "error": error or "",
                "created_at": created_at or "",
            }
        )
    return out


def delete_session_atomic(session_id):
    """
    Atomic deletion of a session and all its dependent chunks, synonyms,
    favorites, comments, and coach results.
    Returns True if deleted successfully, False otherwise.
    """
    conn = get_connection()
    cursor = conn.cursor()
    success = False
    try:
        cursor.execute("BEGIN TRANSACTION")

        cursor.execute("DELETE FROM coach_results WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM chunk_comments WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM favorites WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM synonyms WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

        conn.commit()
        success = True
    except Exception as exc:
        conn.rollback()
        raise exc
    finally:
        conn.close()

    if success:
        cache_dir = os.path.join(".cache", "audio_sessions", session_id)
        if os.path.exists(cache_dir):
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass

    return success


# --------------------------------------------------------------------------- #
# Translation phrase cache (cross-session TM)
# --------------------------------------------------------------------------- #


def upsert_translation_pair(
    source_lang,
    target_lang,
    source_norm,
    source_text,
    target_text,
    *,
    bump_hit=False,
    quality=None,
):
    """
    Insert or update a phrase pair.

    If target_text changes, archives old value into translation_pairs_history.
    Returns (pair_id, previous_target_or_None).
    """
    now = _now()
    source_lang = (source_lang or "").lower().strip()
    target_lang = (target_lang or "").lower().strip()
    source_norm = (source_norm or "").strip()
    source_text = (source_text or "").strip()
    target_text = (target_text or "").strip()
    if not source_lang or not target_lang or not source_norm or not target_text:
        return None, None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, target_text FROM translation_pairs
        WHERE source_lang = ? AND target_lang = ? AND source_norm = ?
        """,
        (source_lang, target_lang, source_norm),
    )
    row = cursor.fetchone()
    prev_target = None
    pair_id = None
    if row:
        pair_id = int(row[0])
        prev_target = (row[1] or "").strip() or None
        if prev_target and prev_target != target_text:
            cursor.execute(
                """
                INSERT INTO translation_pairs_history
                (pair_id, source_lang, target_lang, source_norm, source_text,
                 old_target_text, new_target_text, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair_id,
                    source_lang,
                    target_lang,
                    source_norm,
                    source_text,
                    prev_target,
                    target_text,
                    "overwrite",
                    now,
                ),
            )
        changed = prev_target is not None and prev_target != target_text
        if bump_hit and quality is not None:
            cursor.execute(
                """
                UPDATE translation_pairs SET
                    source_text = ?, target_text = ?,
                    hit_count = hit_count + 1,
                    quality = ?, last_used_at = ?
                WHERE id = ?
                """,
                (source_text, target_text, quality, now, pair_id),
            )
        elif bump_hit and changed:
            cursor.execute(
                """
                UPDATE translation_pairs SET
                    source_text = ?, target_text = ?,
                    hit_count = hit_count + 1,
                    quality = NULL, last_used_at = ?
                WHERE id = ?
                """,
                (source_text, target_text, now, pair_id),
            )
        elif bump_hit:
            cursor.execute(
                """
                UPDATE translation_pairs SET
                    source_text = ?, target_text = ?,
                    hit_count = hit_count + 1, last_used_at = ?
                WHERE id = ?
                """,
                (source_text, target_text, now, pair_id),
            )
        elif quality is not None:
            cursor.execute(
                """
                UPDATE translation_pairs SET
                    source_text = ?, target_text = ?,
                    quality = ?, last_used_at = ?
                WHERE id = ?
                """,
                (source_text, target_text, quality, now, pair_id),
            )
        elif changed:
            cursor.execute(
                """
                UPDATE translation_pairs SET
                    source_text = ?, target_text = ?,
                    quality = NULL, last_used_at = ?
                WHERE id = ?
                """,
                (source_text, target_text, now, pair_id),
            )
        else:
            cursor.execute(
                """
                UPDATE translation_pairs SET
                    source_text = ?, target_text = ?, last_used_at = ?
                WHERE id = ?
                """,
                (source_text, target_text, now, pair_id),
            )
    else:
        cursor.execute(
            """
            INSERT INTO translation_pairs
            (source_lang, target_lang, source_norm, source_text, target_text,
             hit_count, quality, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                source_lang,
                target_lang,
                source_norm,
                source_text,
                target_text,
                quality,
                now,
                now,
            ),
        )
        pair_id = int(cursor.lastrowid)
        prev_target = None
    conn.commit()
    conn.close()
    return pair_id, (
        prev_target if (prev_target and prev_target != target_text) else None
    )


def get_translation_pair(source_lang, target_lang, source_norm):
    """Return dict or None: id, source_text, target_text, hit_count, quality, ..."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, source_text, target_text, hit_count, quality,
               created_at, last_used_at
        FROM translation_pairs
        WHERE source_lang = ? AND target_lang = ? AND source_norm = ?
        """,
        (
            (source_lang or "").lower().strip(),
            (target_lang or "").lower().strip(),
            (source_norm or "").strip(),
        ),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "source_text": row[1] or "",
        "target_text": row[2] or "",
        "hit_count": int(row[3] or 0),
        "quality": row[4],
        "created_at": row[5] or "",
        "last_used_at": row[6] or "",
    }


def touch_translation_pair_hit(source_lang, target_lang, source_norm):
    """Increment hit_count + last_used for a cache HIT."""
    now = _now()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE translation_pairs SET
            hit_count = hit_count + 1,
            last_used_at = ?
        WHERE source_lang = ? AND target_lang = ? AND source_norm = ?
        """,
        (
            now,
            (source_lang or "").lower().strip(),
            (target_lang or "").lower().strip(),
            (source_norm or "").strip(),
        ),
    )
    conn.commit()
    conn.close()


def set_translation_pair_quality(source_lang, target_lang, source_norm, quality):
    """quality: 'good' | 'bad' | None."""
    now = _now()
    q = (quality or "").strip().lower() or None
    if q not in (None, "good", "bad"):
        q = None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE translation_pairs SET quality = ?, last_used_at = ?
        WHERE source_lang = ? AND target_lang = ? AND source_norm = ?
        """,
        (
            q,
            now,
            (source_lang or "").lower().strip(),
            (target_lang or "").lower().strip(),
            (source_norm or "").strip(),
        ),
    )
    n = cursor.rowcount
    conn.commit()
    conn.close()
    return n > 0


def undo_translation_pair(source_lang, target_lang, source_norm):
    """
    Restore the most recent history.old_target_text for this pair.
    Returns restored target text or None.
    """
    src_l = (source_lang or "").lower().strip()
    tgt_l = (target_lang or "").lower().strip()
    norm = (source_norm or "").strip()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, old_target_text, new_target_text, source_text
        FROM translation_pairs_history
        WHERE source_lang = ? AND target_lang = ? AND source_norm = ?
        ORDER BY id DESC LIMIT 1
        """,
        (src_l, tgt_l, norm),
    )
    hist = cursor.fetchone()
    if not hist:
        conn.close()
        return None
    hist_id, old_tgt, new_tgt, src_txt = hist
    old_tgt = (old_tgt or "").strip()
    if not old_tgt:
        conn.close()
        return None
    now = _now()
    cursor.execute(
        """
        SELECT id, target_text FROM translation_pairs
        WHERE source_lang = ? AND target_lang = ? AND source_norm = ?
        """,
        (src_l, tgt_l, norm),
    )
    pair = cursor.fetchone()
    if not pair:
        conn.close()
        return None
    pair_id, cur_tgt = int(pair[0]), (pair[1] or "").strip()
    # Archive the undo as well
    cursor.execute(
        """
        INSERT INTO translation_pairs_history
        (pair_id, source_lang, target_lang, source_norm, source_text,
         old_target_text, new_target_text, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair_id,
            src_l,
            tgt_l,
            norm,
            src_txt or "",
            cur_tgt,
            old_tgt,
            "undo",
            now,
        ),
    )
    cursor.execute(
        """
        UPDATE translation_pairs SET
            target_text = ?, quality = NULL, last_used_at = ?
        WHERE id = ?
        """,
        (old_tgt, now, pair_id),
    )
    conn.commit()
    conn.close()
    return old_tgt


def list_translation_pairs(source_lang=None, target_lang=None, limit=5000):
    """Return list of pair dicts ordered by hit_count DESC."""
    conn = get_connection()
    cursor = conn.cursor()
    lim = max(1, int(limit or 5000))
    if source_lang and target_lang:
        cursor.execute(
            """
            SELECT id, source_lang, target_lang, source_norm, source_text,
                   target_text, hit_count, quality, created_at, last_used_at
            FROM translation_pairs
            WHERE source_lang = ? AND target_lang = ?
            ORDER BY hit_count DESC, last_used_at DESC
            LIMIT ?
            """,
            (
                source_lang.lower().strip(),
                target_lang.lower().strip(),
                lim,
            ),
        )
    else:
        cursor.execute(
            """
            SELECT id, source_lang, target_lang, source_norm, source_text,
                   target_text, hit_count, quality, created_at, last_used_at
            FROM translation_pairs
            ORDER BY hit_count DESC, last_used_at DESC
            LIMIT ?
            """,
            (lim,),
        )
    rows = cursor.fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "source_lang": r[1] or "",
                "target_lang": r[2] or "",
                "source_norm": r[3] or "",
                "source_text": r[4] or "",
                "target_text": r[5] or "",
                "hit_count": int(r[6] or 0),
                "quality": r[7],
                "created_at": r[8] or "",
                "last_used_at": r[9] or "",
            }
        )
    return out


def next_session_chunk_num(session_id) -> int:
    """Next free chunk_num for session (MAX+1, or 1)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COALESCE(MAX(chunk_num), 0) FROM chunks
        WHERE session_id = ?
        """,
        (session_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return int(row[0] or 0) + 1


def load_chunks_for_warmup(limit=5000, origin=None):
    """
    Chunks (newest first) for phrase-cache warm-up.
    Returns list of (heard_text, translated_text).

    origin:
      None            — all chunks (legacy)
      "livecaptions"  — only timing_json source=livecaptions
      "voice"         — exclude livecaptions (mic pipeline)
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT heard_text, translated_text, timing_json FROM chunks
        WHERE heard_text IS NOT NULL AND translated_text IS NOT NULL
          AND TRIM(heard_text) != '' AND TRIM(translated_text) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, int(limit or 5000) * 3 if origin else int(limit or 5000)),),
    )
    rows = cursor.fetchall()
    conn.close()
    want = (origin or "").strip().lower() or None
    out = []
    for heard, translated, timing_json in rows:
        src_tag = _chunk_origin_from_timing(timing_json)
        if want == "livecaptions" and src_tag != "livecaptions":
            continue
        if want == "voice" and src_tag == "livecaptions":
            continue
        out.append((heard or "", translated or ""))
        if len(out) >= max(1, int(limit or 5000)):
            break
    return out


def _chunk_origin_from_timing(timing_json) -> str:
    """Return 'livecaptions' or 'voice' from chunks.timing_json."""
    if not timing_json:
        return "voice"
    data = timing_from_json(timing_json)
    if not isinstance(data, dict):
        return "voice"
    src = (data.get("source") or data.get("origin") or "").strip().lower()
    if src in ("livecaptions", "lc", "captions"):
        return "livecaptions"
    return "voice"


def translation_pairs_inventory(limit=50000):
    """
    Inventory of phrase-cache pairs for UI summary.

    Returns list of dicts:
      source_lang, target_lang, source_text, target_text, hit_count, quality
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT source_lang, target_lang, source_text, target_text,
                   hit_count, quality
            FROM translation_pairs
            ORDER BY hit_count DESC
            LIMIT ?
            """,
            (max(1, int(limit or 50000)),),
        )
        rows = cursor.fetchall()
    except Exception:
        rows = []
    conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "source_lang": (r[0] or "").lower().strip(),
                "target_lang": (r[1] or "").lower().strip(),
                "source_text": r[2] or "",
                "target_text": r[3] or "",
                "hit_count": int(r[4] or 0),
                "quality": r[5],
            }
        )
    return out
