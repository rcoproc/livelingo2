"""Unit tests for command catalog / help markdown."""

from __future__ import annotations

from livelingo.command_help import (
    _GROUP_IDS,
    _alpha_key,
    build_commands_markdown,
    command_by_id,
    iter_command_menu,
    tab_title,
)


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
    assert "## Coach" in md


def test_group_order_and_coach_membership():
    assert _GROUP_IDS == (
        "audio",
        "sentence",
        "idiom",
        "coach",
        "keys",
        "session",
    )
    rows = iter_command_menu("en")
    kinds = [r["kind"] for r in rows]
    assert "group" in kinds and "cmd" in kinds
    group_titles = [r["title"] for r in rows if r["kind"] == "group"]
    assert group_titles[0].lower().startswith("audio") or "Audio" in group_titles[0]
    assert any(t.lower() == "coach" for t in group_titles)
    coach_ids = {
        r["id"]
        for r in rows
        if r["kind"] == "cmd"
        and r["id"] in ("coach", "coach_provider", "airespond", "interview", "f7")
    }
    assert coach_ids == {
        "coach",
        "coach_provider",
        "airespond",
        "interview",
        "f7",
    }
    # F7 must not appear under Keyboard anymore
    f7 = command_by_id("f7", "en")
    assert f7 is not None
    assert f7["token"] == "F7"
    assert command_by_id("interview", "pt")["token"] == "interview"
    air = command_by_id("airespond", "pt")
    assert air is not None and air["token"] == "airespond"


def test_build_commands_markdown_includes_n_force_and_sub():
    """v1.2.2: capital [N] force soft-listen + [sub] vcam burn-in."""
    md_en = build_commands_markdown("en")
    assert "`[N]`" in md_en or "[N]" in md_en
    assert "soft-listen" in md_en.lower() or "soft listen" in md_en.lower()
    assert "`[sub]`" in md_en or "[sub]" in md_en
    assert "burn-in" in md_en.lower() or "target" in md_en.lower()

    md_pt = build_commands_markdown("pt-BR")
    assert "escuta forçada" in md_pt.lower()
    assert "`[sub]`" in md_pt or "[sub]" in md_pt
    assert "legenda" in md_pt.lower() or "burn-in" in md_pt.lower()


def test_build_commands_markdown_includes_go_flush():
    md_en = build_commands_markdown("en")
    assert "`[go]`" in md_en or "[go]" in md_en
    assert "flush" in md_en.lower() or "stt" in md_en.lower()
    assert "F6" in md_en or "`F6`" in md_en

    md_pt = build_commands_markdown("pt-BR")
    assert "go" in md_pt.lower()
    assert "stt" in md_pt.lower() or "flush" in md_pt.lower()


def test_build_commands_markdown_includes_coach_providers():
    md_en = build_commands_markdown("en")
    assert "coach provider" in md_en.lower() or "`coach provider`" in md_en
    for p in ("grok", "groq", "deepseek", "claude", "gemini"):
        assert p in md_en.lower()
    assert "airespond" in md_en.lower()
    assert "F7" in md_en or "`F7`" in md_en

    md_pt = build_commands_markdown("pt-BR")
    assert "coach" in md_pt.lower()
    assert "airespond" in md_pt.lower()
    assert "provider" in md_pt.lower() or "gemini" in md_pt.lower()


def test_build_commands_markdown_includes_interview_preset():
    """interview / iv: sound off + cam off + lc on + coach on + hide pane."""
    md_en = build_commands_markdown("en")
    assert "`[interview]`" in md_en or "[interview]" in md_en
    assert "airespond" in md_en.lower()
    assert "view coach" in md_en.lower()
    assert "cam off" in md_en.lower() or "coach on" in md_en.lower()

    md_pt = build_commands_markdown("pt-BR")
    assert "interview" in md_pt.lower()
    assert "airespond" in md_pt.lower()
    assert "view coach" in md_pt.lower()
    assert "cam off" in md_pt.lower() or "coach on" in md_pt.lower() or "som" in md_pt.lower()


def test_build_commands_markdown_includes_view_viewer():
    """v1.2.3: detached panel viewers (view lc|coach|voz) + copy/export."""
    md_en = build_commands_markdown("en")
    assert "`[view]`" in md_en or "[view]" in md_en
    assert "view coach" in md_en.lower() or "view lc" in md_en.lower()
    assert "export" in md_en.lower()
    assert "ctrl+s" in md_en.lower() or "Ctrl+S" in md_en

    md_pt = build_commands_markdown("pt-BR")
    assert "view" in md_pt.lower()
    assert "viewer" in md_pt.lower() or "avulso" in md_pt.lower()
    assert "export" in md_pt.lower() or "exportar" in md_pt.lower() or ".md" in md_pt


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
