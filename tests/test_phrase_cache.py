"""Unit + integration tests for phrase translation memory."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from livelingo.phrase_cache import PhraseCache, normalize_phrase


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("  Hello, World!  ", "hello world"),
        ("Café...", "café"),
        ("A  B\tC", "a b c"),
        ("UPPER", "upper"),
    ],
)
def test_normalize_phrase(raw, expected):
    assert normalize_phrase(raw) == expected


def test_cache_disabled_returns_none_on_lookup_store(mock_cfg):
    mock_cfg.PHRASE_CACHE = False
    cache = PhraseCache(mock_cfg)
    assert cache.lookup("en", "pt", "hello") is None
    assert cache.store("en", "pt", "hello", "olá") is None


def test_cache_memory_hit_miss_store(tmp_db, mock_cfg):
    cache = PhraseCache(mock_cfg)
    assert cache.enabled is True
    assert cache.lookup("en", "pt", "Hello, World!") is None
    assert cache.misses == 1

    ev = cache.store("en", "pt", "Hello, World!", "Olá, mundo!")
    assert ev is not None
    assert ev["kind"] == "store"
    assert cache.stores == 1

    hit = cache.lookup("en", "pt", "hello world")  # normalized match
    assert hit == "Olá, mundo!"
    assert cache.hits >= 1
    assert cache.last_event["kind"] == "hit"
    assert cache.last_event["layer"] == "memory"


def test_cache_force_next_skips_hit(tmp_db, mock_cfg):
    cache = PhraseCache(mock_cfg)
    cache.store("en", "pt", "ping", "pong")
    cache.request_force_next()
    assert cache.force_pending() is True
    assert cache.lookup("en", "pt", "ping") is None
    assert cache.consume_force_next() is True
    assert cache.force_pending() is False
    # After consume, HIT works again
    assert cache.lookup("en", "pt", "ping") == "pong"


def test_cache_sqlite_layer_hit(tmp_db, mock_cfg):
    """Second cache instance cold-starts from SQLite filled by first store."""
    c1 = PhraseCache(mock_cfg)
    c1.store("en", "pt", "exact sentence here", "frase exata aqui")
    c2 = PhraseCache(mock_cfg)
    out = c2.lookup("en", "pt", "exact sentence here")
    assert out == "frase exata aqui"
    assert c2.last_event["layer"] in ("sqlite", "memory")


def test_set_enabled(mock_cfg):
    cache = PhraseCache(mock_cfg)
    assert cache.set_enabled(False) is False
    assert cache.enabled is False
    assert cache.set_enabled(True) is True
