"""
Import smoke tests — ensure security upgrades don't break module load.

Heavy optional engines (faster-whisper, edge-tts network) are not exercised.
"""

from __future__ import annotations

import importlib

import pytest

# Modules that never need PortAudio / GPU — always importable in CI/WSL.
CORE_MODULES = [
    "config",
    "livelingo",
    "livelingo.translate",
    "livelingo.llm",
    "livelingo.groq_transcribe",
    "livelingo.failover",
    "livelingo.webcam",
    "livelingo.stt_filter",
    "livelingo.synonyms",
    "livelingo.synthesis_error",
    "livelingo.tts_segments",
    "livelingo.db",
]

# Need PortAudio (libportaudio). Skip cleanly on headless/WSL without system libs.
AUDIO_MODULES = [
    "livelingo.devices",
    "livelingo.capture",
    "livelingo.playback",
]


def _portaudio_available() -> bool:
    try:
        import sounddevice  # noqa: F401

        return True
    except OSError:
        return False


@pytest.mark.parametrize("modname", CORE_MODULES)
def test_import_core_module(modname: str):
    mod = importlib.import_module(modname)
    assert mod is not None


@pytest.mark.parametrize("modname", AUDIO_MODULES)
def test_import_audio_module(modname: str):
    if not _portaudio_available():
        pytest.skip("PortAudio not installed (expected on headless/WSL CI)")
    mod = importlib.import_module(modname)
    assert mod is not None


def test_config_has_essential_attrs():
    import config as cfg

    for name in (
        "SOURCE_LANG",
        "TARGET_LANG",
        "SAMPLE_RATE",
        "TRANSLATION_ENGINE",
        "STT_ENGINE",
        "STT_FALLBACK",
        "TRANSLATION_FALLBACK",
        "CIRCUIT_FAIL_THRESHOLD",
        "FAILOVER_MAX_RETRIES",
        "WEBCAM_ENABLED",
        "WEBCAM_LIP_ENGINE",
    ):
        assert hasattr(cfg, name), f"config missing {name}"


def test_translator_and_llm_share_interface(mock_cfg):
    """Pipeline treats both as .translate(text) drop-ins."""
    from unittest.mock import MagicMock, patch

    with (
        patch("livelingo.translate.GoogleTranslator"),
        patch("livelingo.llm.requests.Session"),
    ):
        from livelingo.llm import LLMTranslator
        from livelingo.translate import Translator

        g = Translator(mock_cfg)
        l = LLMTranslator(mock_cfg)
        assert callable(g.translate)
        assert callable(l.translate)
        assert callable(getattr(g, "set_language_pair", None))
        assert callable(getattr(l, "set_language_pair", None))
