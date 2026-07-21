"""Unit tests for mic mute helpers (pure matching + app gate)."""

from __future__ import annotations

from livelingo import mic_control
from livelingo.mic_control import MicController, availability_note, available


def test_normalize_name_strips_host_api_suffix():
    n = mic_control._normalize_name("Microphone (Realtek Audio) (MME)")
    assert "mme" not in n
    assert "microphone" in n or "realtek" in n


def test_match_score_exact_and_substring():
    assert mic_control._match_score("Headset Mic", "Headset Mic") == 100.0
    assert mic_control._match_score("Cable", "CABLE Input VB-Audio") >= 85.0
    assert mic_control._match_score("", "x") == 0.0


def test_match_score_partial_tokens():
    score = mic_control._match_score("USB Headset", "USB Headset Microphone Array")
    assert score > 0


def test_available_and_note_on_linux():
    # On WSL/Linux, OS mute is not available
    note = availability_note()
    assert isinstance(note, str)
    assert isinstance(available(), bool)
    if not available():
        assert "Windows" in note or "pycaw" in note or "not" in note.lower()


def test_mic_controller_app_gate_without_os():
    ctl = MicController(device_name="Fake Mic")
    ctl.set_app_muted(True)
    assert ctl.is_app_muted() is True
    assert ctl.is_muted() is True  # app gate forces muted
    ctl.set_app_muted(False)
    # Without OS, effectively not muted when app gate off
    assert ctl.is_app_muted() is False


def test_mic_controller_set_muted_toggle_app_level():
    ctl = MicController(device_name="")
    muted, _os_ok, _name = ctl.set_muted(True)
    assert muted is True
    now, _os_ok2, _n2 = ctl.toggle()
    assert now is False
