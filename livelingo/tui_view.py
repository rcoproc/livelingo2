"""
tui_view.py
===========
Read-only Textual viewer for one Tradução panel (LC / Coach / VOZ).

Connects to the host log bus started by the main LiveLingo TUI:

    python main.py              # terminal A
    python main.py view coach   # terminal B

Copy: mouse-select + Ctrl+C (selection) · Ctrl+Shift+C / ``a`` / ``copy`` (entire log)
Export: ``e`` / Ctrl+S / ``export`` → ``YYYY-MM-DD_livelingo-view-<panel>.md``
Search (same as main TUI): /texto · /n · /p · aliases find/search/s?
"""

from __future__ import annotations

import datetime as _dt
import queue
import re
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.selection import Selection
from textual.widgets import Footer, Header, Input, RichLog, Static

from .log_bus import (
    format_view_markup,
    iter_events,
    normalize_view_panel,
    panel_matches,
)

_PANEL_TITLES = {
    "main": "VOZ · tradução",
    "lc": "LC · LiveCaptions",
    "coach": "Coach · entrevista",
}

_PANEL_SLUGS = {
    "main": "voz",
    "lc": "lc",
    "coach": "coach",
}


def _os_clipboard(text: str) -> bool:
    """Best-effort OS clipboard (Windows / WSL / Linux)."""
    text = text or ""
    if not text:
        return False
    for clip_cmd in (["clip.exe"], ["clip"]):
        try:
            r = subprocess.run(
                clip_cmd,
                input=text.encode("utf-16le"),
                capture_output=True,
                timeout=8,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$t = [Console]::In.ReadToEnd(); Set-Clipboard -Value $t",
            ],
            input=text,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    for cmd in (
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["wl-copy"],
    ):
        try:
            r = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=5,
            )
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _strip_markup_plain(raw: str) -> str:
    """Best-effort plain text from Rich markup."""
    plain = raw or ""
    try:
        from rich.text import Text

        plain = Text.from_markup(plain).plain
    except Exception:
        plain = re.sub(r"\[/?[^\]]*\]", "", plain)
    return plain


