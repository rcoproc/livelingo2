"""Unit tests for the ``interview`` / ``iv`` preset command."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


def _stub_native_deps() -> None:
    """Stub PortAudio / Whisper / optional natives so ``import main`` works in CI/WSL."""
    stubs = (
        "sounddevice",
        "faster_whisper",
        "ctranslate2",
        "av",
        "tokenizers",
        "edge_tts",
        "deep_translator",
        "groq",
        "uiautomation",
        "pycaw",
        "comtypes",
        "cv2",
        "mediapipe",
        "pyvirtualcam",
        "onnxruntime",
    )
    for name in stubs:
        sys.modules.setdefault(name, MagicMock())
    fw = sys.modules["faster_whisper"]
    if not hasattr(fw, "WhisperModel"):
        fw.WhisperModel = MagicMock()


@pytest.fixture(scope="module")
def main_mod():
    _stub_native_deps()
    import main as main_mod

    return main_mod


def test_cmd_interview_forces_sound_off_and_runs_subcommands(main_mod):
    pipeline = MagicMock()
    pipeline.is_sound_enabled.return_value = True
    pipeline.webcam_service = None
    pipeline.caption_service = None
    pipeline.interview_coach = None

    # No call_from_thread → direct set_* path (TUI classic / unit)
    indicator = MagicMock(
        spec=[
            "set_sound_on",
            "set_coach_panel_visible",
            "set_coach_minimized",
            "set_interview_mode",
        ]
    )
    synonym_lookup = MagicMock()
    dispatched = []

    def _fake_dispatch(pipe, syn, raw, cmd, ind=None):
        dispatched.append((raw, cmd))

    with (
        patch.object(main_mod, "_dispatch_command", side_effect=_fake_dispatch),
        patch.object(main_mod.ui, "warn") as warn,
        patch.object(main_mod.ui, "info") as info,
        patch.object(main_mod.ui, "success") as success,
    ):
        main_mod._cmd_interview(pipeline, synonym_lookup, indicator)

    pipeline.set_sound_enabled.assert_called_once_with(False)
    indicator.set_sound_on.assert_called_once_with(False)
    indicator.set_coach_panel_visible.assert_called_once_with(True)
    indicator.set_coach_minimized.assert_called_once_with(True)
    indicator.set_interview_mode.assert_called_once_with(True)
    warn.assert_any_call(
        "Sound OFF — só texto (TTS omitido se TTS_SKIP_WHEN_MUTED). "
        "Pressione [s] para ouvir de novo.",
        indent=3,
        panel="app",
    )
    assert dispatched == [
        ("cam off", "cam off"),
        ("lc on", "lc on"),
        ("coach on", "coach on"),
    ]
    success.assert_any_call(
        "Modo interview: som OFF · cam OFF · LC ON · Coach ON · minimizado",
        indent=3,
        panel="app",
    )
    info.assert_any_call(
        "Use comando airespond de simulação para coach!",
        indent=3,
        panel="main",
    )
    info.assert_any_call(
        "Use comando > python main.py view coach",
        indent=3,
        panel="main",
    )


def test_cmd_interview_idempotent_when_sound_already_off(main_mod):
    pipeline = MagicMock()
    pipeline.is_sound_enabled.return_value = False
    indicator = MagicMock()

    with (
        patch.object(main_mod, "_dispatch_command"),
        patch.object(main_mod.ui, "info") as info,
        patch.object(main_mod.ui, "warn") as warn,
        patch.object(main_mod.ui, "success"),
    ):
        main_mod._cmd_interview(pipeline, MagicMock(), indicator)

    pipeline.set_sound_enabled.assert_called_once_with(False)
    info.assert_any_call("Sound já OFF.", indent=3, panel="app")
    for c in warn.call_args_list:
        assert "Sound OFF — só texto" not in str(c)


def test_dispatch_interview_alias_iv(main_mod):
    with patch.object(main_mod, "_cmd_interview") as mock_iv:
        main_mod._dispatch_command(MagicMock(), MagicMock(), "iv", "iv", None)
    mock_iv.assert_called_once()
    with patch.object(main_mod, "_cmd_interview") as mock_iv:
        main_mod._dispatch_command(
            MagicMock(), MagicMock(), "interview", "interview", None
        )
    mock_iv.assert_called_once()


def test_dispatch_interview_off(main_mod):
    ind = MagicMock(spec=["set_interview_mode"])
    with patch.object(main_mod.ui, "info") as info:
        main_mod._dispatch_command(
            MagicMock(), MagicMock(), "interview off", "interview off", ind
        )
    ind.set_interview_mode.assert_called_once_with(False)
    assert any("Interview Mode OFF" in str(c) for c in info.call_args_list)
