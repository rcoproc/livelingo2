"""
ui.py
=====
Tiny helpers for colored, readable terminal output. Uses colorama so the ANSI
colors work on every Windows console (legacy conhost included).
"""

import os
import sys
import threading

from colorama import Fore, Style, init

# autoreset=True -> every print resets the color automatically afterwards.
init(autoreset=True)

# Serialize all terminal writes — sound-OFF parallel workers otherwise interleave
# Heard/Translated lines with filter messages and corrupt the display.
_print_lock = threading.RLock()

# Optional TUI sink: callable(kind, text, panel="main") — when set, prints go there.
# panel: "main" (tradução / comandos) | "app" (etapas técnicas, timestamps, debug).
_log_sink = None
# Optional width provider: callable() -> int (usable columns inside the log panel).
_width_provider = None
# Temporary panel override (e.g. F1 help → Sistema tab). Nested via stack.
_panel_override_stack: list = []
# Optional pipeline stage sink: callable(stage: str, meta: dict) for TUI pipe bar.
# Stages: idle | mic | stt | translate | tts | play | lc | lc_idle
_pipeline_stage_sink = None


def set_log_sink(sink):
    """Route ui.* output to a TUI log (or None to restore classic terminal)."""
    global _log_sink
    with _print_lock:
        _log_sink = sink


def get_log_sink():
    return _log_sink


def set_pipeline_stage_sink(sink):
    """Route pipeline stage changes to the TUI activity bar (or None)."""
    global _pipeline_stage_sink
    with _print_lock:
        _pipeline_stage_sink = sink


def get_pipeline_stage_sink():
    return _pipeline_stage_sink


def pipeline_stage(stage: str, **meta):
    """
    Notify the TUI of a VOZ pipeline step (non-blocking, best-effort).

    stage: idle | mic | stt | translate | tts | play
    Optional meta: chunk, detail, source ("voz"|"lc")
    """
    cb = _pipeline_stage_sink
    if cb is None:
        return
    try:
        cb(str(stage or "idle"), dict(meta) if meta else {})
    except Exception:
        pass


def set_width_provider(provider):
    """Optional callable returning usable content width (TUI log panel columns)."""
    global _width_provider
    with _print_lock:
        _width_provider = provider


def get_width_provider():
    return _width_provider


class log_panel:
    """
    Context manager: force all ui.* emissions to a TUI panel.

    Example (F1 help → Sistema)::
        with ui.log_panel("app"):
            ui.info("help…")
    """

    def __init__(self, panel: str = "app"):
        self.panel = "app" if str(panel or "main").lower() == "app" else "main"

    def __enter__(self):
        with _print_lock:
            _panel_override_stack.append(self.panel)
        return self

    def __exit__(self, exc_type, exc, tb):
        with _print_lock:
            if _panel_override_stack:
                _panel_override_stack.pop()
        return False


def _effective_panel(panel: str = "main") -> str:
    """Resolve panel, honoring log_panel() override when set."""
    with _print_lock:
        if _panel_override_stack:
            return _panel_override_stack[-1]
    return "app" if str(panel or "main").lower() == "app" else "main"


def _emit(kind, text, panel="main"):
    """kind: info|success|warn|error|dim|raw|rich|list; panel: main|app"""
    sink = _log_sink
    if sink is not None:
        panel = _effective_panel(panel)
        try:
            sink(kind, text, panel)
            return True
        except TypeError:
            # Older 2-arg sinks (tests / classic adapters)
            try:
                sink(kind, text)
                return True
            except Exception:
                return False
        except Exception:
            return False
    return False


__all__ = [
    "banner",
    "info",
    "success",
    "warn",
    "error",
    "dim",
    "raw",
    "rich",
    "device_line",
    "chunk_status",
    "chunk_timings",
    "chunk_progress",
    "listen_progress",
    "pipeline_stage",
    "format_timing_line",
    "clock_hhmmss",
    "format_recorded_stamp",
    "resolve_share_path",
    "format_audio_lines",
    "print_audio_ref",
    "chunk_text_preview",
    "chunk_stream_start",
    "chunk_stream_update",
    "chunk_stream_done",
    "synonyms_result",
    "favorites_popup",
    "set_log_sink",
    "get_log_sink",
    "set_pipeline_stage_sink",
    "get_pipeline_stage_sink",
    "set_width_provider",
    "get_width_provider",
    "log_panel",
    "content_width",
    "panel_width",
    "rule_line",
]

# Map chunk_progress stages → pipe-bar stage ids
_CHUNK_TO_PIPE = {
    "stt": "stt",
    "heard": "stt",
    "translate": "translate",
    "translated": "translate",
    "tts": "tts",
    "tts_bg": "tts",
    "ready": "play",  # audio path will refine to play; sound-off → idle soon
    "ready_text": "idle",
}

# Pipeline UX stages (always shown — not VERBOSE-only).
_CHUNK_PROGRESS = {
    "stt": ("📝", "Transcrevendo (voz → texto)…"),
    "heard": ("📝", "Texto original pronto"),
    "translate": ("🌐", "Traduzindo…"),
    "translated": ("🌐", "Tradução pronta"),
    "tts": ("🔊", "Gerando voz traduzida…"),
    "tts_bg": ("🔊", "Gerando voz em segundo plano…"),
    "ready": ("✅", "Pronto — tradução completa"),
    "ready_text": ("✅", "Pronto — texto completo (sem áudio ao vivo)"),
}


def _term_width():
    try:
        return max(40, os.get_terminal_size().columns)
    except OSError:
        # TUI often has no usable tty size — prefer a wide default over 80.
        return 140 if _log_sink is not None else 80


def panel_width():
    """
    Full columns available for one log line (matches TUI RichLog bake width).

    Prefer the width provider (cached live panel). Fall back to terminal with
    chrome reserved for header/tabs/footer/scrollbar.
    """
    provider = _width_provider
    if provider is not None:
        try:
            w = int(provider())
            if w >= 24:
                return w
        except Exception:
            pass
    try:
        term_w = max(40, os.get_terminal_size().columns)
    except OSError:
        term_w = 140 if _log_sink is not None else 80
    # TUI chrome (header, tabs, borders, scrollbar) eats more than classic
    reserve = 14 if _log_sink is not None else 2
    return max(40, term_w - reserve)


def content_width(margin=3, chrome=0):
    """
    Usable columns for list/menu *body* after a left margin.

    pad(margin) + body(content_width) must fit in panel_width() so RichLog
    does not re-wrap and break hang-indents / rules (=== headers).
    """
    gutter = max(0, int(chrome or 0))
    # Always leave 1 col spare so a full-width "====" rule never wraps to "=="
    gutter = max(gutter, 1 if _log_sink is not None else 0)
    m = max(0, int(margin or 0))
    return max(24, panel_width() - m - gutter)


def rule_line(width=None, char="=", margin=3):
    """
    Horizontal rule that fits the log panel after `margin` spaces.

    width: body width (defaults to content_width(margin)). Never exceeds panel.
    """
    m = max(0, int(margin or 0))
    if width is None:
        width = content_width(margin=m)
    # Clamp hard to panel so pad + rule never exceeds bake width
    max_body = max(8, panel_width() - m - 1)
    n = max(8, min(int(width), max_body))
    ch = (char or "=")[:1]
    return ch * n


def _one_line(text, budget):
    """Collapse whitespace and truncate so the string fits one terminal line."""
    text = " ".join((text or "").split())
    if budget < 2:
        return "…"
    if len(text) <= budget:
        return text
    return text[: budget - 1] + "…"