class ViewRichLog(RichLog):
    """
    RichLog with reflow on resize + mouse selection / Ctrl+C + /search highlight.

    Same gaps as upstream RichLog (no get_selection / no apply_offsets) that
    SelectableRichLog fixes in the main TUI — mirrored here for detached viewers.
    """

    # Keys only when this log is focused — never steal from #search Input
    BINDINGS = [
        Binding("n", "view_search_next", "Próx.", show=False),
        Binding("p", "view_search_prev", "Anter.", show=False),
        Binding("slash", "view_focus_search", "Buscar", show=False),
        Binding("a", "view_copy_all", "Copiar tudo", show=False),
        Binding("e", "view_export_md", "Exportar .md", show=False),
    ]

    def __init__(self, **kwargs):
        kwargs.setdefault("min_width", 80)
        super().__init__(**kwargs)
        self._markup_lines: list[str] = []
        self._plain_lines: list[str] = []
        self._reflowing: bool = False
        self._last_good_width: int = 0
        # Vim-style / search highlight (same as SelectableRichLog in tui_app)
        self._search_query: str = ""
        self._search_hit_ys: set[int] = set()
        self._search_current_y: int | None = None

    def action_view_search_next(self) -> None:
        try:
            self.app.action_search_next()
        except Exception:
            pass

    def action_view_search_prev(self) -> None:
        try:
            self.app.action_search_prev()
        except Exception:
            pass

    def action_view_focus_search(self) -> None:
        try:
            self.app.action_focus_search()
        except Exception:
            pass

    def action_view_copy_all(self) -> None:
        try:
            self.app.action_copy_log()
        except Exception:
            pass

    def action_view_export_md(self) -> None:
        try:
            self.app.action_export_md()
        except Exception:
            pass

    def _safe_render_width(self) -> int:
        try:
            region_w = int(self.scrollable_content_region.width or 0)
        except Exception:
            region_w = 0
        if region_w >= 20:
            w = max(20, region_w - 2)
            self._last_good_width = w
            return w
        if int(self._last_good_width or 0) >= 20:
            return int(self._last_good_width)
        try:
            app_w = int(getattr(self.app.size, "width", 0) or 0)
            if app_w >= 40:
                return max(40, app_w - 4)
        except Exception:
            pass
        return 80

    def write(
        self,
        content,
        width=None,
        expand=False,
        shrink=True,
        scroll_end=None,
        animate=False,
    ):
        try:
            raw_src = content if isinstance(content, str) else str(content)
        except Exception:
            raw_src = ""
        if not getattr(self, "_reflowing", False):
            try:
                self._markup_lines.append(raw_src)
                max_n = self.max_lines
                if max_n is not None and len(self._markup_lines) > max_n:
                    self._markup_lines = self._markup_lines[-max_n:]
            except Exception:
                pass
            try:
                plain = _strip_markup_plain(raw_src)
                for line in (plain or "").splitlines() or [""]:
                    self._plain_lines.append(line)
                max_n = self.max_lines
                if max_n is not None and len(self._plain_lines) > max_n:
                    self._plain_lines = self._plain_lines[-max_n:]
            except Exception:
                pass

        if width is None:
            width = self._safe_render_width()
            expand = False
            shrink = False

        return super().write(
            content,
            width=width,
            expand=expand,
            shrink=shrink,
            scroll_end=scroll_end,
            animate=animate,
        )

    def clear(self) -> None:
        if not getattr(self, "_reflowing", False):
            self._markup_lines.clear()
            self._plain_lines.clear()
            self.clear_search_highlight(refresh=False)
        try:
            return super().clear()
        except Exception:
            return None

    def get_plain_text(self) -> str:
        if self._plain_lines:
            return "\n".join(self._plain_lines)
        try:
            return "\n".join(line.text for line in self.lines)
        except Exception:
            return ""

    def set_search_highlight(
        self,
        query: str,
        hit_ys: list[int] | None,
        current_y: int | None,
    ) -> None:
        """Highlight search hits; current_y is the active match (stronger color)."""
        self._search_query = (query or "").strip()
        self._search_hit_ys = set(int(y) for y in (hit_ys or []) if y is not None)
        try:
            self._search_current_y = int(current_y) if current_y is not None else None
        except Exception:
            self._search_current_y = None
        try:
            self._line_cache.clear()
        except Exception:
            pass
        try:
            self.refresh()
        except Exception:
            pass

    def clear_search_highlight(self, *, refresh: bool = True) -> None:
        """Remove / search highlight from this log."""
        self._search_query = ""
        self._search_hit_ys = set()
        self._search_current_y = None
        try:
            self._line_cache.clear()
        except Exception:
            pass
        if refresh:
            try:
                self.refresh()
            except Exception:
                pass

    def find_match_ys(self, query: str) -> list[int]:
        """Case-insensitive substring search; Y matches scroll content_y."""
        q = (query or "").casefold()
        if not q:
            return []
        hits: list[int] = []
        try:
            lines = getattr(self, "lines", None)
            if lines:
                for y, line in enumerate(lines):
                    try:
                        text = getattr(line, "text", None)
                        if text is None:
                            text = str(line)
                    except Exception:
                        text = ""
                    if q in (text or "").casefold():
                        hits.append(y)
                if hits or len(lines) > 0:
                    return hits
        except Exception:
            hits = []
        for y, line in enumerate(self._plain_lines):
            if q in (line or "").casefold():
                hits.append(y)
        return hits

    def scroll_to_content_y(self, y: int) -> None:
        """Scroll so content row ``y`` is visible (prefer upper third)."""
        try:
            self.auto_scroll = False
        except Exception:
            pass
        try:
            region_h = int(self.scrollable_content_region.height or 0)
        except Exception:
            region_h = 0
        if region_h < 1:
            try:
                region_h = int(getattr(self.size, "height", 0) or 0)
            except Exception:
                region_h = 10
        region_h = max(3, region_h)
        target = max(0, int(y) - max(0, region_h // 3))
        try:
            max_y = int(getattr(self, "max_scroll_y", 0) or 0)
            if max_y > 0:
                target = min(target, max_y)
        except Exception:
            pass
        for kwargs in (
            {"animate": False, "immediate": True},
            {"animate": False},
            {},
        ):
            try:
                self.scroll_to(0, target, **kwargs)
                break
            except TypeError:
                continue
            except Exception:
                break

    def _search_spans_on_line(self, text: str) -> list[tuple[int, int]]:
        q = (self._search_query or "").strip()
        if not q or not text:
            return []
        spans: list[tuple[int, int]] = []
        try:
            for m in re.finditer(re.escape(q), text, flags=re.IGNORECASE):
                a, b = m.span()
                if b > a:
                    spans.append((a, b))
        except Exception:
            lower = text.casefold()
            ql = q.casefold()
            start = 0
            while True:
                idx = lower.find(ql, start)
                if idx < 0:
                    break
                spans.append((idx, idx + len(ql)))
                start = idx + max(1, len(ql))
        return spans

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Required for mouse-drag + Ctrl+C (Textual Screen copy)."""
        if selection is None:
            return None
        try:
            if self.lines:
                text = "\n".join(line.text for line in self.lines)
                extracted = selection.extract(text)
                # Soft-wrap → spaces so paste into single-line fields stays whole
                cleaned = " ".join((extracted or "").split())
                return cleaned, "\n"
        except Exception:
            pass
        if self._plain_lines:
            extracted = selection.extract("\n".join(self._plain_lines))
            cleaned = " ".join((extracted or "").split())
            return cleaned, "\n"
        return None

    def selection_updated(self, selection: Selection | None) -> None:
        try:
            self._line_cache.clear()
        except Exception:
            pass
        self.refresh()

    def render_line(self, y: int):
        """Stamp content offsets + selection + /search highlights."""
        from rich.cells import cell_len
        from rich.style import Style
        from rich.text import Text
        from textual.strip import Strip as TStrip

        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y
        try:
            width = self.scrollable_content_region.width
        except Exception:
            width = self.size.width

        selection = self.text_selection
        has_sel = selection is not None and not (
            selection.start is None and selection.end is None
        )
        has_search = bool(
            (self._search_query or "").strip()
            and content_y in (self._search_hit_ys or set())
        )

        if has_sel or has_search:
            try:
                if content_y < len(self.lines):
                    full = self.lines[content_y]
                    raw = full.text if hasattr(full, "text") else str(full)
                    line_text = Text(raw, no_wrap=True)

                    if has_sel:
                        span = selection.get_span(content_y)
                        if span is not None:
                            start, end = span
                            if end == -1:
                                end = len(line_text)
                            # Always yellow + dark text (Textual default navy
                            # hides light/dark theme text — same fix as main TUI).
                            sel_style = Style(
                                bgcolor="#f0d78c", color="#1a1b26", bold=True
                            )
                            start = max(0, min(int(start), len(line_text)))
                            end = max(start, min(int(end), len(line_text)))
                            if end > start:
                                line_text.stylize(sel_style, start, end)

                    if has_search:
                        other_style = Style(
                            bgcolor="#f0d78c", color="#1a1b26", bold=True
                        )
                        current_style = Style(
                            bgcolor="#ff9500", color="#1a1b26", bold=True
                        )
                        is_current = (
                            self._search_current_y is not None
                            and int(self._search_current_y) == int(content_y)
                        )
                        if is_current:
                            line_text.stylize(
                                Style(bgcolor="#3d2e12", color=None),
                                0,
                                len(line_text),
                            )
                        else:
                            line_text.stylize(
                                Style(bgcolor="#2a2818", color=None),
                                0,
                                len(line_text),
                            )
                        for a, b in self._search_spans_on_line(raw):
                            a = max(0, min(a, len(line_text)))
                            b = max(a, min(b, len(line_text)))
                            if b > a:
                                line_text.stylize(
                                    current_style if is_current else other_style,
                                    a,
                                    b,
                                )

                    # Match main TUI: crop_extend (do NOT apply_style — it
                    # can wipe selection fg/bg back to theme defaults).
                    strip = TStrip(
                        list(line_text.render(self.app.console)),
                        cell_len(raw),
                    )
                    strip = strip.crop_extend(
                        scroll_x, scroll_x + width, self.rich_style
                    )
                    try:
                        strip = strip.apply_offsets(scroll_x, content_y)
                    except Exception:
                        pass
                    return strip
            except Exception:
                pass

        try:
            line = super()._render_line(content_y, scroll_x, width)
            strip = line.apply_style(self.rich_style)
        except Exception:
            strip = TStrip.blank(width, self.rich_style)
        try:
            strip = strip.apply_offsets(scroll_x, content_y)
        except Exception:
            pass
        return strip

    def reflow(self, width: int | None = None) -> None:
        """Rewrite stored markup at the current (or given) panel width."""
        if getattr(self, "_reflowing", False):
            return
        sources = list(getattr(self, "_markup_lines", []) or [])
        if not sources:
            return
        self._last_good_width = 0
        if width is not None:
            try:
                width = max(20, int(width))
                self._last_good_width = width
            except Exception:
                width = None
        if width is None:
            width = self._safe_render_width()

        follow = bool(getattr(self, "auto_scroll", True))
        self._reflowing = True
        try:
            self._plain_lines.clear()
            try:
                super().clear()
            except Exception:
                pass
            for src in sources:
                try:
                    self.write(
                        src,
                        width=width,
                        expand=False,
                        shrink=False,
                        scroll_end=False,
                        animate=False,
                    )
                except Exception:
                    pass
            self._markup_lines = list(sources)
            if follow:
                try:
                    y = int(getattr(self, "max_scroll_y", 0) or 0)
                    self.scroll_to(0, y, animate=False)
                except Exception:
                    try:
                        self.scroll_end(animate=False)
                    except Exception:
                        pass
            try:
                self.refresh()
            except Exception:
                pass
        finally:
            self._reflowing = False


class PanelViewApp(App):
    """Single-panel log mirror — no mic/pipeline, reconnects to host bus."""

    CSS = """
    Screen {
        layout: vertical;
        width: 100%;
        height: 100%;
    }
    /* Soft selection: light highlight + dark text (not solid navy "erase") */
    Screen > .screen--selection {
        background: #f0d78c;
        color: #1a1b26;
        text-style: bold;
    }
    #status {
        height: 1;
        width: 1fr;
        dock: top;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    #status.-ok {
        color: $success;
    }
    #status.-wait {
        color: $warning;
    }
    #view-body {
        height: 1fr;
        width: 1fr;
        layout: vertical;
    }
    #log {
        height: 1fr;
        width: 1fr;
        min-width: 1;
        padding: 0 1;
        border: none;
        scrollbar-size: 1 1;
        overflow-x: auto;
        overflow-y: auto;
    }
    #search-row {
        height: 3;
        width: 1fr;
        dock: bottom;
        layout: horizontal;
        padding: 0 1;
        background: $surface;
    }
    #search-label {
        width: auto;
        height: 3;
        content-align: left middle;
        color: $text-muted;
        padding: 0 1 0 0;
    }
    #search {
        width: 1fr;
        height: 3;
        border: round $primary;
        background: $surface;
    }
    #search:focus {
        border: round $accent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Sair", show=True),
        Binding("ctrl+q", "quit", "Sair", show=False),
        Binding("ctrl+c", "copy_selection", "Copiar", show=True, priority=True),
        Binding(
            "ctrl+shift+c",
            "copy_log",
            "Copiar tudo",
            show=True,
            priority=True,
        ),
        Binding("ctrl+s", "export_md", "Exportar .md", show=True, priority=True),
        # a / e / slash / n / p live on ViewRichLog (log focused only).
        Binding("escape", "clear_search", "Limpar busca", show=False),
    ]

    TITLE = "LiveLingo View"

    def __init__(
        self,
        panel: str,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.panel = normalize_view_panel(panel)
        self.host = str(host or "127.0.0.1")
        self.port = int(port or 8765)
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._connected = False
        self._reflow_pending = False
        self._last_width_for_reflow = 0
        self._search_query: str = ""
        self._search_hits: list[int] = []
        self._search_i: int = -1
        self.sub_title = _PANEL_TITLES.get(self.panel, self.panel)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Aguardando host em {self.host}:{self.port} …",
            id="status",
            classes="-wait",
        )
        with Vertical(id="view-body"):
            yield ViewRichLog(
                id="log",
                highlight=True,
                markup=True,
                wrap=True,
                auto_scroll=True,
                max_lines=5000,
                min_width=80,
            )
        with Horizontal(id="search-row"):
            yield Static("/", id="search-label", markup=False)
            yield Input(
                placeholder=(
                    "busca  ·  /n /p  ·  copy  ·  export  ·  find texto"
                ),
                id="search",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="log-view-reader", daemon=True
        )
        self._reader.start()
        self.set_interval(0.05, self._drain)
        self._paint_status(False)
        try:
            self._last_width_for_reflow = int(getattr(self.size, "width", 0) or 0)
        except Exception:
            self._last_width_for_reflow = 0
        # Focus log so mouse selection / Ctrl+C work immediately
        try:
            self.query_one("#log", ViewRichLog).focus()
        except Exception:
            pass

    def on_unmount(self) -> None:
        self._stop.set()

    def on_resize(self, event) -> None:  # noqa: ARG002
        """Reflow wrapped lines when the viewer terminal width changes."""
        cur_w = 0
        try:
            cur_w = int(getattr(self.size, "width", 0) or 0)
        except Exception:
            cur_w = 0
        prev = int(getattr(self, "_last_width_for_reflow", 0) or 0)
        if cur_w >= 20 and (prev < 20 or abs(cur_w - prev) >= 2):
            self._last_width_for_reflow = cur_w
            self._schedule_reflow()

    def _schedule_reflow(self) -> None:
        if getattr(self, "_reflow_pending", False):
            return
        self._reflow_pending = True

        def _run() -> None:
            self._reflow_pending = False
            try:
                log = self.query_one("#log", ViewRichLog)
            except Exception:
                return
            try:
                app_w = int(getattr(self.size, "width", 0) or 0)
                if app_w >= 40:
                    log.min_width = max(40, app_w - 4)
            except Exception:
                pass
            try:
                log._last_good_width = 0
                log.reflow()
            except Exception:
                pass
            # Wrapped lines shift Y — rebuild hits if a search is active
            if (self._search_query or "").strip():
                try:
                    self._run_search(self._search_query, start_i=max(0, self._search_i))
                except Exception:
                    pass

        try:
            self.set_timer(0.08, _run)
        except Exception:
            _run()

    def action_focus_search(self) -> None:
        """`/` focuses the search box (same idea as main TUI cmd line)."""
        try:
            inp = self.query_one("#search", Input)
        except Exception:
            return
        try:
            if self.focused is inp:
                return
        except Exception:
            pass
        try:
            inp.focus()
            inp.value = ""
        except Exception:
            pass

    def action_search_next(self) -> None:
        """`n` — next match (skipped while typing in #search)."""
        try:
            if isinstance(self.focused, Input):
                return
        except Exception:
            pass
        self._handle_log_search("/n")

    def action_search_prev(self) -> None:
        """`p` — previous match (skipped while typing in #search)."""
        try:
            if isinstance(self.focused, Input):
                return
        except Exception:
            pass
        self._handle_log_search("/p")

    def action_clear_search(self) -> None:
        """Esc — clear highlights; blur search box back to the log."""
        self._search_query = ""
        self._search_hits = []
        self._search_i = -1
        try:
            log = self.query_one("#log", ViewRichLog)
            log.clear_search_highlight()
        except Exception:
            pass
        try:
            inp = self.query_one("#search", Input)
            inp.value = ""
        except Exception:
            pass
        try:
            self.query_one("#log", ViewRichLog).focus()
        except Exception:
            pass
        try:
            self.notify("Busca limpa", severity="information", timeout=1.5)
        except Exception:
            pass

    @on(Input.Submitted, "#search")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        try:
            event.input.value = ""
        except Exception:
            pass
        if not value:
            try:
                self.notify(
                    "Busca: /texto · /n · /p  ·  copy/copiar  ·  export/exportar",
                    severity="information",
                    timeout=3,
                )
            except Exception:
                pass
            return

        low = value.lower()
        # Copy / export commands (before treating as search query)
        if low in ("copy", "copiar", "copyall", "copy all", "a"):
            self.action_copy_log()
            try:
                event.input.focus()
            except Exception:
                pass
            return
        if low in ("export", "exportar", "e", "md", "save", "salvar"):
            self.action_export_md()
            try:
                event.input.focus()
            except Exception:
                pass
            return
        if low.startswith("export ") or low.startswith("exportar "):
            # Optional custom slug: export minha-entrevista
            slug = value.split(None, 1)[1].strip()
            self.action_export_md(slug=slug)
            try:
                event.input.focus()
            except Exception:
                pass
            return

        # Normalize aliases → /… (same as main TUI)
        if low in ("find", "find:", "search", "search:"):
            cmd = "/"
        elif low.startswith("find:") or low.startswith("search:"):
            cmd = "/" + value.split(":", 1)[1]
        elif low.startswith("find ") or low.startswith("search "):
            cmd = "/" + (value.split(None, 1)[1] if " " in value else "")
        elif len(value) >= 2 and value[0] in ("s", "S") and value[1] == "?":
            cmd = "/" + value[2:]
        elif value.startswith("/"):
            cmd = value
        else:
            # Plain text in the search box = new query
            cmd = "/" + value
        self._handle_log_search(cmd)
        # Keep focus in search for /n /p chaining; user Esc/click for log
        try:
            event.input.focus()
        except Exception:
            pass

    def _run_search(self, query: str, *, start_i: int = 0) -> None:
        """Find query, store hits, jump to start_i (wrapped)."""
        try:
            log = self.query_one("#log", ViewRichLog)
        except Exception:
            return
        query = (query or "").strip()
        if not query:
            try:
                self.notify(
                    "Busca: /texto  ·  /n próximo  ·  /p anterior",
                    severity="information",
                    timeout=3,
                )
            except Exception:
                pass
            return

        hits: list[int] = []
        try:
            hits = list(log.find_match_ys(query) or [])
        except Exception:
            hits = []

        self._search_query = query
        self._search_hits = hits
        if not hits:
            self._search_i = -1
            try:
                log.clear_search_highlight()
            except Exception:
                pass
            try:
                self.notify(
                    f'Nenhuma ocorrência: "{query}"',
                    severity="warning",
                    timeout=3,
                )
            except Exception:
                pass
            return

        n = len(hits)
        i = int(start_i) % n
        self._search_i = i
        y = hits[i]
        try:
            log.set_search_highlight(query, hits, y)
            log.scroll_to_content_y(y)
        except Exception:
            pass
        try:
            self.notify(
                f'Busca: "{query}" — {i + 1}/{n}',
                severity="information",
                timeout=2,
            )
        except Exception:
            pass

    def _handle_log_search(self, value: str) -> None:
        """
        Vim-style log search (same semantics as the main TUI):

          /text   new search
          /n      next match (wrap)
          /p      previous match (wrap)
          /       repeat last query
        """
        raw = (value or "").strip()
        if not raw.startswith("/"):
            return
        low = raw.lower()

        if low == "/n":
            action = "next"
            query = self._search_query
        elif low == "/p":
            action = "prev"
            query = self._search_query
        elif raw == "/":
            action = "repeat"
            query = self._search_query
        else:
            action = "new"
            query = raw[1:]

        if action == "new":
            if not (query or "").strip():
                try:
                    self.notify(
                        "Busca: /texto  ·  /n  ·  /p",
                        severity="information",
                        timeout=3,
                    )
                except Exception:
                    pass
                return
            self._run_search(query, start_i=0)
            return

        if not (query or "").strip():
            try:
                self.notify(
                    "Sem busca ativa. Digite /texto (ou use a caixa abaixo).",
                    severity="warning",
                    timeout=3,
                )
            except Exception:
                pass
            return

        need_refresh = action == "repeat" or not self._search_hits
        if not need_refresh:
            try:
                log = self.query_one("#log", ViewRichLog)
                n_lines = len(getattr(log, "lines", None) or [])
                if n_lines <= 0 or any(
                    y < 0 or y >= n_lines for y in self._search_hits
                ):
                    need_refresh = True
            except Exception:
                need_refresh = True

        if need_refresh:
            start_i = -1 if action == "prev" else 0
            self._run_search(query, start_i=start_i)
            return

        n = len(self._search_hits)
        if n <= 0:
            self._run_search(query, start_i=0)
            return
        if action == "next":
            i = (int(self._search_i) + 1) % n
        elif action == "prev":
            i = (int(self._search_i) - 1) % n
        else:
            i = max(0, int(self._search_i)) % n
        self._search_i = i
        y = self._search_hits[i]
        try:
            log = self.query_one("#log", ViewRichLog)
            log.set_search_highlight(query, self._search_hits, y)
            log.scroll_to_content_y(y)
        except Exception:
            pass
        try:
            self.notify(
                f'Busca: "{query}" — {i + 1}/{n}',
                severity="information",
                timeout=2,
            )
        except Exception:
            pass

    def _clipboard_set(self, text: str) -> bool:
        text = text or ""
        if not text:
            return False
        try:
            self.copy_to_clipboard(text)
        except Exception:
            pass
        return _os_clipboard(text)

    def action_copy_selection(self) -> None:
        """Ctrl+C: copy mouse selection, or entire log if nothing selected."""
        selected = None
        try:
            selected = self.screen.get_selected_text()
        except Exception:
            selected = None
        if selected and selected.strip():
            selected = " ".join(selected.split())
            if self._clipboard_set(selected):
                try:
                    self.notify(
                        f"Seleção copiada ({len(selected)} chars)",
                        severity="information",
                        timeout=2,
                    )
                except Exception:
                    pass
                return
            try:
                self.notify("Falha ao copiar", severity="error", timeout=3)
            except Exception:
                pass
            return
        self.action_copy_log()

    def _get_log_plain_text(self) -> str:
        """Entire viewer scrollback as plain text (keeps newlines)."""
        text = ""
        try:
            log = self.query_one("#log", ViewRichLog)
            text = log.get_plain_text() or ""
        except Exception:
            text = ""
        if not (text or "").strip():
            try:
                log = self.query_one("#log", ViewRichLog)
                text = "\n".join(line.text for line in log.lines)
            except Exception:
                text = ""
        return (text or "").rstrip() + ("\n" if (text or "").strip() else "")

    def action_copy_log(self) -> None:
        """Ctrl+Shift+C / ``a`` / ``copy``: copy entire viewer scrollback."""
        text = self._get_log_plain_text().strip()
        if not text:
            try:
                self.notify("Log vazio", severity="warning", timeout=2)
            except Exception:
                pass
            return
        if self._clipboard_set(text):
            try:
                self.notify(
                    f"Tudo copiado ({len(text)} chars)",
                    severity="information",
                    timeout=2,
                )
            except Exception:
                pass
        else:
            try:
                self.notify("Falha ao copiar", severity="error", timeout=3)
            except Exception:
                pass

    def action_export_md(self, slug: str | None = None) -> None:
        """Ctrl+S / ``e`` / ``export``: write viewer content to a ``.md`` file."""
        body = self._get_log_plain_text().strip()
        if not body:
            try:
                self.notify("Log vazio — nada para exportar", severity="warning", timeout=2)
            except Exception:
                pass
            return

        title = _PANEL_TITLES.get(self.panel, self.panel)
        panel_slug = _PANEL_SLUGS.get(self.panel, self.panel)
        now = _dt.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        custom = (slug or "").strip()
        if custom:
            normalized = (
                unicodedata.normalize("NFKD", custom)
                .encode("ascii", "ignore")
                .decode("ascii")
            )
            safe = re.sub(r"[^\w\s-]", "", normalized.lower())
            safe = re.sub(r"[-\s]+", "-", safe).strip("-_") or panel_slug
            base = f"{date_str}_{safe}"
        else:
            safe = panel_slug
            base = f"{date_str}_livelingo-view-{panel_slug}"

        out_dir = Path.cwd()
        path = out_dir / f"{base}.md"
        # Avoid overwrite: append HHMMSS if file exists
        if path.exists():
            path = out_dir / f"{base}_{now.strftime('%H%M%S')}.md"
        filename = path.name

        md = (
            f"# LiveLingo View — {title}\n\n"
            f"- Painel: `{self.panel}`\n"
            f"- Host: `{self.host}:{self.port}`\n"
            f"- Exportado: {date_str} {time_str}\n"
            f"- Linhas: {body.count(chr(10)) + 1}\n"
            f"\n---\n\n"
            f"{body}\n"
        )
        try:
            path.write_text(md, encoding="utf-8")
        except Exception as exc:
            try:
                self.notify(f"Falha ao salvar: {exc}", severity="error", timeout=4)
            except Exception:
                pass
            return

        display = str(path.resolve())
        # Friendlier Windows path when running under WSL
        try:
            if display.startswith("/mnt/") and len(display) > 6 and display[5] == "/":
                drive = display[5].upper()
                rest = display[7:].replace("/", "\\")
                display = f"{drive}:\\{rest}"
        except Exception:
            pass
        try:
            self.notify(
                f"Exportado: {filename}",
                severity="information",
                timeout=4,
            )
        except Exception:
            pass
        # Also echo absolute path in status briefly
        try:
            st = self.query_one("#status", Static)
            st.update(f"● salvo → {display}")
        except Exception:
            pass

    def _paint_status(self, connected: bool) -> None:
        self._connected = connected
        try:
            st = self.query_one("#status", Static)
        except Exception:
            return
        title = _PANEL_TITLES.get(self.panel, self.panel)
        if connected:
            st.update(
                f"● {title}  ·  {self.host}:{self.port}  ·  "
                f"/ busca  ·  a copiar  ·  e/.md  ·  Ctrl+S  ·  [q]"
            )
            st.set_class(True, "-ok")
            st.set_class(False, "-wait")
        else:
            st.update(
                f"○ {title}  ·  reconectando {self.host}:{self.port} …  ·  "
                f"/ busca  ·  [q] sair"
            )
            st.set_class(False, "-ok")
            st.set_class(True, "-wait")

    def _read_loop(self) -> None:
        def _on_conn(ok: bool) -> None:
            try:
                self._q.put_nowait(("__status__", bool(ok)))
            except queue.Full:
                pass

        for ev in iter_events(
            self.host,
            self.port,
            stop_event=self._stop,
            reconnect=True,
            on_connection=_on_conn,
        ):
            if self._stop.is_set():
                break
            if not panel_matches(ev.get("panel") or "main", self.panel):
                continue
            try:
                self._q.put_nowait(ev)
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(ev)
                except queue.Full:
                    pass
        try:
            self._q.put_nowait(("__status__", False))
        except queue.Full:
            pass

    def _drain(self) -> None:
        try:
            log = self.query_one("#log", ViewRichLog)
        except Exception:
            return
        for _ in range(100):
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item and item[0] == "__status__":
                self._paint_status(bool(item[1]))
                continue
            if not isinstance(item, dict):
                continue
            if not self._connected:
                self._paint_status(True)
            kind = str(item.get("kind") or "info")
            text = item.get("text")
            if kind == "clear":
                try:
                    log.clear()
                except Exception:
                    pass
                self._search_hits = []
                self._search_i = -1
                continue
            markup = format_view_markup(kind, text if text is not None else "")
            if markup is None:
                continue
            try:
                log.write(markup)
            except Exception:
                try:
                    log.write(str(text or ""))
                except Exception:
                    pass


def run_panel_view(panel: str, *, host: str = "127.0.0.1", port: int = 8765) -> int:
    """Entry for ``main.py view …``. Returns process exit code."""
    try:
        panel = normalize_view_panel(panel)
    except ValueError as exc:
        print(str(exc))
        return 2
    app = PanelViewApp(panel, host=host, port=port)
    app.run()
    return 0
