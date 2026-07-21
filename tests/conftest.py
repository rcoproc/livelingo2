"""Shared pytest fixtures for LiveLingo unit + integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def mock_cfg():
    """Minimal config for Translator / LLM / Groq STT / cache (no real .env)."""
    return SimpleNamespace(
        SOURCE_LANG="en",
        TARGET_LANG="pt",
        GROQ_API_KEY="gsk_test_fake_key",
        GROQ_MODEL="llama-3.1-8b-instant",
        GROQ_STT_MODEL="whisper-large-v3",
        LLM_TIMEOUT=30.0,
        GROQ_STT_TIMEOUT=30.0,
        SAMPLE_RATE=16000,
        STT_INITIAL_PROMPT="",
        LOW_LATENCY=False,
        STT_HALLUCINATION_FILTER=True,
        STT_MIN_RMS=0.010,
        STT_LOW_ENERGY_MAX_WORDS=6,
        STT_LOW_ENERGY_MAX_SEC=2.5,
        PHRASE_CACHE=True,
        PHRASE_CACHE_SIZE=100,
        PHRASE_CACHE_LOG=False,
        LIVE_CAPTIONS_INVERT_LANGS=True,
        LIVE_CAPTIONS_SOURCE_LANG="",
        LIVE_CAPTIONS_TARGET_LANG="",
        TRANSLATION_ENGINE="google",
        VERBOSE=False,
    )


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite DB for db / phrase_cache integration tests."""
    from livelingo import db

    path = tmp_path / "livelingo_test.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    return path
