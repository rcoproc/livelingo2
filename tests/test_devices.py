"""Unit tests for audio device resolution (sounddevice mocked per-test)."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _dev(name, in_ch=0, out_ch=0, hostapi=0):
    return {
        "name": name,
        "max_input_channels": in_ch,
        "max_output_channels": out_ch,
        "hostapi": hostapi,
    }


@pytest.fixture
def devices_mod(monkeypatch):
    """Load livelingo.devices against a fake sounddevice (no PortAudio needed)."""
    fake_sd = ModuleType("sounddevice")
    fake_sd.query_devices = MagicMock(return_value=[])
    fake_sd.query_hostapis = MagicMock(return_value=[])
    fake_sd.default = SimpleNamespace(device=[0, 1])

    hostapis = [
        {"name": "MME"},
        {"name": "Windows WASAPI"},
    ]
    devs = [
        _dev("Microphone (Realtek)", in_ch=2, hostapi=0),
        _dev("Speakers (Realtek)", out_ch=2, hostapi=0),
        _dev("CABLE Input (VB-Audio Virtual Cable)", out_ch=2, hostapi=0),
        _dev("CABLE Input (VB-Audio Virtual Cable)", out_ch=2, hostapi=1),
        _dev("Headset Mic", in_ch=1, hostapi=1),
    ]

    def query_devices(idx=None):
        if idx is None:
            return devs
        return devs[idx]

    fake_sd.query_devices = query_devices
    fake_sd.query_hostapis = MagicMock(return_value=hostapis)

    # Isolate modules so we don't poison the rest of the suite
    saved_sd = sys.modules.get("sounddevice")
    saved_dev = sys.modules.pop("livelingo.devices", None)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    import livelingo.devices as devices

    # Prefer MME helpers use the module's query_* which call sd — rebind
    monkeypatch.setattr(devices, "query_devices", lambda: devs)
    monkeypatch.setattr(devices, "query_hostapis", lambda: hostapis)
    monkeypatch.setattr(devices, "default_input_index", lambda: 0)
    monkeypatch.setattr(devices, "default_output_index", lambda: 1)
    monkeypatch.setattr(devices.sd, "query_devices", query_devices)

    yield devices, devs

    sys.modules.pop("livelingo.devices", None)
    if saved_sd is not None:
        sys.modules["sounddevice"] = saved_sd
    else:
        sys.modules.pop("sounddevice", None)
    if saved_dev is not None:
        sys.modules["livelingo.devices"] = saved_dev


def test_resolve_empty_input_uses_default(devices_mod):
    devices, _ = devices_mod
    idx, name = devices.resolve_device("", "input")
    assert idx == 0
    assert isinstance(name, str)


def test_resolve_numeric_index(devices_mod):
    devices, _ = devices_mod
    idx, name = devices.resolve_device("2", "output")
    assert idx == 2
    assert "CABLE" in name


def test_resolve_numeric_wrong_kind(devices_mod):
    devices, _ = devices_mod
    with pytest.raises(ValueError, match="no input"):
        devices.resolve_device("1", "input")


def test_resolve_by_substring_prefers_mme(devices_mod):
    devices, _ = devices_mod
    idx, name = devices.resolve_device("CABLE Input", "output")
    assert "CABLE" in name
    assert idx == 2


def test_resolve_missing_name(devices_mod):
    devices, _ = devices_mod
    with pytest.raises(ValueError, match="No output device"):
        devices.resolve_device("NoSuchDeviceXYZ", "output")


def test_find_vbcable_output(devices_mod):
    devices, _ = devices_mod
    idx, name = devices.find_vbcable_output()
    assert idx is not None
    assert "CABLE" in name


def test_find_vbcable_missing(devices_mod, monkeypatch):
    devices, _ = devices_mod
    monkeypatch.setattr(devices, "query_devices", lambda: [_dev("Speakers", out_ch=2)])
    assert devices.find_vbcable_output() == (None, None)


def test_matches_kind(devices_mod):
    devices, _ = devices_mod
    assert devices._matches_kind(_dev("m", in_ch=1), "input")
    assert not devices._matches_kind(_dev("m", out_ch=1), "input")
    assert devices._matches_kind(_dev("s", out_ch=2), "output")
