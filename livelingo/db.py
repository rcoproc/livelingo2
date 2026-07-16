"""
db.py
=====
Database manager for LiveLingo session and chunk persistence.
Uses sqlite3 (standard library) to store session logs and audio file paths.
"""

import sqlite3
import os
import datetime
import shutil

DB_PATH = "livelingo.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Create sessions table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # Create chunks table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        chunk_num INTEGER NOT NULL,
        heard_text TEXT NOT NULL,
        translated_text TEXT NOT NULL,
        audio_path TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    # Create synonyms table
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

    # Create favorites table
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
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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


def insert_chunk(session_id, chunk_num, heard_text, translated_text, audio_path):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    INSERT INTO chunks (session_id, chunk_num, heard_text, translated_text, audio_path)
    VALUES (?, ?, ?, ?, ?)
    """,
        (session_id, chunk_num, heard_text, translated_text, audio_path),
    )
    conn.commit()
    conn.close()


def load_session_chunks(session_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT chunk_num, heard_text, translated_text, audio_path
    FROM chunks
    WHERE session_id = ?
    ORDER BY chunk_num ASC
    """,
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_chunk(session_id, chunk_num, heard_text, translated_text, audio_path):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    UPDATE chunks
    SET heard_text = ?, translated_text = ?, audio_path = ?
    WHERE session_id = ? AND chunk_num = ?
    """,
        (heard_text, translated_text, audio_path, session_id, chunk_num),
    )
    conn.commit()
    conn.close()


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
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

        # 1. Delete dependent favorites
        cursor.execute("DELETE FROM favorites WHERE session_id = ?", (session_id,))
        # 2. Delete dependent synonyms
        cursor.execute("DELETE FROM synonyms WHERE session_id = ?", (session_id,))
        # 3. Delete dependent chunks
        cursor.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
        # 4. Delete the session itself
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

        conn.commit()
        success = True
    except Exception as exc:
        conn.rollback()
        raise exc
    finally:
        conn.close()

    # 5. Clean up physical audio cache files if DB transaction succeeded
    if success:
        cache_dir = os.path.join(".cache", "audio_sessions", session_id)
        if os.path.exists(cache_dir):
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass

    return success
