"""Unit tests for LiveCaptions pure text helpers (no Windows UIA)."""

from __future__ import annotations

from types import SimpleNamespace

from livelingo.livecaptions import (
    caption_lang_pair,
    is_same_utterance,
    is_windows,
    preprocess_captions,
    replace_newlines,
    shorten_display,
    text_similarity,
    uia_available,
)


def test_is_windows_bool():
    assert isinstance(is_windows(), bool)


def test_uia_available_false_on_linux():
    if not is_windows():
        assert uia_available() is False


def test_caption_lang_pair_inverts_by_default():
    cfg = SimpleNamespace(
        SOURCE_LANG="pt",
        TARGET_LANG="en",
        LIVE_CAPTIONS_INVERT_LANGS=True,
        LIVE_CAPTIONS_SOURCE_LANG="",
        LIVE_CAPTIONS_TARGET_LANG="",
    )
    assert caption_lang_pair(cfg) == ("en", "pt")


def test_caption_lang_pair_no_invert():
    cfg = SimpleNamespace(
        SOURCE_LANG="pt",
        TARGET_LANG="en",
        LIVE_CAPTIONS_INVERT_LANGS=False,
        LIVE_CAPTIONS_SOURCE_LANG="",
        LIVE_CAPTIONS_TARGET_LANG="",
    )
    assert caption_lang_pair(cfg) == ("pt", "en")


def test_caption_lang_pair_explicit_override():
    cfg = SimpleNamespace(
        SOURCE_LANG="pt",
        TARGET_LANG="en",
        LIVE_CAPTIONS_INVERT_LANGS=True,
        LIVE_CAPTIONS_SOURCE_LANG="fr",
        LIVE_CAPTIONS_TARGET_LANG="de",
    )
    assert caption_lang_pair(cfg) == ("fr", "de")


def test_text_similarity_identical_and_prefix():
    assert text_similarity("Hello", "Hello") == 1.0
    assert text_similarity("Hello world", "Hello") == 1.0
    assert text_similarity("", "") == 1.0
    assert text_similarity("a", "") == 0.0


def test_is_same_utterance_growth():
    assert is_same_utterance("The item.", "The item that we discussed") is True
    assert is_same_utterance("Completely different", "Other topic here") is False


def test_preprocess_and_newlines():
    out = preprocess_captions("Hello\nworld")
    assert isinstance(out, str)
    assert "Hello" in out or "world" in out
    joined = replace_newlines("Short\nNext", byte_threshold=1000)
    assert "—" in joined or "Short" in joined


def test_shorten_display_trims_long():
    long = "First. " + ("word " * 200)
    short = shorten_display(long, max_byte_length=40)
    assert len(short.encode("utf-8")) <= len(long.encode("utf-8"))