def _display_lang_label(code: str) -> str:
    """Short UI label: pt → BR, else upper code."""
    c = (code or "?").lower().strip()
    if "-" in c:
        c = c.split("-", 1)[0]
    if c in ("pt", "por", "pt-br", "pt_br"):
        return "BR"
    return (c or "?").upper()


def _voz_lang_pair():
    """(src_label, tgt_label) for live mic/VOZ chunks from config."""
    try:
        import config as cfg

        src = getattr(cfg, "SOURCE_LANG", "?")
        tgt = getattr(cfg, "TARGET_LANG", "?")
    except Exception:
        src, tgt = "?", "?"
    return _display_lang_label(src), _display_lang_label(tgt)


# VOZ (mic) blocks sit on the right rail; nudge left so they are not flush
# against the far right edge (user: ~15 cols more to the left).
_VOZ_RAIL_LEFT_NUDGE = 15


def _rail_geometry(margin=3):
    """
    Dual-rail geometry matching list `l`: LC left · VOZ right.

    Returns (pad, content_w, left_w, right_w, left_shift, right_shift).
    VOZ starts mid-screen minus `_VOZ_RAIL_LEFT_NUDGE` columns.
    """
    m = max(0, int(margin or 0))
    pad = " " * m
    content_w = content_width(margin=m)
    gutter = 1
    left_w = max(28, (content_w - gutter) // 2)
    # Shift VOZ ~15 cols left; reclaim that width for wrapping budget
    shift_cols = max(0, left_w + gutter - _VOZ_RAIL_LEFT_NUDGE)
    right_w = max(28, content_w - shift_cols)
    left_shift = ""
    right_shift = " " * shift_cols
    return pad, content_w, left_w, right_w, left_shift, right_shift


def _wrap_labeled_body(label_plain: str, body: str, width: int) -> list:
    """
    Wrap body so first line fits after label; later lines align under body.
    `width` = full line budget for label+body (rail column width).
    """
    body = " ".join((body or "").split())
    if not body:
        return [""]
    width = max(8, int(width))
    limit = max(8, width - len(label_plain))
    words = body.split(" ")
    lines = []
    cur = ""
    for w in words:
        trial = w if not cur else f"{cur} {w}"
        if len(trial) <= limit:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            while len(w) > limit:
                lines.append(w[:limit])
                w = w[limit:]
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _wrap_path_body(label_plain: str, path: str, width: int) -> list:
    """
    Wrap a filesystem path under ``label_plain`` (e.g. ``audio: ``).

    Prefers breaks after ``\\`` / ``/`` so folder names stay intact. Never
    inserts middle ellipsis — the full path is always present across lines.
    """
    path = (path or "").strip()
    if not path:
        return [""]
    width = max(8, int(width))
    limit = max(8, width - len(label_plain or ""))
    # Tokenize keeping separators attached to the preceding segment
    segs: list[str] = []
    buf = ""
    for ch in path:
        buf += ch
        if ch in ("\\", "/"):
            segs.append(buf)
            buf = ""
    if buf:
        segs.append(buf)
    if not segs:
        return [path]

    lines: list[str] = []
    cur = ""
    for seg in segs:
        trial = cur + seg
        if len(trial) <= limit:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        # Single segment longer than limit (rare) — hard-split, keep all chars
        while len(seg) > limit:
            lines.append(seg[:limit])
            seg = seg[limit:]
        cur = seg
    if cur:
        lines.append(cur)
    return lines or [path]


def _audio_display_pieces(text: str, budget: int) -> list[str]:
    """
    Split an ``audio: <path>`` line into right-rail pieces (full path, no …).
    """
    text = (text or "").strip()
    if not text:
        return []
    label = "audio: "
    if text.lower().startswith("audio: "):
        label = text[:7]
        body = text[7:]
    elif text.lower().startswith("audio:"):
        body = text[len("audio:") :].lstrip()
        label = "audio: "
    else:
        return _wrap_labeled_body("", text, budget)

    parts = _wrap_path_body(label, body, budget)
    out = []
    for i, part in enumerate(parts):
        if i == 0:
            out.append(f"{label}{part}")
        else:
            out.append(f"{' ' * len(label)}{part}")
    return out


def _pad(indent):
    """Left margin spaces (e.g. menu/list blocks use indent=3)."""
    try:
        n = int(indent or 0)
    except (TypeError, ValueError):
        n = 0
    return " " * max(0, n)


def banner(indent=3):
    """Print the startup banner (default 3-char left margin)."""
    pad = _pad(indent)
    # Classic print indents title block with 8 extra spaces after pad.
    title_pad = pad + "        "
    line = "=" * 64
    with _print_lock:
        if _log_sink is not None:
            # TUI: keep same left margin as classic; no [i] prefix on art lines.
            _emit("rich", f"{pad}[cyan]{line}[/]")
            _emit(
                "rich",
                f"{title_pad}[bold cyan]L I V E L I N G O   🎙️  ->  🌍[/]",
            )
            _emit(
                "rich",
                f"{title_pad}[cyan]Real-time speech translation into a virtual mic[/]",
            )
            _emit(
                "rich",
                f"{title_pad}[cyan]mic -> Whisper -> translate -> Edge TTS -> VB-Cable[/]",
            )
            _emit("rich", f"{pad}[cyan]{line}[/]")
            return
        print(pad + Fore.CYAN + line)
        print(
            pad
            + Fore.CYAN
            + Style.BRIGHT
            + "        L I V E L I N G O   \U0001f399️  ->  \U0001f30d"
        )
        print(
            pad + Fore.CYAN + "        Real-time speech translation into a virtual mic"
        )
        print(
            pad
            + Fore.CYAN
            + "        mic -> Whisper -> translate -> Edge TTS -> VB-Cable"
        )
        print(pad + Fore.CYAN + line + Style.RESET_ALL)


def info(msg, indent=0, panel="main"):
    text = str(msg)
    with _print_lock:
        # TUI sink must get the same left margin as classic prints.
        if _emit("info", _pad(indent) + text, panel=panel):
            return
        print("\r\033[K" + _pad(indent) + Fore.CYAN + "[i] " + Style.RESET_ALL + text)


def success(msg, indent=0, panel="main"):
    text = str(msg)
    with _print_lock:
        if _emit("success", _pad(indent) + text, panel=panel):
            return
        print("\r\033[K" + _pad(indent) + Fore.GREEN + "[ok] " + Style.RESET_ALL + text)


def warn(msg, indent=0, panel="main"):
    text = str(msg)
    with _print_lock:
        if _emit("warn", _pad(indent) + text, panel=panel):
            return
        print("\r\033[K" + _pad(indent) + Fore.YELLOW + "[!] " + Style.RESET_ALL + text)


def error(msg, indent=0, panel="main"):
    text = str(msg)
    with _print_lock:
        if _emit("error", _pad(indent) + text, panel=panel):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.RED
            + Style.BRIGHT
            + "[x] "
            + Style.RESET_ALL
            + Fore.RED
            + text
        )


def dim(msg, indent=0, panel="main"):
    text = str(msg)
    # Verbose/debug pipeline chatter → technical panel (keep Tradução clean)
    if panel == "main" and "[debug]" in text:
        panel = "app"
    with _print_lock:
        if _emit("dim", _pad(indent) + text, panel=panel):
            return
        print("\r\033[K" + _pad(indent) + Style.DIM + text + Style.RESET_ALL)


