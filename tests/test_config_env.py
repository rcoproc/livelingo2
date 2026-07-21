"""Unit tests for config env parsers (without reloading full config module)."""

from __future__ import annotations

import config as cfg_mod


def test_get_str(monkeypatch):
    monkeypatch.setenv("LL_TEST_STR", "  hello  ")
    assert cfg_mod._get_str("LL_TEST_STR", "def") == "hello"
    monkeypatch.delenv("LL_TEST_STR", raising=False)
    assert cfg_mod._get_str("LL_TEST_STR", "def") == "def"
    monkeypatch.setenv("LL_TEST_STR", "   ")
    assert cfg_mod._get_str("LL_TEST_STR", "def") == "def"


def test_get_int(monkeypatch):
    monkeypatch.setenv("LL_TEST_INT", "42")
    assert cfg_mod._get_int("LL_TEST_INT", 0) == 42
    monkeypatch.setenv("LL_TEST_INT", "nope")
    assert cfg_mod._get_int("LL_TEST_INT", 7) == 7
    monkeypatch.delenv("LL_TEST_INT", raising=False)
    assert cfg_mod._get_int("LL_TEST_INT", 7) == 7


def test_get_float(monkeypatch):
    monkeypatch.setenv("LL_TEST_F", "3.5")
    assert cfg_mod._get_float("LL_TEST_F", 0.0) == 3.5
    monkeypatch.setenv("LL_TEST_F", "x")
    assert cfg_mod._get_float("LL_TEST_F", 1.1) == 1.1


def test_get_bool(monkeypatch):
    for val in ("1", "true", "YES", "on", "Y"):
        monkeypatch.setenv("LL_TEST_B", val)
        assert cfg_mod._get_bool("LL_TEST_B", False) is True
    for val in ("0", "false", "no", "off"):
        monkeypatch.setenv("LL_TEST_B", val)
        assert cfg_mod._get_bool("LL_TEST_B", True) is False
    monkeypatch.delenv("LL_TEST_B", raising=False)
    assert cfg_mod._get_bool("LL_TEST_B", True) is True


def test_essential_config_types():
    assert isinstance(cfg_mod.SOURCE_LANG, str)
    assert isinstance(cfg_mod.TARGET_LANG, str)
    assert isinstance(cfg_mod.SAMPLE_RATE, int)
    assert cfg_mod.SAMPLE_RATE > 0
