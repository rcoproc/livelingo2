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


def test_build_commands_markdown_includes_cls_sides():
    """Tradução split: cls clears all; cls1=LC left; cls2=VOZ right."""
    md_en = build_commands_markdown("en")
    assert "`[cls]`" in md_en or "[cls]" in md_en
    assert "`[cls1]`" in md_en or "[cls1]" in md_en
    assert "`[cls2]`" in md_en or "[cls2]" in md_en
    # Descriptions should mention LC / VOZ columns
    assert "LC" in md_en and "VOZ" in md_en

    md_pt = build_commands_markdown("pt-BR")
    assert "cls1" in md_pt and "cls2" in md_pt
    assert "esquerda" in md_pt.lower() or "LC" in md_pt


def test_build_commands_markdown_search_mentions_focused_pane():
    md = build_commands_markdown("en")
    lower = md.lower()
    assert "focused" in lower or "lc" in lower
    assert "voz" in lower


def test_build_commands_markdown_pt_localized():
    md = build_commands_markdown("pt-BR")
    assert isinstance(md, str)
    assert len(md) > 100