def raw(msg, indent=0, panel="main"):
    """Plain log line (no [ok]/[i] prefix). Prefer for multi-line list blocks in TUI."""
    text = _pad(indent) + str(msg) if indent else str(msg)
    with _print_lock:
        if _emit("raw", text, panel=panel):
            return
        print("\r\033[K" + text)


# Debounce listen log lines (noise can flip VAD many times per second).
_listen_log_state = {"active": None, "t": 0.0}
_LISTEN_LOG_MIN_GAP = 0.9  # seconds between identical spam lines

# High-res clocks for stage timing (compare latency across pipeline steps).
_progress_timing = {
    "listen_mono": None,  # perf_counter at last "Escutando" start
    "listen_wall": None,  # HH:MM:SS.ffffff at that start
    "last_mono": None,  # perf_counter of previous stage line
}


def _stamp_us():
    """Wall clock with microseconds: HH:MM:SS.ffffff"""
    import datetime as _dt

    return _dt.datetime.now().strftime("%H:%M:%S.%f")


def _progress_clock_suffix(*, listen_start=False, listen_end=False):
    """
    Build " · @HH:MM:SS.ffffff · início=… · +X.XXXs desde escuta · ΔYms"
    for stage lines so users can compare times in the log.
    """
    import time as _time

    wall = _stamp_us()
    mono = _time.perf_counter()
    parts = [f"@{wall}"]

    if listen_start:
        _progress_timing["listen_mono"] = mono
        _progress_timing["listen_wall"] = wall
        _progress_timing["last_mono"] = mono
        parts.append(f"início={wall}")
        return " · " + " · ".join(parts)

    listen_wall = _progress_timing.get("listen_wall")
    listen_mono = _progress_timing.get("listen_mono")
    if listen_wall:
        parts.append(f"início={listen_wall}")
    if listen_mono is not None:
        elapsed = mono - float(listen_mono)
        parts.append(f"+{elapsed:.6f}s desde escuta")

    last = _progress_timing.get("last_mono")
    if last is not None:
        delta_s = mono - float(last)
        parts.append(f"Δ{delta_s * 1_000_000:.0f}µs")
    _progress_timing["last_mono"] = mono

    if listen_end:
        # Keep listen_* so chunk stages still show "desde escuta";
        # next listen_start will reset.
        pass

    return " · " + " · ".join(parts)


def listen_progress(active: bool):
    """
    VAD / mic listening state (no chunk number yet).
    active=True  → started hearing speech
    active=False → silence after speech (utterance closed)

    Rate-limited so laptop-mic noise does not flood the log.
    Includes wall clock (µs) and start time for latency comparison.
    """
    import time as _time

    now = _time.monotonic()
    active = bool(active)
    prev = _listen_log_state["active"]
    last_t = float(_listen_log_state["t"] or 0.0)
    # Skip duplicate state; also skip rapid flapping (noise blips).
    if prev is active and (now - last_t) < _LISTEN_LOG_MIN_GAP:
        return
    if prev is not None and prev is not active and (now - last_t) < 0.25:
        # Ignore sub-250ms false starts (click / brief spike)
        if active:
            return
    _listen_log_state["active"] = active
    _listen_log_state["t"] = now
    if active:
        suffix = _progress_clock_suffix(listen_start=True)
        # Etapas/timestamps → painel "app" da TUI (não polui tradução)
        dim(f"🎙️  Escutando voz…{suffix}", panel="app")
        pipeline_stage("mic", source="voz")
    else:
        suffix = _progress_clock_suffix(listen_end=True)
        dim(f"⏹️  Fim da fala — processando…{suffix}", panel="app")
        # Utterance closed — STT usually follows immediately
        pipeline_stage("stt", source="voz")


def _emit_chunk_progress_line(stage: str, text: str) -> None:
    """Route a stage line to the Sistema/app panel (or classic colors)."""
    if stage in ("ready", "ready_text", "translated", "heard"):
        if stage in ("ready", "ready_text"):
            success(text, panel="app")
        else:
            info(text, panel="app")
    else:
        dim(text, panel="app")


def chunk_progress(n, stage: str, detail: str = ""):
    """
    Pipeline stage for chunk N.

    Stages: stt | heard | translate | translated | tts | tts_bg | ready | ready_text
    Includes @HH:MM:SS.ffffff, início=…, +s desde escuta, Δµs.
    In TUI these go to the Sistema/app log panel (main keeps Heard/Translated only).

    Detail text is kept full under TUI (RichLog wraps). Classic terminal may
    soft-trim only when the line would wildly exceed the tty width.
    """
    icon, label = _CHUNK_PROGRESS.get(stage, ("•", str(stage)))
    prefix = f"[chunk {n}] "
    head = f"{prefix}{icon}  {label}"
    det = " ".join((detail or "").split()).strip()  # collapse newlines/spaces
    clocks = _progress_clock_suffix()

    # TUI pipe bar (Mic → STT → Trad → TTS → Out)
    pipe = _CHUNK_TO_PIPE.get(stage)
    if pipe:
        pipeline_stage(pipe, chunk=n, detail=det[:80] if det else "", source="voz")

    if det:
        # TUI Sistema: never hard-truncate with "…" — panel wraps with scrollbar.
        # Classic: only trim if absurdly long for the terminal width.
        if _log_sink is None:
            # Leave room for head + clocks + " — "
            budget = max(24, _term_width() - len(head) - len(clocks) - 8)
            if len(det) > budget:
                det = det[: max(1, budget - 1)] + "…"
        body = f"{head} — {det}"
    else:
        body = head

    # Long milestone lines: put clocks on the next dim line so wrap does not
    # split mid-timestamp (looked like "…3796" / next line "66").
    if _log_sink is not None and det and (len(body) + len(clocks)) > 90:
        _emit_chunk_progress_line(stage, body)
        dim(f"    {clocks.lstrip(' ·')}", panel="app")
        return

    _emit_chunk_progress_line(stage, f"{body}{clocks}")


def _rich_escape(text):
    """Escape user text for Rich markup (brackets etc.)."""
    try:
        from rich.markup import escape

        return escape(str(text) if text is not None else "")
    except Exception:
        return str(text or "").replace("[", "\\[")


def rich(msg, indent=0, panel="main"):
    """
    Log line with Rich markup (TUI only). Caller must escape user content via
    _rich_escape / rich.markup.escape. Classic terminal strips tags.
    """
    text = _pad(indent) + str(msg) if indent else str(msg)
    with _print_lock:
        if _emit("rich", text, panel=panel):
            return
        # Classic fallback: drop simple [style] tags
        import re

        plain = re.sub(r"\[/?[^\]]*\]", "", text)
        print("\r\033[K" + plain)


def device_line(role, index, name, indent=0):
    """Pretty one-liner confirming a selected device."""
    idx = "default" if index is None else f"#{index}"
    # Same visual as classic: 2 spaces + role + index + name (after [i] in TUI).
    line = f"  {role:<8} {idx:>8}  {name}"
    with _print_lock:
        if _emit("info", _pad(indent) + line):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.MAGENTA
            + f"  {role:<8}"
            + Style.RESET_ALL
            + f" {idx:>8}  "
            + Fore.WHITE
            + Style.BRIGHT
            + str(name)
        )


def _emit_chunk_blank(panel="main"):
    """Blank separator line (TUI raw or classic empty print)."""
    if _log_sink is not None:
        _emit("raw", "", panel=panel)
    else:
        print()


