"""VAD end-silence: fast short turns, patient long monologues."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from livelingo.capture import Recorder


def _cfg(**overrides):
    base = dict(
        SAMPLE_RATE=16000,
        BLOCK_DURATION=0.03,
        SILENCE_DURATION=1.5,
        SILENCE_THRESHOLD=0.02,
        MIN_SPEECH_DURATION=0.4,
        MAX_CHUNK_DURATION=60.0,
        CHUNK_DURATION=6.0,
        PREROLL_DURATION=0.5,
        VAD_ONSET_BLOCKS=2,
        VAD_ONSET_GAP_BLOCKS=2,
        VAD_ONSET_THRESHOLD_SCALE=0.75,
        VAD_SPLIT_OVERLAP=1.5,
        VAD_ADAPTIVE_SILENCE=True,
        VAD_SILENCE_SCALE_MAX=4.0,
        VAD_SPEECH_HANGOVER=0.65,
        VAD_MODE="energy",
        SENTENCE_SPLIT=False,
        PARAGRAPH_SPLIT=False,
        PARAGRAPH_SILENCE=1.0,
        PARAGRAPH_MIN_SPEECH=5.0,
        PARAGRAPH_SPLIT_OVERLAP=0.3,
        SOUND_OFF_SILENCE_DURATION=1.6,
        ROLLING_CHUNK_DURATION=2.5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _recorder(cfg, shorter_end=False):
    return Recorder(
        cfg,
        device_index=0,
        chunk_queue=queue.Queue(),
        stop_event=threading.Event(),
        shorter_end_enabled=(lambda: True) if shorter_end else (lambda: False),
    )


def _blocks_to_sec(blocks, block_dur=0.03):
    return blocks * block_dur


def test_short_speech_ends_near_base_silence():
    """Frase curta (~1s) → perto da base (ainda sem escala forte)."""
    rec = _recorder(_cfg(SILENCE_DURATION=2.2))
    frames = int(1.0 * 16000)  # 1s de fala
    need = rec._silence_blocks_to_end(frames)
    sec = _blocks_to_sec(need)
    # <1.2s speech → no adaptive yet (threshold is 1.2)
    assert 2.0 <= sec <= 2.4, f"short end silence {sec:.2f}s"


def test_long_speech_waits_more_before_translate():
    """Monólogo longo → silêncio adaptativo maior (não corta mid-thought)."""
    rec = _recorder(_cfg(SILENCE_DURATION=2.2, VAD_SILENCE_SCALE_MAX=4.0))
    short = _blocks_to_sec(rec._silence_blocks_to_end(int(1.0 * 16000)))
    mid = _blocks_to_sec(rec._silence_blocks_to_end(int(10.0 * 16000)))
    long_ = _blocks_to_sec(rec._silence_blocks_to_end(int(30.0 * 16000)))
    assert mid > short, f"mid {mid:.2f} should exceed short {short:.2f}"
    assert long_ > mid, f"long {long_:.2f} should exceed mid {mid:.2f}"
    # 2.2 * ~4.0 ≈ 8.8s region for 30s speech
    assert long_ >= 6.0, f"long monologue end silence too short: {long_:.2f}s"
    assert long_ <= 10.0, f"long monologue end silence too long: {long_:.2f}s"


def test_adaptive_off_stays_at_base():
    rec = _recorder(_cfg(SILENCE_DURATION=1.5, VAD_ADAPTIVE_SILENCE=False))
    short = rec._silence_blocks_to_end(int(1.0 * 16000))
    long_ = rec._silence_blocks_to_end(int(40.0 * 16000))
    assert short == long_


def test_sound_off_base_still_scales_for_long():
    """SOUND_OFF base menor, mas monólogo ainda exige mais pausa."""
    rec = _recorder(
        _cfg(SILENCE_DURATION=1.5, SOUND_OFF_SILENCE_DURATION=1.6),
        shorter_end=True,
    )
    short = _blocks_to_sec(rec._silence_blocks_to_end(int(1.0 * 16000)))
    long_ = _blocks_to_sec(rec._silence_blocks_to_end(int(25.0 * 16000)))
    assert 1.5 <= short <= 1.8
    assert long_ > short * 1.5


def test_old_5s_base_is_slower_than_balanced_profile():
    """Regressão: base 5.0s sempre lenta; perfil 2.2+adaptativo é mais rápido em short."""
    slow = _recorder(_cfg(SILENCE_DURATION=5.0, VAD_ADAPTIVE_SILENCE=True))
    fast = _recorder(_cfg(SILENCE_DURATION=2.2, VAD_ADAPTIVE_SILENCE=True))
    frames = int(1.0 * 16000)
    assert slow._silence_blocks_to_end(frames) > fast._silence_blocks_to_end(frames) * 1.5


def test_live_captions_start_on_launch_default_false():
    import config as cfg

    assert getattr(cfg, "LIVE_CAPTIONS_START_ON_LAUNCH", True) is False
