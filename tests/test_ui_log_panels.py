"""Unit tests for log panel routing, dual-pane geometry, and audio lines."""

from __future__ import annotations

from livelingo import ui


def test_normalize_panel_aliases():
    assert ui._normalize_panel("main") == "main"
    assert ui._normalize_panel("VOZ") == "main"
    assert ui._normalize_panel("lc") == "lc"
    assert ui._normalize_panel("LiveCaptions") == "lc"
    assert ui._normalize_panel("captions") == "lc"
    assert ui._normalize_panel("caption") == "lc"
    assert ui._normalize_panel("main-lc") == "lc"
    assert ui._normalize_panel("app") == "app"
    assert ui._normalize_panel("sistema") == "app"
    assert ui._normalize_panel("system") == "app"
    assert ui._normalize_panel("") == "main"
    assert ui._normalize_panel(None) == "main"  # type: ignore[arg-type]


def test_log_panel_context_forces_lc():
    captured: list[tuple[str, str, str]] = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        with ui.log_panel("lc"):
            ui.info("hello-lc")
        ui.info("hello-main")
    finally:
        ui.set_log_sink(prev)

    assert any(p == "lc" and "hello-lc" in t for _, t, p in captured)
    assert any(p == "main" and "hello-main" in t for _, t, p in captured)


def test_rail_geometry_no_pad_when_tui_sink():
    """TUI has real LC|VOZ widgets — geometry must not mid-screen-shift VOZ."""
    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(lambda *a, **k: None)
        pad, content_w, left_w, right_w, left_shift, right_shift = ui._rail_geometry(
            margin=3
        )
        assert pad == "   "
        assert left_w == content_w
        assert right_w == content_w
        assert left_shift == ""
        assert right_shift == ""
    finally:
        ui.set_log_sink(prev)


def test_rail_geometry_classic_dual_rail_without_sink():
    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(None)
        pad, content_w, left_w, right_w, left_shift, right_shift = ui._rail_geometry(
            margin=0
        )
        assert pad == ""
        assert left_w >= 28
        assert right_w >= 28
        assert left_shift == ""
        # Classic VOZ is space-padded into the right half (nudge may expand wrap budget)
        assert isinstance(right_shift, str)
        if content_w >= 60:
            assert len(right_shift) > 0
            assert right_shift == " " * len(right_shift)
    finally:
        ui.set_log_sink(prev)


def test_format_audio_lines_empty_path_omits_line():
    assert ui.format_audio_lines("") == []
    assert ui.format_audio_lines(None) == []  # type: ignore[arg-type]
    assert ui.format_audio_lines("   ") == []


def test_format_audio_lines_path_only_no_missing_note(tmp_path):
    path = tmp_path / "chunk_1.wav"
    # Path not on disk yet — still one line with full path, no second "missing" line
    lines = ui.format_audio_lines(str(path))
    assert len(lines) == 1
    assert lines[0].startswith("audio: ")
    assert str(path) in lines[0] or path.name in lines[0]
    # No "r / rN" / "not generated" clutter
    joined = "\n".join(lines).lower()
    assert "not generated" not in joined
    assert "r / rn" not in joined
    assert "missing" not in joined or "missing" in str(path).lower()


def test_format_audio_lines_pending_write_shows_path(tmp_path):
    path = tmp_path / "chunk_2.wav"
    lines = ui.format_audio_lines(str(path), pending_write=True)
    assert len(lines) == 1
    assert "audio:" in lines[0]


def test_format_timing_line_includes_engine_and_first_chunk_ms():
    from livelingo import ui

    line = ui.format_timing_line(
        {
            "stt": 0.5,
            "translate": 0.2,
            "tts": 1.2,
            "tts_engine": "piper",
            "tts_voice": "en_US-lessac-low",
            "tts_first_ms": 58,
            "total": 1.9,
        },
        include_clock=False,
    )
    assert "engine=piper(en_US-lessac-low)" in line
    assert "first_chunk 58ms" in line
    assert "TTS 1.20s" in line


def test_format_sistema_status_shape(monkeypatch):
    import config as cfg
    from livelingo import ui

    monkeypatch.setattr(cfg, "SOURCE_LANG", "pt", raising=False)
    monkeypatch.setattr(cfg, "TARGET_LANG", "en", raising=False)
    monkeypatch.setattr(cfg, "TTS_ENGINE", "hybrid", raising=False)
    monkeypatch.setattr(cfg, "PIPER_VOICE", "auto:en", raising=False)

    class _Pipe:
        def is_sound_enabled(self):
            return True

        def is_mic_muted(self):
            return True

    line = ui.format_sistema_status(_Pipe())
    assert "Languages: pt -> en" in line
    assert "TTS: hybrid" in line
    assert "Sound: ON" in line
    assert "Mic: MUTED" in line


def test_begin_chunk_sistema_clears_app_and_prints_status(monkeypatch):
    """Each chunk: clear Sistema, keep status, then chunk header."""
    from livelingo import ui

    captured: list[tuple[str, str, str]] = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    class _Pipe:
        def is_sound_enabled(self):
            return False

        def is_mic_muted(self):
            return False

    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        ui.begin_chunk_sistema(7, pipeline=_Pipe())
    finally:
        ui.set_log_sink(prev)

    assert any(k == "clear" and p == "app" for k, _, p in captured), captured
    assert any(
        p == "app" and "Languages:" in t and "Mic:" in t for _, t, p in captured
    ), captured
    assert any(p == "app" and "[chunk 7]" in t for _, t, p in captured), captured


def test_live_caption_block_emits_to_lc_panel():
    captured: list[tuple[str, str, str]] = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        ui.live_caption_block(3, "Hello world", "Olá mundo", from_cache=False)
    finally:
        ui.set_log_sink(prev)

    assert captured, "expected LC lines via sink"
    assert all(p == "lc" for _, _, p in captured), captured
    body = "\n".join(t for _, t, _ in captured)
    assert "Hello world" in body or "Caption" in body or "LC 3" in body
    assert "Olá mundo" in body or "Translated" in body


def test_ui_lang_code_br_aliases(monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg, "SOURCE_LANG", "br", raising=False)
    assert ui._ui_lang_code() == "pt"
    monkeypatch.setattr(cfg, "SOURCE_LANG", "bra", raising=False)
    assert ui._ui_lang_code() == "pt"
    monkeypatch.setattr(cfg, "SOURCE_LANG", "pt-BR", raising=False)
    assert ui._ui_lang_code() == "pt"
    # back-compat alias still works
    assert ui._target_lang_code() == ui._ui_lang_code()
