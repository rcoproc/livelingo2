"""Shared pytest fixtures for LiveLingo unit tests."""

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
    """Minimal config object for Translator / LLM / Groq STT (no real .env)."""
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
    )
