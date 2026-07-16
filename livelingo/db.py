"""
db.py
=====
Database manager for LiveLingo session and chunk persistence.
Uses sqlite3 (standard library) to store session logs and audio file paths.
"""

import json
import sqlite3
import os
import datetime
import shutil

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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, created_at FROM sessions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


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


def delete_session_atomic(session_id):
    """
    Atomic deletion of a session and all its dependent chunks, synonyms, and favorites.
    Returns True if deleted successfully, False otherwise.
    """
    conn = get_connection()
    cursor = conn.cursor()
    success = False
    try:
        cursor.execute("BEGIN TRANSACTION")

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
