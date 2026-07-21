"""Unit tests for command catalog / help markdown."""

from __future__ import annotations

from livelingo.command_help import _alpha_key, build_commands_markdown, tab_title


def test_alpha_key_strips_accents():
    assert _alpha_key("Áudio") == "audio"
    assert _alpha_key("Sessão") == "sessao"


def test_tab_title_en_and_pt():
    en = tab_title("en")
    pt = tab_title("pt")
    assert isinstance(en, str) and en
    assert isinstance(pt, str) and pt


def test_build_commands_markdown_en_has_core_commands():
    md = build_commands_markdown("en")
    assert md.startswith("#")
    assert "`[n]`" in md or "[n]" in md
    assert "`[s]`" in md or "[s]" in md
    assert "`[g]`" in md or "[g]" in md
    # Groups present
    assert "##" in md


def test_build_commands_markdown_pt_localized():
    md = build_commands_markdown("pt-BR")
    assert isinstance(md, str)
    assert len(md) > 100