def _cache_badge(from_cache=None):
    """
    Optional CACHE/LIVE badge when phrase cache is in play.

    True  → [CACHE] (HIT)
    False → [LIVE]  (MISS / force / live path while cache on)
    None  → no badge (cache off / legacy)
    """
    if from_cache is True:
        return "[CACHE]"
    if from_cache is False:
        return "[LIVE]"
    return ""


def _emit_voz_chunk_block(n, heard, translated, *, from_cache=None, blank_after=True):
    """
    Emit a VOZ (mic) chunk on the **right rail**.

        <right> [Chunk N] BR: heard…          (SOURCE)
                          EN [CACHE]: tr…     (TARGET)

    Order always follows system SOURCE → TARGET (heard then translated).
    from_cache: True=CACHE (magenta), False=LIVE (cyan), None=no badge.
    """
    heard = (heard or "").strip()
    translated = (translated or "").strip()
    pad, _cw, _lw, right_w, _ls, right_shift = _rail_geometry(margin=3)
    src_l, tgt_l = _voz_lang_pair()
    badge = _cache_badge(from_cache)
    # Plain labels for wrap math: "EN [CACHE]: " or "EN: "
    if badge:
        tgt_plain = f"{tgt_l} {badge}: "
    else:
        tgt_plain = f"{tgt_l}: "
    prefix = f"[Chunk {n}] "
    # Source first (with chunk prefix), target second (hang-indent)
    lab_src = f"{prefix}{src_l}: "
    lab_tgt = f"{' ' * len(prefix)}{tgt_plain}"
    ind_src = " " * len(lab_src)
    ind_tgt = " " * len(lab_tgt)
    head = pad + right_shift

    with _print_lock:
        if _log_sink is not None:
            e = _rich_escape
            if from_cache is True:
                # Magenta — easy to spot HIT on Tradução tab
                tgt_lab_rich = f"[bold blue]{e(tgt_l)} [/][bold magenta]{e(badge)}: [/]"
            elif from_cache is False:
                # Cyan — live Google/LLM
                tgt_lab_rich = f"[bold blue]{e(tgt_l)} [/][bold cyan]{e(badge)}: [/]"
            else:
                tgt_lab_rich = f"[bold blue]{e(tgt_plain)}[/]"
            # 1) SOURCE (heard) — green
            for i, line in enumerate(_wrap_labeled_body(lab_src, heard, right_w)):
                if i == 0:
                    _emit(
                        "rich",
                        f"{head}"
                        f"[bold yellow]{e(prefix)}[/]"
                        f"[white]{e(src_l)}: [/]"
                        f"[green]{e(line)}[/]",
                    )
                else:
                    _emit(
                        "rich",
                        f"{head}{ind_src}[green]{e(line)}[/]",
                    )
            # 2) TARGET (translated) — white + optional CACHE/LIVE
            for i, line in enumerate(_wrap_labeled_body(lab_tgt, translated, right_w)):
                if i == 0:
                    _emit(
                        "rich",
                        f"{head}"
                        f"[white]{e(' ' * len(prefix))}[/]"
                        f"{tgt_lab_rich}"
                        f"[bold white]{e(line)}[/]",
                    )
                else:
                    _emit(
                        "rich",
                        f"{head}{ind_tgt}[bold white]{e(line)}[/]",
                    )
            if blank_after:
                _emit_chunk_blank()
            return

        if from_cache is True:
            badge_col = Fore.MAGENTA + Style.BRIGHT
        elif from_cache is False:
            badge_col = Fore.CYAN + Style.BRIGHT
        else:
            badge_col = Fore.BLUE + Style.BRIGHT
        # 1) SOURCE (heard)
        for i, line in enumerate(_wrap_labeled_body(lab_src, heard, right_w)):
            if i == 0:
                print(
                    "\r\033[K"
                    + head
                    + Fore.YELLOW
                    + Style.BRIGHT
                    + prefix
                    + Style.RESET_ALL
                    + Fore.WHITE
                    + f"{src_l}: "
                    + Fore.GREEN
                    + line
                    + Style.RESET_ALL
                )
            else:
                print("\r\033[K" + head + ind_src + Fore.GREEN + line + Style.RESET_ALL)
        # 2) TARGET (translated)
        for i, line in enumerate(_wrap_labeled_body(lab_tgt, translated, right_w)):
            if i == 0:
                if badge:
                    tgt_part = (
                        Fore.BLUE
                        + Style.BRIGHT
                        + f"{tgt_l} "
                        + badge_col
                        + f"{badge}: "
                        + Style.RESET_ALL
                    )
                else:
                    tgt_part = Fore.BLUE + Style.BRIGHT + f"{tgt_l}: " + Style.RESET_ALL
                print(
                    "\r\033[K"
                    + head
                    + (" " * len(prefix))
                    + tgt_part
                    + Fore.WHITE
                    + line
                    + Style.RESET_ALL
                )
            else:
                print("\r\033[K" + head + ind_tgt + Fore.WHITE + line + Style.RESET_ALL)
        if blank_after:
            print()


