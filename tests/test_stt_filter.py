"""Unit tests for STT hallucination filter (pure logic)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from livelingo.stt_filter import (
    audio_rms,
    clean_transcript,
    is_hallucination,
    should_discard_transcript,
    strip_hallucinations,
)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Thanks for watching",
        "goodbye",
        "Good night.",
        "thank you",
        "silence",
        "[music]",
        "subscribe",
        "legenda por amara.org",
        "tchau",
        "boa noite",
    ],
)
def test_is_hallucination_true(text):
    assert is_hallucination(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Hello everyone, welcome to the meeting",
        "We need to discuss the quarterly results",
        "Por favor confirme o horário da call",
    ],
)
def test_is_hallucination_false_real_speech(text):
    assert is_hallucination(text) is False


def test_strip_hallucinations_removes_credit_tail():
    text = "This is real speech. Subtitles by John Doe"
    out = strip_hallucinations(text)
    assert "real speech" in out.lower()
    assert "subtitle" not in out.lower()


def test_strip_hallucinations_pure_credit_becomes_empty():
    assert strip_hallucinations("Thanks for watching") == ""


def test_strip_does_not_eat_mid_sentence_traduzido():
    # Guard: PT speech with "traduzido por" mid-sentence should not be wiped
    text = "O documento foi traduzido por nosso time ontem"
    assert strip_hallucinations(text) == text


def test_clean_transcript_reports_modification():
    cleaned, modified = clean_transcript(
        "Hello world. Goodbye",
        SimpleNamespace(STT_HALLUCINATION_FILTER=True),
    )
    assert modified is True
    assert "hello" in cleaned.lower()
    assert "goodbye" not in cleaned.lower()


def test_clean_transcript_filter_disabled():
    raw = "Thanks for watching"
    cleaned, modified = clean_transcript(
        raw,
        SimpleNamespace(STT_HALLUCINATION_FILTER=False),
    )
    assert cleaned == raw
    assert modified is False


def test_audio_rms_empty_and_signal():
    assert audio_rms(None) == 0.0
    assert audio_rms(np.array([], dtype=np.float32)) == 0.0
    silent = np.zeros(1600, dtype=np.float32)
    assert audio_rms(silent) == 0.0
    loud = np.ones(1600, dtype=np.float32) * 0.5
    assert audio_rms(loud) > 0.4


def test_should_discard_hallucination_phrase():
    audio = np.ones(1600, dtype=np.float32) * 0.2
    assert should_discard_transcript(audio, "goodbye") is True


def test_should_discard_low_energy_short_text():
    cfg = SimpleNamespace(
        STT_HALLUCINATION_FILTER=True,
        SAMPLE_RATE=16000,
        STT_MIN_RMS=0.004,
        STT_LOW_ENERGY_MAX_WORDS=2,
        STT_LOW_ENERGY_MAX_SEC=1.2,
    )
    # ~0.1 s of near-silence + ultra-short text (1–2 words)
    audio = np.zeros(1600, dtype=np.float32)
    assert should_discard_transcript(audio, "hi there", cfg) is True
    assert should_discard_transcript(audio, "ok", cfg) is True


def test_should_keep_quiet_real_pt_phrases():
    """Regression: soft mic + short clause must not be energy-dropped.

    User log: "E aí, vai encarar?" / "Tem que ser ágil para encarar."
    were discarded with old max_words=6 + max_sec=2.5 + rms=0.01.
    """
    from livelingo.stt_filter import transcript_discard_reason

    cfg = SimpleNamespace(
        STT_HALLUCINATION_FILTER=True,
        SAMPLE_RATE=16000,
        STT_MIN_RMS=0.004,
        STT_LOW_ENERGY_MAX_WORDS=2,
        STT_LOW_ENERGY_MAX_SEC=1.2,
    )
    # Quiet audio ~2.4s (same order as user VAD end)
    n = int(2.4 * 16000)
    quiet = np.ones(n, dtype=np.float32) * 0.006  # below old 0.010 floor
    for text in (
        "E aí, vai encarar?",
        "Tem que ser ágil para encarar.",
        "So, are you going to face it?",
    ):
        assert should_discard_transcript(quiet, text, cfg) is False, text
        assert transcript_discard_reason(quiet, text, cfg) is None, text


def test_should_keep_loud_real_speech():
    cfg = SimpleNamespace(
        STT_HALLUCINATION_FILTER=True,
        SAMPLE_RATE=16000,
        STT_MIN_RMS=0.004,
        STT_LOW_ENERGY_MAX_WORDS=2,
        STT_LOW_ENERGY_MAX_SEC=1.2,
    )
    audio = np.ones(8000, dtype=np.float32) * 0.2  # 0.5s
    text = "We should schedule the architecture review for next week"
    assert should_discard_transcript(audio, text, cfg) is False


def test_should_discard_disabled():
    assert (
        should_discard_transcript(
            None,
            "goodbye",
            SimpleNamespace(STT_HALLUCINATION_FILTER=False),
        )
        is False
    )
