"""Persist / restore Textual theme name under ``.cache/tui_theme.txt``."""

from __future__ import annotations

from pathlib import Path

import livelingo.tui_app as tui_app


def test_save_and_load_tui_theme_name(tmp_path, monkeypatch):
    path = tmp_path / "tui_theme.txt"
    monkeypatch.setattr(tui_app, "_TUI_THEME_PATH", str(path))
    assert tui_app._load_tui_theme_name() is None

    tui_app._save_tui_theme_name("  nord  ")
    assert path.read_text(encoding="utf-8").strip() == "nord"
    assert tui_app._load_tui_theme_name() == "nord"


def test_save_ignores_empty(tmp_path, monkeypatch):
    path = tmp_path / "tui_theme.txt"
    monkeypatch.setattr(tui_app, "_TUI_THEME_PATH", str(path))
    tui_app._save_tui_theme_name("")
    assert not path.exists()


def test_load_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tui_app, "_TUI_THEME_PATH", str(tmp_path / "missing" / "tui_theme.txt")
    )
    assert tui_app._load_tui_theme_name() is None