def _ellipsize_middle(text: str, max_len: int) -> str:
    """
    Fit text on one line: keep head + tail with a middle ellipsis.

    Paths keep more of the tail (filename) so ``chunk_N.wav`` stays readable.
    """
    text = text or ""
    max_len = max(4, int(max_len or 0))
    if len(text) <= max_len:
        return text
    if max_len <= 5:
        return text[: max_len - 1] + "…"
    # Tail-heavy: ~55% of budget for the end (filename / session folder)
    tail = max(8, (max_len * 55) // 100)
    head = max_len - tail - 1  # 1 for …
    if head < 3:
        return "…" + text[-(max_len - 1) :]
    return text[:head] + "…" + text[-tail:]


def _emit_voz_meta_lines(n, lines, *, style="dim", panel="main", nowrap=False):
    """
    Emit meta under a VOZ chunk, keeping the **right-rail** indent.

    style: dim | yellow  (matches list `l` VOZ rail)
    nowrap: if True, emit each line as-is (still full text — no middle
    ellipsis). If False, soft-wrap to the rail width and re-apply the right
    shift on every physical line so RichLog does not dump continuations at
    column 0.
    """
    if not lines:
        return
    pad, _cw, _lw, right_w, _ls, right_shift = _rail_geometry(margin=3)
    prefix = f"[Chunk {n}] "
    meta_indent = " " * len(prefix)
    head = pad + right_shift + meta_indent
    budget = max(12, right_w - len(meta_indent))
    with _print_lock:
        for text in lines:
            # Keep path separators (\, /); only collapse pure whitespace runs
            # when wrapping ordinary meta (not forced single-line).
            # Full text (paths keep every folder segment). Strip ends only —
            # never middle-ellipsis folder/file names.
            text = (text or "").strip()
            if not text:
                continue
            if nowrap:
                pieces = [text]
            elif text.lower().startswith("audio:"):
                pieces = _audio_display_pieces(text, budget)
            else:
                pieces = _wrap_labeled_body("", text, budget)
            for piece in pieces:
                if _log_sink is not None:
                    e = _rich_escape
                    if style == "yellow":
                        _emit(
                            "rich",
                            f"{head}[bold yellow]{e(piece)}[/]",
                            panel=panel,
                        )
                    else:
                        _emit("dim", f"{head}{piece}", panel=panel)
                else:
                    col = Fore.YELLOW + Style.BRIGHT if style == "yellow" else Style.DIM
                    print("\r\033[K" + head + col + piece + Style.RESET_ALL)


def chunk_status(n, heard, translated, timings, finalize=False, at=None):
    """
    Print live VOZ chunk on the right rail.

    Tradução: text + blank (timing goes to Sistema in TUI).
    """
    _emit_voz_chunk_block(n, heard, translated, blank_after=False)
    # Blank where timing would sit on Tradução
    _emit_chunk_blank()
    timing = format_timing_line(timings or {}, at=None, include_clock=False)
    meta = []
    if timing:
        meta.append(timing)
    if at:
        rec = format_recorded_stamp(at)
        if rec:
            meta.append(f"gravado: {rec}")
    if meta:
        # TUI → Sistema; classic → under chunk
        panel = "app" if _log_sink is not None else "main"
        _emit_voz_meta_lines(n, meta, style="dim", panel=panel)
    if finalize and _log_sink is None:
        print()


def _translated_label(from_cache=None):
    """
    Label for the translated line (no trailing colon).

    from_cache:
      True  → came from phrase cache (HIT)
      False → live Google/LLM (MISS / force / cache off path marked live)
      None  → no badge (cache disabled / legacy)
    """
    if from_cache is True:
        return "Translated [CACHE]"
    if from_cache is False:
        return "Translated [LIVE]"
    return "Translated"


# Longest role label used for column alignment of phrase text after ": ".
_ROLE_LABEL_WIDTH = len("Translated [CACHE]: ")


def _role_labels_aligned(from_cache=None):
    """
    Return (heard_label, translated_label) plain strings, same width.

    Both end with ': ' and are left-padded with spaces after the colon so the
    phrase text of Heard and Translated starts on the same column.
    """
    tlab = f"{_translated_label(from_cache)}: "
    hlab = "Heard: "
    w = max(_ROLE_LABEL_WIDTH, len(tlab), len(hlab))
    return hlab.ljust(w), tlab.ljust(w)


def chunk_text_preview(n, heard, translated, from_cache=None):
    """
    Show VOZ chunk on the right rail without timing (timing after TTS).

    SOURCE → TARGET order:
        <right> [Chunk N] BR: heard…
                          EN [CACHE]: translated…
    Blank line comes after audio meta (see chunk_timings).
    """
    _emit_voz_chunk_block(
        n, heard, translated, from_cache=from_cache, blank_after=False
    )


def live_caption_block(n, original, translated, from_cache=None):
    """
    Final Live Captions pair on Tradução — same layout as voice chunks.

        [LC 3]  Caption:    English caption from Windows…
                Translated: Tradução em português…

    from_cache: True=HIT badge, False=LIVE badge, None=plain.
    Only call for **stable/final** utterances (not partial growth spam).
    """
    original = (original or "").strip()
    translated = (translated or "").strip()
    if not original and not translated:
        return
    prefix = f"[LC {n}] "
    indent = " " * len(prefix)
    hlab, tlab = _role_labels_aligned(from_cache)
    # Caption (not mic Heard) — keep column width from aligned labels
    hlab = ("Caption: ").ljust(len(hlab))
    with _print_lock:
        if _log_sink is not None:
            e = _rich_escape
            _emit(
                "rich",
                f"[bold magenta]{e(prefix)}[/][white]{e(hlab)}[/][green]{e(original)}[/]",
            )
            if from_cache is True:
                lab = f"{indent}[bold magenta]{e(tlab)}[/]"
            elif from_cache is False:
                lab = f"{indent}[bold cyan]{e(tlab)}[/]"
            else:
                lab = f"{indent}[bold cyan]{e(tlab)}[/]"
            _emit("rich", f"{lab}[bold white]{e(translated)}[/]")
            _emit_chunk_blank()
            return
        print(
            "\r\033[K"
            + Fore.MAGENTA
            + Style.BRIGHT
            + prefix
            + Style.RESET_ALL
            + Fore.WHITE
            + hlab
            + Fore.GREEN
            + original
        )
        if from_cache is True:
            col = Fore.MAGENTA + Style.BRIGHT
        else:
            col = Fore.CYAN + Style.BRIGHT
        print(
            "\r\033[K" + indent + col + tlab + Style.RESET_ALL + Fore.WHITE + translated
        )
        print()


def clock_hhmmss(stamp=None):
    """
    Return HH:MM:SS for a DB/local timestamp string, datetime, or now.
    Accepts 'YYYY-MM-DD HH:MM:SS', 'HH:MM:SS', or empty → current time.
    """
    import datetime as _dt

    if stamp is None or stamp == "":
        return _dt.datetime.now().strftime("%H:%M:%S")
    if hasattr(stamp, "strftime"):
        return stamp.strftime("%H:%M:%S")
    text = str(stamp).strip()
    # '2026-07-16 14:32:05' or '14:32:05'
    if len(text) >= 19 and text[10] in (" ", "T"):
        return text[11:19]
    if len(text) >= 8 and text[2] == ":" and text[5] == ":":
        return text[:8]
    return _dt.datetime.now().strftime("%H:%M:%S")


def format_recorded_stamp(stamp):
    """Human label for DB created_at (date + time)."""
    if not stamp:
        return ""
    text = str(stamp).strip().replace("T", " ")
    if len(text) >= 19:
        return text[:19]
    return text


def resolve_share_path(path):
    """
    Absolute path suited for attaching/opening on the host OS.

    Converts WSL ``/mnt/c/...`` → ``C:\\...`` so Explorer / Teams / WhatsApp
    Desktop can open the file when LiveLingo runs under WSL.
    """
    import os
    import re

    if not path:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    try:
        abs_path = os.path.abspath(text)
    except OSError:
        abs_path = text

    # WSL mount: /mnt/c/Users/... → C:\Users\...
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", abs_path.replace("\\", "/"))
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"

    # Already Windows-style or native POSIX outside /mnt
    if os.name == "nt":
        return os.path.normpath(abs_path)
    return abs_path


# Audio status strings by TARGET_LANG (display only).
_AUDIO_I18N = {
    "en": {
        "not_generated": "(not generated yet — use r / rN)",
        "missing": "(file missing — use r / rN)",
        "saving": "(saving to disk…)",
    },
    "pt": {
        "not_generated": "(ainda não gerado — use r / rN)",
        "missing": "(arquivo ausente — use r / rN)",
        "saving": "(gravando em disco…)",
    },
    "es": {
        "not_generated": "(aún no generado — use r / rN)",
        "missing": "(archivo ausente — use r / rN)",
        "saving": "(guardando en disco…)",
    },
    "fr": {
        "not_generated": "(pas encore généré — utilisez r / rN)",
        "missing": "(fichier absent — utilisez r / rN)",
        "saving": "(enregistrement…)",
    },
    "de": {
        "not_generated": "(noch nicht erzeugt — r / rN)",
        "missing": "(Datei fehlt — r / rN)",
        "saving": "(wird gespeichert…)",
    },
    "it": {
        "not_generated": "(non ancora generato — usa r / rN)",
        "missing": "(file assente — usa r / rN)",
        "saving": "(salvataggio…)",
    },
    "zh": {
        "not_generated": "(尚未生成 — 用 r / rN)",
        "missing": "(文件不存在 — 用 r / rN)",
        "saving": "(正在写入磁盘…)",
    },
    "ja": {
        "not_generated": "(未生成 — r / rN)",
        "missing": "(ファイルなし — r / rN)",
        "saving": "(保存中…)",
    },
}


def _target_lang_code():
    try:
        import config as cfg

        code = (getattr(cfg, "TARGET_LANG", "en") or "en").lower().strip()
    except Exception:
        code = "en"
    if "-" in code:
        code = code.split("-", 1)[0]
    if code in ("por", "pt-br", "pt_br"):
        code = "pt"
    if code in ("cn", "zh-cn", "zh-tw", "cmn"):
        code = "zh"
    if code in ("jp",):
        code = "ja"
    if code in ("ger", "deu"):
        code = "de"
    if code in ("ita",):
        code = "it"
    return code if code in _AUDIO_I18N else "en"


def _audio_msg(key):
    """Localized audio status snippet for current TARGET_LANG."""
    pack = _AUDIO_I18N.get(_target_lang_code()) or _AUDIO_I18N["en"]
    return pack.get(key) or _AUDIO_I18N["en"].get(key, "")


def _audio_path_exists(path):
    """True if path or its share form is a real file on this OS."""
    import os

    if not path or not str(path).strip():
        return False
    candidates = [str(path).strip()]
    try:
        share = resolve_share_path(path)
        if share and share not in candidates:
            candidates.append(share)
    except Exception:
        pass
    # Relative → absolute from cwd (project root when launched via livelingo.bat)
    for p in list(candidates):
        try:
            abs_p = os.path.abspath(p)
            if abs_p not in candidates:
                candidates.append(abs_p)
        except OSError:
            pass
    for p in candidates:
        try:
            if os.path.isfile(p):
                return True
        except OSError:
            continue
    return False


def format_audio_lines(path, missing_hint=None, pending_write=False, max_width=None):
    """
    Return list of plain display lines for a chunk audio reference.

    Empty path → not-generated hint (TARGET_LANG).
    pending_write=True → path only (WAV still flushing in background; audio
    may already have been played from memory — do NOT show "missing").
    Missing on disk (and not pending) → path + missing note on next line,
    aligned under the path after ``audio: ``.

    Paths are always the **full** host path (no middle ``…`` ellipsis).
    ``max_width`` is accepted for API compat but does **not** truncate the
    path — the right-rail emitter wraps long lines with hang-indent instead.
    """
    del max_width  # never truncate folder/file names (user needs full path)
    label = "audio: "
    if missing_hint is None:
        missing_hint = _audio_msg("not_generated")

    def _one(path_disp: str) -> str:
        return f"{label}{str(path_disp or '')}"

    if not path or not str(path).strip():
        return [f"{label}{missing_hint}"]

    share = resolve_share_path(path)
    display = share or path

    if pending_write:
        # File will appear shortly — show full path even while flushing.
        return [_one(display)]

    if _audio_path_exists(path) or _audio_path_exists(share):
        return [_one(display)]

    # Truly missing on disk (e.g. deleted, or list history without WAV)
    pad = " " * len(label)
    return [_one(display), f"{pad}{_audio_msg('missing')}"]


def _voz_audio_line_width(n=None) -> int:
    """Usable width for one right-rail audio meta line (under [Chunk N] indent)."""
    _pad, _cw, _lw, right_w, _ls, _rs = _rail_geometry(margin=3)
    prefix = f"[Chunk {n if n is not None else 0}] "
    # Use a typical prefix width; caller may pass real n for accuracy
    meta_indent = len(prefix) if n is not None else len("[Chunk 999] ")
    return max(12, right_w - meta_indent)


def _emit_audio_path_one_line(n, path, *, pending_write=False, panel="main"):
    """
    Emit ``audio: <full path>`` as **exactly one** physical line.

    - Full host path (no middle ``…``).
    - No soft wrap into a second line (uses full content width, not the
      narrow VOZ rail budget).
    - Right-aligned within the log content area (grows left if long).
    ``n`` is kept for API symmetry with other VOZ meta helpers.
    """
    del n  # geometry uses full width; chunk prefix not on audio line
    lines = format_audio_lines(path, pending_write=pending_write)
    pad, content_w, _lw, _rw, _ls, _rs = _rail_geometry(margin=3)
    with _print_lock:
        for text in lines:
            text = (text or "").strip()
            if not text:
                continue
            # Right-align inside content width when it fits; never truncate.
            if content_w > 0 and len(text) < content_w:
                text = (" " * (content_w - len(text))) + text
            head = pad  # full width available (not right-rail-only)
            if _log_sink is not None:
                e = _rich_escape
                _emit(
                    "rich",
                    f"{head}[bold yellow]{e(text)}[/]",
                    panel=panel,
                )
            else:
                print(
                    "\r\033[K"
                    + head
                    + Fore.YELLOW
                    + Style.BRIGHT
                    + text
                    + Style.RESET_ALL
                )


def print_audio_ref(n, path, indent=None, pending_write=False):
    """Print full audio path under a VOZ chunk — one line, right-aligned."""
    del indent  # geometry owns indent
    _emit_audio_path_one_line(n, path, pending_write=pending_write, panel="main")
    _emit_chunk_blank()


def format_timing_line(timings, extra=None, at=None, include_clock=True):
    """
    Build the same timing string used in live logs, e.g.
    timing: STT 1.61s | translate 0.73s | TTS 15.47s | ... | 14:32:05

    at: DB/local stamp used for HH:MM:SS (if set).
    include_clock: when True and at is empty, use current time (live logs).
    """
    if not timings:
        if include_clock and at:
            return f"timing: — | {clock_hhmmss(at)}"
        return ""
    parts = []
    if "stt" in timings:
        parts.append("STT {stt:.2f}s".format(**timings))
    if "translate" in timings:
        parts.append("translate {translate:.2f}s".format(**timings))
    if timings.get("tts_skipped"):
        parts.append("TTS —")
    elif "tts" in timings:
        parts.append("TTS {tts:.2f}s".format(**timings))
    if timings.get("tts_first") is not None:
        parts.append("first_audio {tts_first:.2f}s".format(**timings))
    if timings.get("tts_start") is not None:
        parts.append("tts_start {tts_start:.2f}s".format(**timings))
    if timings.get("time_to_audio") is not None:
        parts.append("hear {time_to_audio:.2f}s".format(**timings))
    if "total" in timings:
        parts.append("total {total:.2f}s".format(**timings))
    if not parts:
        return ""
    line = "timing: " + " | ".join(parts)
    if extra:
        line = f"{line}  {extra}"
    # Right-side clock: when the translation was produced / recorded.
    if at:
        line = f"{line} | {clock_hhmmss(at)}"
    elif include_clock:
        line = f"{line} | {clock_hhmmss()}"
    return line


def chunk_timings(
    n, timings, extra=None, at=None, audio_path=None, audio_pending=False
):
    """
    Meta under a live VOZ chunk:

    Tradução (main):
        <blank where timing sat>
        audio: …
        <blank>

    Sistema (app) in TUI — classic keeps dim lines under the chunk:
        timing: STT … | translate … | TTS …
        (sound OFF …)          # if extra
        gravado: YYYY-MM-DD …
    """
    # Timing body without clock; extra as its own line (clearer on Sistema)
    timing = format_timing_line(timings, extra=None, at=None, include_clock=False)
    meta = []
    if timing:
        meta.append(timing)
    extra_s = " ".join(str(extra or "").split())
    if extra_s:
        meta.append(extra_s)
    if at:
        rec = format_recorded_stamp(at)
        if rec:
            meta.append(f"gravado: {rec}")

    # Blank on Tradução in place of the timing block
    _emit_chunk_blank()

    if meta:
        if _log_sink is not None:
            # Sistema tab — technical meta, not mixed with Heard/Translated
            prefix = f"[Chunk {n}] "
            for line in meta:
                _emit("dim", f"{prefix}{line}", panel="app")
        else:
            _emit_voz_meta_lines(n, meta, style="dim", panel="main")

    if audio_path is not None:
        _emit_audio_path_one_line(
            n,
            audio_path,
            pending_write=bool(audio_pending),
            panel="main",
        )
    # Blank after audio on Tradução
    _emit_chunk_blank()


def chunk_stream_start(n, heard):
    """
    Print VOZ stream skeleton on the right rail (SOURCE heard + TARGET …).

    Single-line bodies so classic \\033[1A updates stay aligned.
    """
    heard = (heard or "").strip()
    pad, _cw, _lw, right_w, _ls, right_shift = _rail_geometry(margin=3)
    src_l, tgt_l = _voz_lang_pair()
    prefix = f"[Chunk {n}] "
    lab_src = f"{prefix}{src_l}: "
    lab_tgt = f"{' ' * len(prefix)}{tgt_l}: "
    head = pad + right_shift
    src_budget = max(8, right_w - len(lab_src))
    tgt_budget = max(8, right_w - len(lab_tgt))
    heard_disp = _one_line(heard, src_budget)
    with _print_lock:
        if _log_sink is not None:
            e = _rich_escape
            # 1) SOURCE first
            _emit(
                "rich",
                f"{head}"
                f"[bold yellow]{e(prefix)}[/]"
                f"[white]{e(src_l)}: [/]"
                f"[green]{e(heard_disp)}[/]",
            )
            # 2) TARGET placeholder
            _emit(
                "rich",
                f"{head}"
                f"[white]{e(' ' * len(prefix))}[/]"
                f"[bold blue]{e(tgt_l)}: [/]"
                f"[bold white]…[/]",
            )
            return
        print(
            "\r\033[K"
            + head
            + Fore.YELLOW
            + Style.BRIGHT
            + prefix
            + Style.RESET_ALL
            + Fore.WHITE
            + f"{src_l}: "
            + Fore.GREEN
            + heard_disp
            + Style.RESET_ALL
        )
        print(
            "\r\033[K"
            + head
            + (" " * len(prefix))
            + Fore.BLUE
            + Style.BRIGHT
            + f"{tgt_l}: "
            + Style.RESET_ALL
            + Fore.WHITE
            + "…"
        )


def chunk_stream_update(n, translated):
    """Overwrite/append the translated (target) row while LLM tokens stream."""
    translated = (translated or "").strip() or "…"
    pad, _cw, _lw, right_w, _ls, right_shift = _rail_geometry(margin=3)
    _src_l, tgt_l = _voz_lang_pair()
    prefix = f"[Chunk {n}] "
    # Target is line 2 (hang-indent under source prefix)
    lab_tgt = f"{' ' * len(prefix)}{tgt_l}: "
    head = pad + right_shift
    budget = max(8, right_w - len(lab_tgt))
    disp = _one_line(translated, budget)
    with _print_lock:
        if _log_sink is not None:
            e = _rich_escape
            _emit(
                "rich",
                f"{head}"
                f"[white]{e(' ' * len(prefix))}[/]"
                f"[bold blue]{e(tgt_l)}: [/]"
                f"[bold white]{e(disp)}[/]",
            )
            return
        # Classic: cursor after tgt (line 2); go up 1 and rewrite target only.
        sys.stdout.write(
            "\033[1A\r\033[K"
            + head
            + (" " * len(prefix))
            + Fore.BLUE
            + Style.BRIGHT
            + f"{tgt_l}: "
            + Style.RESET_ALL
            + Fore.WHITE
            + disp
            + "\n"
        )
        sys.stdout.flush()


def chunk_stream_done(n, heard, translated, from_cache=None):
    """
    Finalize streamed VOZ block on the right rail (full wrap).

    Classic: cursor after compact stream → move up 2 and replace with full block.
    TUI: append final right-rail block (no cursor rewrite).
    Blank after audio comes from chunk_timings.
    """
    if _log_sink is not None:
        _emit_voz_chunk_block(
            n, heard, translated, from_cache=from_cache, blank_after=False
        )
        return
    with _print_lock:
        # Clear compact src + tgt rows, then print full right-rail block.
        sys.stdout.write("\033[2A\r\033[K")
        # _emit_voz_chunk_block acquires the lock again (RLock) — OK.
        _emit_voz_chunk_block(
            n, heard, translated, from_cache=from_cache, blank_after=False
        )
        sys.stdout.flush()


def _synonym_md_to_rich(line: str) -> str:
    """
    Convert simple Markdown (**bold**, *italic*) to Rich markup for TUI.
    Section headers (1. **Title**:) get stronger color.
    """
    import re

    e = _rich_escape
    s = (line or "").rstrip()
    if not s:
        return ""

    # Numbered section header: 1. **Significado e Uso**:
    m = re.match(r"^(\d+\.\s*)\*\*(.+?)\*\*(\s*:?\s*)$", s)
    if m:
        return f"[bold magenta]{e(m.group(1))}{e(m.group(2))}{e(m.group(3))}[/]"

    # Bullet with bold label: - **Frase em Inglês**: rest
    m = re.match(r"^(\s*[-•]\s*)\*\*(.+?)\*\*(\s*:?\s*)(.*)$", s)
    if m:
        rest = m.group(4) or ""
        rest_fmt = _synonym_inline_md(rest)
        return (
            f"[dim]{e(m.group(1))}[/]"
            f"[bold cyan]{e(m.group(2))}{e(m.group(3))}[/]"
            f"{rest_fmt}"
        )

    # Indented translation line: **Tradução**: ...
    m = re.match(r"^(\s*)\*\*(.+?)\*\*(\s*:?\s*)(.*)$", s)
    if m:
        rest = m.group(4) or ""
        return (
            f"{e(m.group(1))}"
            f"[bold green]{e(m.group(2))}{e(m.group(3))}[/]"
            f"{_synonym_inline_md(rest)}"
        )

    # Plain bullet synonym list: - Quick (Rápido)
    m = re.match(r"^(\s*[-•]\s*)(.+)$", s)
    if m:
        return f"[yellow]{e(m.group(1))}[/][white]{_synonym_inline_md(m.group(2))}[/]"

    return f"[white]{_synonym_inline_md(s)}[/]"


def _synonym_inline_md(s: str) -> str:
    """Inline **bold** and *italic* → Rich; escape the rest."""
    import re

    e = _rich_escape
    if not s:
        return ""

    # Tokenize by **...** then *...*
    out = []
    pos = 0
    # Bold first
    pattern = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`")
    for m in pattern.finditer(s):
        if m.start() > pos:
            out.append(e(s[pos : m.start()]))
        if m.group(1) is not None:
            out.append(f"[bold yellow]{e(m.group(1))}[/]")
        elif m.group(2) is not None:
            out.append(f"[bold cyan]{e(m.group(2))}[/]")
        else:
            out.append(f"[dim]{e(m.group(3))}[/]")
        pos = m.end()
    if pos < len(s):
        out.append(e(s[pos:]))
    return "".join(out)


def _synonym_md_to_ansi(line: str) -> str:
    """Classic terminal: **bold** / *word* without raw asterisks."""
    import re

    s = (line or "").rstrip()
    if not s:
        return ""

    m = re.match(r"^(\d+\.\s*)\*\*(.+?)\*\*(\s*:?\s*)$", s)
    if m:
        return (
            Fore.MAGENTA
            + Style.BRIGHT
            + m.group(1)
            + m.group(2)
            + m.group(3)
            + Style.RESET_ALL
        )

    def repl_bold(mo):
        return Style.BRIGHT + Fore.YELLOW + mo.group(1) + Style.RESET_ALL

    def repl_ital(mo):
        return Fore.CYAN + Style.BRIGHT + mo.group(1) + Style.RESET_ALL

    def repl_code(mo):
        return Style.DIM + mo.group(1) + Style.RESET_ALL

    # Strip markdown markers while coloring
    s2 = re.sub(r"\*\*(.+?)\*\*", repl_bold, s)
    s2 = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", repl_ital, s2)
    s2 = re.sub(r"`(.+?)`", repl_code, s2)
    if s.lstrip().startswith(("-", "•")):
        return Fore.YELLOW + s2 + Style.RESET_ALL
    return Fore.WHITE + s2 + Style.RESET_ALL


def synonyms_result(word, text):
    """
    Print synonym explanation with readable formatting.

    Converts LLM Markdown (**headers**, *emphasis*) to Rich (TUI) or ANSI
    (classic) instead of showing raw asterisks.
    """
    word = (word or "").strip()
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    rule = "=" * 64
    pad = _pad(3)

    lines_out = []
    for raw_line in body.split("\n"):
        # Soft-wrap very long lines for classic; TUI RichLog wraps too
        lines_out.append(raw_line)

    with _print_lock:
        if _log_sink is not None:
            _emit("rich", f"{pad}[bold cyan]{rule}[/]")
            w_esc = _rich_escape((word or "").upper())
            _emit(
                "rich",
                f"{pad}[bold cyan]  ★ Sinônimos / Meaning: [/][bold yellow]{w_esc}[/]",
            )
            _emit("rich", f"{pad}[bold cyan]{rule}[/]")
            _emit("raw", "")
            for ln in lines_out:
                if not ln.strip():
                    _emit("raw", "")
                    continue
                _emit("rich", pad + _synonym_md_to_rich(ln))
            _emit("raw", "")
            _emit("rich", f"{pad}[bold cyan]{rule}[/]")
            return

        print()
        print("\r\033[K" + pad + Fore.CYAN + rule)
        print(
            "\r\033[K"
            + pad
            + Fore.CYAN
            + Style.BRIGHT
            + f"  ★ Sinônimos / Meaning: "
            + Fore.YELLOW
            + (word or "").upper()
            + Style.RESET_ALL
        )
        print("\r\033[K" + pad + Fore.CYAN + rule + Style.RESET_ALL)
        print()
        for ln in lines_out:
            if not ln.strip():
                print()
                continue
            print("\r\033[K" + pad + _synonym_md_to_ansi(ln))
        print("\r\033[K" + pad + Fore.CYAN + rule + Style.RESET_ALL)
        print()


def favorites_popup(favs, src_lang, tgt_lang):
    """
    Favorited sentences in a box frame with aligned right borders.

    Box geometry (visual columns, not Python len()):
        ╔ + INNER × ═ + ╗
        ║ + INNER content + ║
    Content is padded/truncated by display width so ★ / accents don't
    push the right border out of line.
    """
    import textwrap
    import unicodedata

    INNER = 60  # columns between the two vertical borders

    def _disp_w(s: str) -> int:
        """Terminal display width (wide chars count as 2)."""
        w = 0
        for ch in s or "":
            ea = unicodedata.east_asian_width(ch)
            if ea in ("F", "W"):
                w += 2
            elif unicodedata.category(ch) in ("Mn", "Me", "Cf"):
                continue
            else:
                w += 1
        return w

    def _fit(s: str, width: int) -> str:
        """Truncate/pad string to exactly `width` display columns."""
        s = s or ""
        # Truncate
        out = []
        w = 0
        for ch in s:
            cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
            if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
                out.append(ch)
                continue
            if w + cw > width:
                break
            out.append(ch)
            w += cw
        body = "".join(out)
        pad = max(0, width - _disp_w(body))
        return body + (" " * pad)

    def format_line(text: str) -> str:
        # ║ + INNER content + ║  → same total width as ╔ + INNER × ═ + ╗
        return "║" + _fit(text, INNER) + "║"

    def center(text: str) -> str:
        tw = _disp_w(text)
        if tw >= INNER:
            return _fit(text, INNER)
        left = (INNER - tw) // 2
        return _fit((" " * left) + text, INNER)

    lines = []
    lines.append("╔" + "═" * INNER + "╗")
    lines.append(format_line(center("★ MY FAVORITES ★")))
    lines.append("╠" + "═" * INNER + "╣")

    if not favs:
        lines.append(format_line("  No favorited items in this session."))
    else:
        for chunk_num, heard, translated in favs:
            lines.append(format_line(f" [Chunk {chunk_num}]"))

            # Target language (translated) first
            tgt_prefix = f"   {tgt_lang}: "
            # Wrap by character length ≈ display width for Latin; keep prefix indent
            wrap_w = max(8, INNER - 1)
            tgt_wrapper = textwrap.TextWrapper(
                width=wrap_w,
                initial_indent=tgt_prefix,
                subsequent_indent=" " * len(tgt_prefix),
                replace_whitespace=False,
                drop_whitespace=True,
            )
            for line in tgt_wrapper.wrap((translated or "").strip() or ""):
                lines.append(format_line(line))

            # Source language (heard) second
            src_prefix = f"   {src_lang}: "
            src_wrapper = textwrap.TextWrapper(
                width=wrap_w,
                initial_indent=src_prefix,
                subsequent_indent=" " * len(src_prefix),
                replace_whitespace=False,
                drop_whitespace=True,
            )
            for line in src_wrapper.wrap((heard or "").strip() or ""):
                lines.append(format_line(line))

            lines.append(format_line(""))

    lines.append("╠" + "═" * INNER + "╣")
    lines.append(format_line(" Press [Enter] to close this window..."))
    lines.append("╚" + "═" * INNER + "╝")

    # Color borders/title without breaking geometry (color codes are zero-width)
    def _paint(line: str) -> str:
        if line.startswith("╔") or line.startswith("╚") or line.startswith("╠"):
            return Fore.CYAN + line + Style.RESET_ALL
        if line.startswith("║") and line.endswith("║"):
            mid = line[1:-1]
            # Title row
            if "MY FAVORITES" in mid:
                # re-center already in mid; color the star title substring
                colored_mid = mid.replace(
                    "★ MY FAVORITES ★",
                    Fore.YELLOW
                    + Style.BRIGHT
                    + "★ MY FAVORITES ★"
                    + Style.RESET_ALL
                    + Fore.CYAN,
                )
                return (
                    Fore.CYAN
                    + "║"
                    + Style.RESET_ALL
                    + colored_mid
                    + Fore.CYAN
                    + "║"
                    + Style.RESET_ALL
                )
            if "Press [Enter]" in mid:
                return (
                    Fore.CYAN
                    + "║"
                    + Style.RESET_ALL
                    + Style.DIM
                    + mid
                    + Style.RESET_ALL
                    + Fore.CYAN
                    + "║"
                    + Style.RESET_ALL
                )
            return (
                Fore.CYAN
                + "║"
                + Style.RESET_ALL
                + mid
                + Fore.CYAN
                + "║"
                + Style.RESET_ALL
            )
        return line

    in_tui = False
    print()
    with _print_lock:
        in_tui = _log_sink is not None
        if in_tui:
            # TUI: plain geometry (no ANSI) so RichLog borders stay aligned
            for line in lines:
                _emit("raw", line)
        else:
            for line in lines:
                print(_paint(line))

    # Wait for Enter (outside print lock — TUI stdin proxy may block)
    try:
        sys.stdin.readline()
    except Exception:
        try:
            sys.__stdin__.readline()
        except Exception:
            pass
