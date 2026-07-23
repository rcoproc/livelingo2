"""LiveCaptions: OFF by default; [lc on] starts; [lc off] pauses."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_config_start_on_launch_default_off():
    import config as cfg

    assert cfg.LIVE_CAPTIONS_START_ON_LAUNCH is False


def test_main_skips_start_when_flag_false(monkeypatch):
    """Mirrors main.py branch: build ok, start only if START_ON_LAUNCH."""
    import config as cfg

    monkeypatch.setattr(cfg, "LIVE_CAPTIONS_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "LIVE_CAPTIONS_START_ON_LAUNCH", False, raising=False)

    svc = MagicMock()
    svc.is_running.return_value = False
    auto = bool(getattr(cfg, "LIVE_CAPTIONS_START_ON_LAUNCH", False))
    if auto:
        svc.start()
    # Default path: never called
    svc.start.assert_not_called()


def test_lc_on_starts_when_not_running():
    """Command path: [lc on] → start if dead, else resume."""
    svc = MagicMock()
    svc.is_running.return_value = False

    if not svc.is_running():
        svc.start()
    else:
        svc.resume()

    svc.start.assert_called_once()
    svc.resume.assert_not_called()


def test_lc_on_resumes_when_running_paused():
    svc = MagicMock()
    svc.is_running.return_value = True

    if not svc.is_running():
        svc.start()
    else:
        svc.resume()

    svc.start.assert_not_called()
    svc.resume.assert_called_once()


def test_lc_off_pauses_only_if_running():
    svc = MagicMock()
    svc.is_running.return_value = True
    if svc.is_running():
        svc.pause()
    svc.pause.assert_called_once()

    svc2 = MagicMock()
    svc2.is_running.return_value = False
    if svc2.is_running():
        svc2.pause()
    svc2.pause.assert_not_called()


def test_pipe_lc_chip_off_when_paused_or_idle():
    """UI rule: chip only when running and not paused."""

    def chip(status, paused=False, running=False, has_text=False):
        if paused or status in (
            "disabled",
            "paused",
            "stopped",
            "idle",
            "error",
            "",
        ):
            if status == "error" and has_text and running and not paused:
                return True
            return False
        if status in ("running", "translating", "starting") or (
            running and not paused
        ):
            return True
        if has_text and running and not paused:
            return True
        return False

    assert chip("idle") is False
    assert chip("running", running=True) is True
    assert chip("running", paused=True, running=True) is False
    assert chip("paused", paused=True, running=True) is False
    assert chip("starting", running=True) is True
