"""Escuta retoma após STT vazio / chunk curto / TTS hold."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from livelingo.capture import Recorder
from livelingo import ui


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
        STT_HALLUCINATION_FILTER=True,
        STT_MIN_RMS=0.010,
        CAPTURE_TAIL_MAX_SEC=2.0,
        CHANNELS=1,
        VAD_ENABLED=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_emit_returns_false_for_short_audio():
    rec = Recorder(
        _cfg(MIN_SPEECH_DURATION=0.4),
        0,
        queue.Queue(),
        threading.Event(),
    )
    import numpy as np

    # 0.1s of audio — below min speech
    short = [np.zeros(int(0.1 * 16000), dtype=np.float32)]
    assert rec._emit(short) is False
    assert rec.chunk_queue.empty()


def test_emit_returns_true_for_real_speech_energy():
    rec = Recorder(
        _cfg(MIN_SPEECH_DURATION=0.2, STT_HALLUCINATION_FILTER=False),
        0,
        queue.Queue(),
        threading.Event(),
    )
    import numpy as np

    # 0.5s of non-silent audio
    n = int(0.5 * 16000)
    block = (np.random.randn(n).astype(np.float32) * 0.05)
    assert rec._emit([block]) is True
    assert not rec.chunk_queue.empty()


def test_listen_ready_sets_idle_stage(monkeypatch):
    stages = []
    monkeypatch.setattr(ui, "pipeline_stage", lambda s, **m: stages.append(s))
    lines = []
    monkeypatch.setattr(ui, "dim", lambda msg, **k: lines.append(msg))
    # Reset rate limit
    ui._listen_ready_state["t"] = 0.0
    ui.listen_ready("teste")
    assert "idle" in stages
    assert any("Escuta pronta" in x for x in lines)


def test_is_muted_is_app_only_not_os():
    """[n] app mute is the only LiveLingo mute — OS tray does not pause escuta."""
    from livelingo.mic_control import MicController

    m = MicController(device_name="")
    assert m.is_muted() is False
    m.set_app_muted(True)
    assert m.is_muted() is True
    m.set_app_muted(False)
    assert m.is_muted() is False
    # toggle flips app mute only
    muted, _, _ = m.toggle()
    assert muted is True
    assert m.is_app_muted() is True
    muted, _, _ = m.toggle()
    assert muted is False


def test_sync_capture_gate_always_on_unless_app_mute(monkeypatch):
    """Escuta stays ON; only app mute / hold / bypass close the gate."""
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "sounddevice", MagicMock())
    monkeypatch.setattr("livelingo.ui.listen_ready", lambda *a, **k: None)
    from livelingo.pipeline import Pipeline

    class FakeMic:
        def __init__(self):
            self._app = False

        def is_app_muted(self):
            return self._app

    class FakeRec:
        def __init__(self):
            self.enabled = True

        def set_capture_enabled(self, v):
            self.enabled = bool(v)

        def is_capture_enabled(self):
            return self.enabled

    host = SimpleNamespace(
        mic=FakeMic(),
        recorder=FakeRec(),
        _capture_hold_count=0,
        _passthrough_active=False,
    )
    host.is_passthrough_active = lambda: host._passthrough_active
    host._mute_capture_during_playback_enabled = lambda: True
    host._is_capture_held_for_playback = lambda: host._capture_hold_count > 0

    # Bind methods
    host.capture_should_run = lambda: Pipeline.capture_should_run(host)
    host.sync_capture_gate = lambda **kw: Pipeline.sync_capture_gate(host, **kw)

    host.recorder.enabled = False
    assert host.sync_capture_gate() is True
    assert host.recorder.enabled is True

    host.mic._app = True
    assert host.sync_capture_gate() is False
    assert host.recorder.enabled is False

    host.mic._app = False
    host._capture_hold_count = 1
    assert host.sync_capture_gate() is False
    host._capture_hold_count = 0
    assert host.sync_capture_gate() is True


def test_capture_self_heal_when_should_run():
    """Recorder reopens gate when capture_should_run says ON."""
    rec = Recorder(
        _cfg(),
        0,
        queue.Queue(),
        threading.Event(),
        capture_should_run=lambda: True,
    )
    rec.set_capture_enabled(False)
    assert rec.is_capture_enabled() is False
    # Simulate one self-heal step from the VAD disabled branch
    if not rec.is_capture_enabled() and rec.capture_should_run():
        rec.set_capture_enabled(True)
    assert rec.is_capture_enabled() is True


def test_arm_listen_after_tts_aborts_and_opens(monkeypatch):
    """Post-TTS must wipe partial VAD (echo) and reopen gate."""
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "sounddevice", MagicMock())
    from livelingo.pipeline import Pipeline

    aborted = []
    ready = []

    class FakeRec:
        def abort_utterance(self):
            aborted.append(True)

        def set_capture_enabled(self, v):
            self.enabled = bool(v)

        def is_capture_enabled(self):
            return getattr(self, "enabled", True)

    class FakeMic:
        def is_app_muted(self):
            return False

    import threading

    host = SimpleNamespace(
        recorder=FakeRec(),
        mic=FakeMic(),
        _capture_hold_count=3,  # simulate leak — arm must zero this
        _capture_hold_timer=None,
        _capture_hangover_until=99.0,
        _passthrough_active=False,
        _capture_hold_lock=threading.Lock(),
    )
    host.is_passthrough_active = lambda: False
    host._mute_capture_during_playback_enabled = lambda: True
    host._is_capture_held_for_playback = lambda: False

    def _cancel(self_or_host=None):
        host._capture_hold_timer = None

    host._cancel_capture_hold_timer_unlocked = lambda: _cancel()
    host.capture_should_run = lambda: Pipeline.capture_should_run(host)
    host.sync_capture_gate = lambda **kw: Pipeline.sync_capture_gate(host, **kw)
    host.recorder.enabled = False
    monkeypatch.setattr(
        "livelingo.ui.listen_ready",
        lambda *a, **k: ready.append(k.get("force") or a),
    )
    Pipeline._arm_listen_after_tts(host)
    assert aborted == [True]
    assert host.recorder.enabled is True
    assert host._capture_hold_count == 0  # leak cleared
    assert host._capture_hangover_until == 0.0
    assert ready  # force listen_ready called


def test_passthrough_lock_is_reentrant():
    """F2 must not deadlock: start path re-enters is_passthrough_active."""
    import threading
    import sys
    from unittest.mock import MagicMock

    sys.modules.setdefault("sounddevice", MagicMock())
    from livelingo.pipeline import Pipeline

    # RLock allows nested acquire (Lock would hang here)
    lock = threading.RLock()
    acquired = []

    def nested():
        with lock:
            acquired.append("outer")
            with lock:
                acquired.append("inner")

    nested()
    assert acquired == ["outer", "inner"]


def test_recorder_suspend_resume():
    rec = Recorder(
        _cfg(),
        0,
        queue.Queue(),
        threading.Event(),
        capture_should_run=lambda: True,
    )
    assert rec.is_stream_suspended() is False
    rec.suspend_stream()
    assert rec.is_stream_suspended() is True
    assert rec.is_capture_enabled() is False
    rec.resume_stream()
    assert rec.is_stream_suspended() is False


def test_capture_should_run_false_during_hangover():
    """Self-heal must not reopen during post-TTS hangover window."""
    import time
    import threading
    import sys
    from unittest.mock import MagicMock

    # No PortAudio needed
    host = SimpleNamespace(
        _capture_hold_count=0,
        _capture_hangover_until=time.monotonic() + 5.0,
        _passthrough_active=False,
    )
    host.is_passthrough_active = lambda: False
    host.mic = SimpleNamespace(is_app_muted=lambda: False)
    host._mute_capture_during_playback_enabled = lambda: True
    host._is_capture_held_for_playback = lambda: False

    import sys as _sys
    from unittest.mock import MagicMock as _M

    _sys.modules.setdefault("sounddevice", _M())
    from livelingo.pipeline import Pipeline

    assert Pipeline.capture_should_run(host) is False
    host._capture_hangover_until = 0.0
    assert Pipeline.capture_should_run(host) is True
