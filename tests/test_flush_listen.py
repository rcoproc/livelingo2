"""Force-flush utterance ([go]/F6) and parallel Cable|monitor playback."""

from __future__ import annotations

import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

# PortAudio may be missing in CI/WSL — stub before importing playback.
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = MagicMock()

from livelingo.capture import Recorder
from livelingo.playback import Player


def _cfg(**overrides):
    base = dict(
        SAMPLE_RATE=16000,
        BLOCK_DURATION=0.03,
        SILENCE_DURATION=2.0,
        SILENCE_THRESHOLD=0.02,
        MIN_SPEECH_DURATION=0.15,
        MAX_CHUNK_DURATION=60.0,
        CHUNK_DURATION=6.0,
        PREROLL_DURATION=0.15,
        VAD_ONSET_BLOCKS=1,
        VAD_ONSET_GAP_BLOCKS=1,
        VAD_ONSET_THRESHOLD_SCALE=0.75,
        VAD_SPLIT_OVERLAP=0.1,
        VAD_ADAPTIVE_SILENCE=False,
        VAD_SILENCE_SCALE_MAX=1.0,
        VAD_SPEECH_HANGOVER=0.65,
        VAD_MODE="energy",
        SENTENCE_SPLIT=False,
        PARAGRAPH_SPLIT=False,
        PARAGRAPH_SILENCE=1.0,
        PARAGRAPH_MIN_SPEECH=5.0,
        PARAGRAPH_SPLIT_OVERLAP=0.3,
        SOUND_OFF_SILENCE_DURATION=1.6,
        ROLLING_CHUNK_DURATION=2.5,
        STT_HALLUCINATION_FILTER=False,
        CHANNELS=1,
        VAD_ENABLED=True,
        FORCE_LISTEN_THRESHOLD_SCALE=0.12,
        FORCE_LISTEN_ONSET_BLOCKS=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_force_end_false_when_idle():
    rec = Recorder(
        _cfg(),
        0,
        queue.Queue(),
        threading.Event(),
        capture_should_run=lambda: True,
    )
    assert rec.is_in_speech() is False
    assert rec.force_end_utterance() is False


def test_force_end_sets_flag_when_in_speech():
    rec = Recorder(
        _cfg(),
        0,
        queue.Queue(),
        threading.Event(),
        capture_should_run=lambda: True,
    )
    rec._in_speech.set()
    assert rec.force_end_utterance() is True
    assert rec._force_end_utterance.is_set() is True


def test_pipeline_flush_listen_now_messages():
    from livelingo.pipeline import Pipeline

    pipe = SimpleNamespace()
    rec = MagicMock()
    rec.is_capture_enabled.return_value = True
    rec.is_in_speech.return_value = False
    rec.force_end_utterance.return_value = False
    pipe.recorder = rec
    ok, msg = Pipeline.flush_listen_now(pipe)
    assert ok is False
    assert "Nada para flush" in msg or "sem fala" in msg.lower()

    rec.is_in_speech.return_value = True
    rec.force_end_utterance.return_value = True
    ok, msg = Pipeline.flush_listen_now(pipe)
    assert ok is True
    assert "Flush" in msg or "STT" in msg


def test_write_cable_and_monitor_parallel_wall_clock():
    """Parallel dual-write ≈ 1× audio duration, not 2× sequential passes."""

    class _SlowStream:
        def __init__(self, delay_per_write: float):
            self.active = True
            self.writes = 0
            self._delay = delay_per_write

        def write(self, chunk):
            time.sleep(self._delay)
            self.writes += 1

    # Bypass real PortAudio open
    p = object.__new__(Player)
    p.device = 0
    p.samplerate = 16000
    p.monitor_device = 1
    p.monitor_full_playback = True
    p.block_frames = 4000  # 0.25s @ 16k → 4 blocks for 1s audio
    p._interrupt = threading.Event()
    p._stream_lock = threading.Lock()
    delay = 0.05  # per block write
    p._stream = _SlowStream(delay)
    p._monitor_stream = _SlowStream(delay)

    # 16000 samples = 1s; 4 blocks of 4000
    audio = np.zeros(16000, dtype=np.float32)
    t0 = time.perf_counter()
    p._write_cable_and_monitor_parallel(audio)
    elapsed = time.perf_counter() - t0

    assert p._stream.writes == 4
    assert p._monitor_stream.writes == 4
    # Sequential would be ~0.4s (4*0.05*2); parallel ~0.2s (4*0.05)
    assert elapsed < 0.35, f"expected parallel ~0.2s, got {elapsed:.3f}s"
    assert elapsed >= 0.15, f"writes too fast ({elapsed:.3f}s) — mock broken?"
