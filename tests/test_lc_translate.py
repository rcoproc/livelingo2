"""LiveCaptions translation: failover + no silent English echo."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_build_caption_translator_uses_failover_when_llm():
    from livelingo.failover import FailoverTranslator
    from livelingo.livecaptions import build_caption_translator

    cfg = SimpleNamespace(
        TRANSLATION_ENGINE="llm",
        TRANSLATION_FALLBACK="google",
        GROQ_API_KEY="test-key",
        GROQ_MODEL="llama-3.1-8b-instant",
        LLM_TIMEOUT=5,
        LOW_LATENCY=False,
        SOURCE_LANG="en",
        TARGET_LANG="pt",
        CIRCUIT_FAIL_THRESHOLD=3,
        CIRCUIT_COOLDOWN_S=60,
        FAILOVER_MAX_RETRIES=1,
        FAILOVER_RETRY_SLEEP_S=0.01,
        FAILOVER_LOG=False,
    )
    tr = build_caption_translator(cfg, "en", "pt")
    assert isinstance(tr, FailoverTranslator)
    assert tr.secondary is not None


def test_translate_with_cache_flags_same_text_as_error():
    from livelingo.livecaptions import CaptionService

    cfg = SimpleNamespace(
        LIVE_CAPTIONS_SOURCE_LANG="en",
        LIVE_CAPTIONS_TARGET_LANG="pt",
        LIVE_CAPTIONS_INVERT_LANGS=True,
        SOURCE_LANG="pt",
        TARGET_LANG="en",
        TRANSLATION_ENGINE="google",
        TRANSLATION_FALLBACK="google",
        GROQ_API_KEY="",
        PHRASE_CACHE=False,
        PHRASE_CACHE_LOG=False,
        VERBOSE=False,
        LIVE_CAPTIONS_LOG=True,
    )
    svc = CaptionService(cfg, log_to_ui=False)
    # Echo primary + secondary + force one-shot path to also echo → ERROR
    svc.translator = SimpleNamespace(translate=lambda t: t, secondary=None)
    svc.phrase_cache = None

    # Patch one-shot Google to also echo so we hit the ERROR branch
    class _EchoTr:
        def translate(self, text):
            return text

    from livelingo import translate as tr_mod

    old = tr_mod.Translator
    tr_mod.Translator = lambda proxy: _EchoTr()  # type: ignore
    try:
        out, hit = svc._translate_with_cache("Hello everyone here today")
    finally:
        tr_mod.Translator = old
    assert hit is False
    assert out.startswith("[ERROR]")
    assert "mesmo texto" in out.lower() or "sem tradução" in out.lower()


def test_do_translate_live_retries_google_on_llm_echo(monkeypatch):
    from livelingo.livecaptions import CaptionService

    cfg = SimpleNamespace(
        LIVE_CAPTIONS_SOURCE_LANG="en",
        LIVE_CAPTIONS_TARGET_LANG="pt",
        LIVE_CAPTIONS_INVERT_LANGS=True,
        SOURCE_LANG="pt",
        TARGET_LANG="en",
        TRANSLATION_ENGINE="llm",
        TRANSLATION_FALLBACK="google",
        GROQ_API_KEY="x",
        PHRASE_CACHE=False,
        PHRASE_CACHE_LOG=False,
        VERBOSE=False,
        LIVE_CAPTIONS_LOG=True,
        GROQ_MODEL="x",
        LLM_TIMEOUT=5,
        LOW_LATENCY=False,
    )
    svc = CaptionService(cfg, log_to_ui=False)
    svc._caption_src, svc._caption_tgt = "en", "pt"
    google = SimpleNamespace(translate=lambda t: "Olá a todos aqui hoje")
    llm = SimpleNamespace(translate=lambda t: t, secondary=google)
    svc.translator = llm
    monkeypatch.setattr("livelingo.ui.warn", lambda *a, **k: None)
    out = svc._do_translate_live("Hello everyone here today")
    assert out == "Olá a todos aqui hoje"


def test_live_caption_block_always_has_translated_line():
    from livelingo import ui

    captured = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        ui.live_caption_block(1, "Only English caption", "")
    finally:
        ui.set_log_sink(prev)

    body = "\n".join(t for _, t, p in captured if p == "lc")
    assert "Only English caption" in body
    assert "sem tradução" in body.lower() or "Translated" in body or "[!]" in body


def test_commit_error_still_logs_pair(monkeypatch):
    from livelingo.livecaptions import CaptionService

    cfg = SimpleNamespace(
        LIVE_CAPTIONS_SOURCE_LANG="en",
        LIVE_CAPTIONS_TARGET_LANG="pt",
        LIVE_CAPTIONS_INVERT_LANGS=True,
        SOURCE_LANG="pt",
        TARGET_LANG="en",
        TRANSLATION_ENGINE="google",
        GROQ_API_KEY="",
        PHRASE_CACHE=False,
        LIVE_CAPTIONS_LOG=True,
        VERBOSE=False,
    )
    svc = CaptionService(cfg, log_to_ui=True)
    logged = []

    def fake_block(n, src, tgt, from_cache=None):
        logged.append((n, src, tgt))

    monkeypatch.setattr("livelingo.ui.live_caption_block", fake_block)
    monkeypatch.setattr("livelingo.ui.warn", lambda *a, **k: None)

    svc._open_src = "Hello from the meeting"
    svc._open_tgt = "[ERROR] rate limit"
    svc._open_from_cache = False
    svc._last_logged_src = ""
    svc._commit_open_to_log()
    assert logged
    assert logged[0][1] == "Hello from the meeting"
    assert logged[0][2].startswith("[ERROR]")
