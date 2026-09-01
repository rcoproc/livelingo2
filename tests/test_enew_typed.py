"""enew / TypedTextItem: priority queue + fixed PT→EN pair."""

from __future__ import annotations

import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def pipeline_mod(monkeypatch):
    """Import pipeline with PortAudio stubbed."""
    monkeypatch.setitem(sys.modules, "sounddevice", MagicMock())
    # Drop cached modules that imported real sounddevice
    for name in list(sys.modules):
        if name.startswith("livelingo.pipeline") or name.startswith("livelingo.playback"):
            sys.modules.pop(name, None)
    from livelingo import pipeline as pl

    return pl


def test_typed_text_item_normalizes(pipeline_mod):
    TypedTextItem = pipeline_mod.TypedTextItem
    job = TypedTextItem("  Olá   mundo  \n", source_lang="pt", target_lang="en")
    assert job.text == "Olá mundo"
    assert job.source_lang == "pt"
    assert job.target_lang == "en"
    assert TypedTextItem("").text == ""


def test_enqueue_typed_text_priority_over_mic_queue(pipeline_mod):
    """Processor must see enew before a pending mic chunk."""
    Pipeline = pipeline_mod.Pipeline
    TypedTextItem = pipeline_mod.TypedTextItem

    # Minimal host: only queues + stop + alloc
    host = SimpleNamespace()
    host.chunk_queue = queue.Queue()
    host._typed_queue = queue.Queue()
    host.stop_event = threading.Event()
    host._chunk_num_lock = threading.Lock()
    host._chunk_count = 0
    host.cfg = SimpleNamespace(SOUND_OFF_PARALLEL=False, SOUND_ON_PARALLEL=False)

    def alloc():
        with host._chunk_num_lock:
            host._chunk_count += 1
            return host._chunk_count

    host._alloc_chunk_num = alloc
    host._ensure_executor = lambda: None
    handled = []

    def handle(item, n):
        handled.append((item, n))
        host.stop_event.set()

    host._handle_chunk = handle
    host._try_apply_pending_language_swap = lambda: None
    host.sync_capture_gate = lambda **kw: True
    host.is_sound_enabled = lambda: True

    # Mic backlog first, then enew — enew must still run first
    host.chunk_queue.put("mic-audio-placeholder")
    assert Pipeline.enqueue_typed_text(host, "frase em português") is True

    # Run one iteration of process loop logic (prefer typed)
    item = None
    try:
        item = host._typed_queue.get_nowait()
    except queue.Empty:
        item = host.chunk_queue.get(timeout=0.1)
    n = host._alloc_chunk_num()
    host._handle_chunk(item, n)

    assert len(handled) == 1
    assert isinstance(handled[0][0], TypedTextItem)
    assert handled[0][0].text == "frase em português"
    assert handled[0][0].source_lang == "pt"
    assert handled[0][0].target_lang == "en"


def test_translate_text_restores_system_langs(pipeline_mod):
    Pipeline = pipeline_mod.Pipeline

    cfg = SimpleNamespace(SOURCE_LANG="fr", TARGET_LANG="en")
    calls = []

    class FakeTr:
        def set_language_pair(self, source=None, target=None):
            calls.append(("set", source, target))
            cfg.SOURCE_LANG = source
            cfg.TARGET_LANG = target

        def translate(self, text):
            calls.append(("tr", cfg.SOURCE_LANG, cfg.TARGET_LANG, text))
            return "Hello"

    host = SimpleNamespace(
        cfg=cfg,
        translator=FakeTr(),
        _translate_lang_lock=threading.Lock(),
    )
    host._bind_translator_langs = lambda s, t: Pipeline._bind_translator_langs(host, s, t)

    out = Pipeline._translate_text(
        host, "Olá", stream=False, source_lang="pt", target_lang="en"
    )
    assert out == "Hello"
    assert cfg.SOURCE_LANG == "fr"
    assert cfg.TARGET_LANG == "en"
    assert ("tr", "pt", "en", "Olá") in calls


def test_emit_voz_respects_lang_override():
    from livelingo import ui

    captured = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        ui.chunk_text_preview(
            1,
            "Olá",
            "Hello",
            source_lang="pt",
            target_lang="en",
        )
    finally:
        ui.set_log_sink(prev)

    body = "\n".join(t for _, t, _ in captured)
    assert "Olá" in body and "Hello" in body
    # BR/EN labels (pt→BR display)
    assert "BR" in body or "PT" in body
    assert "EN" in body
