"""SOUND_ON_PARALLEL: next chunk STT/Trad while previous TTS is serialized."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

# PortAudio may be missing in CI/WSL — stub before importing pipeline.
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = MagicMock()


def _cfg(**kwargs):
    base = dict(
        SOUND_OFF_PARALLEL=True,
        SOUND_OFF_WORKERS=2,
        SOUND_ON_PARALLEL=False,
        SOUND_ON_WORKERS=2,
        STREAMING_LLM=True,
        STREAMING_TTS=True,
        STREAMING_TTS_OVERLAP=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _pipeline_stub(cfg, sound_on: bool):
    from livelingo.pipeline import Pipeline

    p = object.__new__(Pipeline)
    p.cfg = cfg
    p.sound_enabled = sound_on
    p.sound_lock = __import__("threading").Lock()
    p._executor = None
    return p


def test_use_parallel_sound_off_default():
    p = _pipeline_stub(_cfg(), sound_on=False)
    assert p._use_parallel_processing() is True
    p.cfg.SOUND_OFF_PARALLEL = False
    assert p._use_parallel_processing() is False


def test_use_parallel_sound_on_flag():
    p = _pipeline_stub(_cfg(SOUND_ON_PARALLEL=False), sound_on=True)
    assert p._use_parallel_processing() is False
    p.cfg.SOUND_ON_PARALLEL = True
    assert p._use_parallel_processing() is True


def test_ensure_executor_sound_on_workers():
    p = _pipeline_stub(_cfg(SOUND_ON_PARALLEL=True, SOUND_ON_WORKERS=3), sound_on=True)
    ex = p._ensure_executor()
    assert ex is not None
    assert ex._max_workers == 3
    ex.shutdown(wait=False)


def test_ensure_executor_none_when_sound_on_parallel_off():
    p = _pipeline_stub(_cfg(SOUND_ON_PARALLEL=False), sound_on=True)
    assert p._ensure_executor() is None
