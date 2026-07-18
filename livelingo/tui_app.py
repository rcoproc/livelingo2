"""
tui_app.py
==========
Textual TUI for LiveLingo: fixed listen header (robot + source/target) +
scrollable log + command input.

Pipeline (mic/STT/TTS) keeps running in background threads; this module only
owns the screen. Logs arrive via ui.set_log_sink; commands reuse main dispatch
in a worker thread with stdin/stdout proxies for prompts and prints.
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import traceback
from typing import Callable, Iterable

from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Vertical
from textual.selection import Selection
from textual.widgets import Footer, Header, Input, RichLog, Static

from . import ui as ui_mod

# Strip ANSI for log cleanliness when proxying print()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Command-line history (↑/↓ in #cmd), persisted under .cache/
_CMD_HISTORY_PATH = os.path.join(".cache", "cmd_history.txt")
_CMD_HISTORY_MAX = 100


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _os_clipboard(text: str) -> bool:
    """Best-effort OS clipboard (Windows / WSL / Linux). Returns True on success."""
    import subprocess

    text = text or ""
    if not text:
        return False
    # Windows clip.exe (also works from WSL → host clipboard)
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
    # PowerShell (large pastes via stdin — avoids cmdline length limits)
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


def _win_path_for_ps(path: str) -> str:
    """Convert WSL /mnt/c/... path to C:\\... for PowerShell / Edge on host."""
    p = os.path.abspath(path)
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p.replace("\\", "/"))
    if m:
        return f"{m.group(1).upper()}:\\" + m.group(2).replace("/", "\\")
    return p


def _svg_to_png(svg_path: str, png_path: str) -> bool:
    """
    Rasterize SVG → PNG. Tries cairosvg, then Chrome/Edge headless.
    Returns True if png_path was written.
    """
    import subprocess

    # 1) cairosvg (optional)
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(url=svg_path, write_to=png_path)
        if os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
            return True
    except Exception:
        pass

    # 2) Chrome / Edge headless screenshot of the SVG file
    browsers = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        # WSL-visible host paths
        "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
        "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
        "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    ]
    win_svg = _win_path_for_ps(svg_path)
    win_png = _win_path_for_ps(png_path)
    # file:///C:/Users/...
    file_url = "file:///" + win_svg.replace("\\", "/")
    for browser in browsers:
        if not os.path.isfile(browser):
            continue
        try:
            # Write PNG next to intended path; headless writes to cwd if relative
            out_arg = win_png
            r = subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--screenshot={out_arg}",
                    "--window-size=1400,900",
                    # Must be hex RGB/RGBA (8 digits). "0" aborts screenshot.
                    "--default-background-color=00000000",
                    file_url,
                ],
                capture_output=True,
                timeout=30,
            )
            # Browser may write to cwd with name screenshot.png — check both
            if os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
                return True
            # Sometimes written with Windows path only; try reading via /mnt
            if r.returncode == 0:
                for cand in (png_path, "screenshot.png", out_arg):
                    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        if cand != png_path:
                            try:
                                import shutil

                                shutil.copy2(cand, png_path)
                            except Exception:
                                continue
                        if os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
                            return True
        except Exception:
            continue

    # 3) ImageMagick
    for magick in ("magick", "convert"):
        try:
            r = subprocess.run(
                [magick, svg_path, png_path],
                capture_output=True,
                timeout=20,
            )
            if r.returncode == 0 and os.path.isfile(png_path):
                return True
        except Exception:
            pass
    return False


def _powershell_exe() -> list[str]:
    """Candidate PowerShell paths (Windows host + WSL)."""
    out: list[str] = []
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        out.append(name)
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    out.append(os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    out.append(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    # WSL mounts of the Windows host
    for letter in "cdefgh":
        out.append(
            f"/mnt/{letter}/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
    # de-dupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _clipboard_set_image(path: str) -> bool:
    """
    Put a raster image file on the OS clipboard (Windows Forms / xclip).
    Prefer PNG/BMP. Returns True on success.

    Notes (Windows):
      - Must run PowerShell STA for Clipboard APIs.
      - Use SetDataObject(..., $true) so data survives process exit
        (SetImage alone clears when PS exits).
      - From WSL, call host powershell.exe via full /mnt/c/... path.
    """
    import subprocess

    if not path or not os.path.isfile(path):
        return False
    win = _win_path_for_ps(path)
    # Escape single quotes for PowerShell single-quoted string
    win_esc = win.replace("'", "''")
    # SetDataObject copy=$true keeps image after PowerShell exits.
    # -STA is required for System.Windows.Forms.Clipboard.
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        f"$p = '{win_esc}'; "
        "if (-not (Test-Path -LiteralPath $p)) { exit 2 }; "
        "$img = [System.Drawing.Image]::FromFile((Resolve-Path -LiteralPath $p).Path); "
        "try { "
        "[System.Windows.Forms.Clipboard]::SetDataObject($img, $true) "
        "} finally { $img.Dispose() }"
    )
    for ps_bin in _powershell_exe():
        # Skip non-existent absolute paths (WSL candidates)
        if (ps_bin.startswith("/") or (len(ps_bin) > 2 and ps_bin[1] == ":")) and not os.path.isfile(
            ps_bin
        ):
            continue
        for args in (
            [ps_bin, "-STA", "-NoProfile", "-NonInteractive", "-Command", ps],
            [ps_bin, "-NoProfile", "-NonInteractive", "-Command", ps],
        ):
            try:
                r = subprocess.run(
                    args,
                    capture_output=True,
                    timeout=25,
                )
                if r.returncode == 0:
                    return True
            except FileNotFoundError:
                break
            except Exception:
                continue
    # Linux: xclip / wl-copy image
    for cmd in (
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", path],
        ["xclip", "-selection", "clipboard", "-t", "image/png", path],
        ["wl-copy", "--type", "image/png"],
    ):
        try:
            if cmd[0] == "wl-copy":
                with open(path, "rb") as fh:
                    r = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=8)
            else:
                r = subprocess.run(cmd, capture_output=True, timeout=8)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


class SelectableRichLog(RichLog):
    """
    RichLog with character-level mouse selection + plain-text export.

    Upstream RichLog:
      1) does not implement get_selection()
      2) does not call Strip.apply_offsets() — so the compositor never
         gets content (x,y) under the mouse → Textual falls back to
         SELECT_ALL (entire log blue).

    We mirror the built-in Log widget: apply_offsets + get_selection + highlight.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._plain_lines: list[str] = []

    def write(
        self,
        content,
        width=None,
        expand=False,
        shrink=True,
        scroll_end=None,
        animate=False,
    ):
        # Capture plain text for copy-all (logical lines, before wrap).
        try:
            if isinstance(content, str):
                plain = content
            else:
                plain = str(content)
            try:
                from rich.text import Text

                plain = Text.from_markup(plain).plain
            except Exception:
                plain = re.sub(r"\[/?[^\]]*\]", "", plain)
            for line in (plain or "").splitlines() or [""]:
                self._plain_lines.append(line)
            max_n = self.max_lines
            if max_n is not None and len(self._plain_lines) > max_n:
                self._plain_lines = self._plain_lines[-max_n:]
        except Exception:
            pass
        return super().write(
            content,
            width=width,
            expand=expand,
            shrink=shrink,
            scroll_end=scroll_end,
            animate=animate,
        )

    def clear(self) -> None:
        self._plain_lines.clear()
        try:
            return super().clear()
        except Exception:
            return None

    def get_plain_text(self) -> str:
        """Full log as plain text (for copy-all)."""
        if self._plain_lines:
            return "\n".join(self._plain_lines)
        try:
            return "\n".join(line.text for line in self.lines)
        except Exception:
            return ""

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract selected text (required for Ctrl+C / mouse copy)."""
        if selection is None:
            return None
        # Rendered strips: y/x match on-screen wrapped lines + apply_offsets.
        try:
            if self.lines:
                text = "\n".join(line.text for line in self.lines)
                return selection.extract(text), "\n"
        except Exception:
            pass
        if self._plain_lines:
            return selection.extract("\n".join(self._plain_lines)), "\n"
        return None

    def selection_updated(self, selection: Selection | None) -> None:
        try:
            self._line_cache.clear()
        except Exception:
            pass
        self.refresh()

    def render_line(self, y: int):
        """
        Render a visible row and stamp content coords on each cell.

        apply_offsets is what makes click-drag select words/phrases instead
        of the whole log (Textual reads meta['offset'] under the cursor).
        """
        from textual.strip import Strip as TStrip

        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y
        try:
            width = self.scrollable_content_region.width
        except Exception:
            width = self.size.width
        line = self._render_line(content_y, scroll_x, width)
        strip = line.apply_style(self.rich_style)
        # Content coords (not just screen y) — same as textual.widgets.Log
        try:
            strip = strip.apply_offsets(scroll_x, content_y)
        except Exception:
            pass
        return strip

    def _render_line(self, y: int, scroll_x: int, width: int):
        """Render content line y; apply selection highlight when active."""
        from rich.cells import cell_len
        from rich.style import Style
        from rich.text import Text
        from textual.strip import Strip as TStrip

        if y >= len(self.lines):
            return TStrip.blank(width, self.rich_style)

        selection = self.text_selection
        if selection is None:
            return super()._render_line(y, scroll_x, width)

        # Never paint SELECT_ALL (None, None) as full-widget blue fill —
        # that happens only when offsets are missing; once apply_offsets is
        # wired, normal drags use real start/end. Still guard for safety.
        if selection.start is None and selection.end is None:
            return super()._render_line(y, scroll_x, width)

        try:
            full = self.lines[y]
            line_text = Text(full.text, no_wrap=True)
            span = selection.get_span(y)
            if span is not None:
                start, end = span
                if end == -1:
                    end = len(line_text)
                try:
                    sel_style = self.screen.get_component_rich_style(
                        "screen--selection"
                    )
                except Exception:
                    # Soft yellow highlight — never reverse/navy "erase" look
                    sel_style = Style(bgcolor="#f0d78c", color="#1a1b26", bold=True)
                # If theme only set bgcolor and left color empty/dark-on-dark, force contrast
                if sel_style.bgcolor is not None and (
                    sel_style.color is None
                    or str(sel_style.bgcolor).lower()
                    in ("#0000af", "#000080", "blue", "navy", "#0a2540")
                ):
                    sel_style = Style(bgcolor="#f0d78c", color="#1a1b26", bold=True)
                start = max(0, min(int(start), len(line_text)))
                end = max(start, min(int(end), len(line_text)))
                if end > start:
                    line_text.stylize(sel_style, start, end)
            strip = TStrip(
                line_text.render(self.app.console),
                cell_len(full.text),
            )
            return strip.crop_extend(scroll_x, scroll_x + width, self.rich_style)
        except Exception:
            return super()._render_line(y, scroll_x, width)


class _StdinProxy:
    """Blocks worker threads until the TUI provides a line of input."""

    def __init__(self, app: "LiveLingoApp"):
        self._app = app

    def readline(self, size: int = -1) -> str:  # noqa: ARG002
        return self._app._wait_for_prompt_line()

    def read(self, size: int = -1) -> str:  # noqa: ARG002
        return self.readline()


class _StdoutProxy:
    """Route print() from command workers into the TUI log (thread-safe)."""

    def __init__(self, app: "LiveLingoApp", real):
        self._app = app
        self._real = real
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, data: str) -> int:
        if not data:
            return 0
        with self._lock:
            text = _strip_ansi(data).replace("\r", "")
            if not text and "\n" not in data:
                return len(data)
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._enqueue(line)
            return len(data)

    def _enqueue(self, line: str) -> None:
        line = (line or "").rstrip()
        if not line:
            return
        # NEVER touch widgets from a worker thread — always hop to the UI loop.
        try:
            self._app.call_from_thread(self._app.post_log, "raw", line)
        except Exception:
            # App may be shutting down
            pass

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self._enqueue(self._buf)
                self._buf = ""

    def isatty(self) -> bool:
        return False

    def fileno(self):
        try:
            return self._real.fileno()
        except Exception:
            raise OSError("no fileno")


# Language display names (same map used by classic list/export).
_LANG_NAMES = {
    "fr": "Frances",
    "en": "Ingles",
    "pt": "Portugues",
    "es": "Espanhol",
    "de": "Alemao",
    "it": "Italiano",
    "zh": "Chines",
    "ja": "Japones",
}

# Footer menu + command placeholder by SOURCE_LANG (keys stay e/r/s…; labels translate).
# Short labels so fixed columns (cw=13) still align.
_FOOTER_I18N = {
    "en": {
        "sentence": "Sentence",
        "audio": "Audio",
        "idiom": "Idiom",
        "edit": "Edit",
        "edit_n": "Edit N",
        "del": "Del",
        "del_n": "Del N",
        "fav": "Fav",
        "fav_n": "Fav N",
        "favs": "Favs",
        "list": "List",
        "list_src": "Src only",
        "list_tgt": "Tgt only",
        "comment": "Comment",
        "comment_n": "Comm N",
        "cls": "Clear",
        "go_top": "Go top",
        "go_footer": "Go foot",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "snd": "Snd",
        "mic": "Mic",
        "stop": "Stop",
        "path": "Path",
        "path_n": "Path N",
        "folder": "Folder",
        "swap": "Swap",
        "target": "Target",
        "synonyms": "Synonyms",
        "session": "Session",
        "menu": "Menu",
        "quit": "Quit",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUTED",
        "placeholder": "Type a command and Enter (e.g. s, g, gt, gf, cls, q)…",
        "prompt_placeholder": "Type the answer and Enter…",
        "starting": "starting listen…",
        "g_swap": "g(swap)",
        "t_target": "t(target)",
    },
    "pt": {
        "sentence": "Frase",
        "audio": "Audio",
        "idiom": "Idioma",
        "edit": "Editar",
        "edit_n": "Edit N",
        "del": "Apagar",
        "del_n": "Apag N",
        "fav": "Fav",
        "fav_n": "Fav N",
        "favs": "Favs",
        "list": "Lista",
        "list_src": "So src",
        "list_tgt": "So tgt",
        "comment": "Coment",
        "comment_n": "Com N",
        "cls": "Limpar",
        "go_top": "Topo",
        "go_footer": "Rodape",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "snd": "Som",
        "mic": "Mic",
        "stop": "Parar",
        "path": "Path",
        "path_n": "Path N",
        "folder": "Pasta",
        "swap": "Trocar",
        "target": "Alvo",
        "synonyms": "Sinonimos",
        "session": "Sessao",
        "menu": "Menu",
        "quit": "Sair",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUDO",
        "placeholder": "Digite um comando e Enter (ex: s, g, gt, gf, cls, q)…",
        "prompt_placeholder": "Digite a resposta e Enter…",
        "starting": "iniciando escuta…",
        "g_swap": "g(trocar)",
        "t_target": "t(alvo)",
    },
    "es": {
        "sentence": "Frase",
        "audio": "Audio",
        "idiom": "Idioma",
        "edit": "Editar",
        "edit_n": "Edit N",
        "del": "Borrar",
        "del_n": "Borr N",
        "fav": "Fav",
        "fav_n": "Fav N",
        "favs": "Favs",
        "list": "Lista",
        "cls": "Limpiar",
        "go_top": "Inicio",
        "go_footer": "Final",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "snd": "Son",
        "mic": "Mic",
        "stop": "Parar",
        "path": "Ruta",
        "path_n": "Ruta N",
        "folder": "Carpeta",
        "swap": "Cambiar",
        "target": "Destino",
        "synonyms": "Sinonimos",
        "session": "Sesion",
        "menu": "Menu",
        "quit": "Salir",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUDO",
        "placeholder": "Escriba un comando y Enter (ej: s, g, gt, gf, l, q)…",
        "prompt_placeholder": "Escriba la respuesta y Enter…",
        "starting": "iniciando escucha…",
        "g_swap": "g(cambiar)",
        "t_target": "t(destino)",
    },
    "fr": {
        "sentence": "Phrase",
        "audio": "Audio",
        "idiom": "Langue",
        "edit": "Edit",
        "edit_n": "Edit N",
        "del": "Suppr",
        "del_n": "Suppr N",
        "fav": "Fav",
        "fav_n": "Fav N",
        "favs": "Favs",
        "list": "Liste",
        "cls": "Effacer",
        "go_top": "Haut",
        "go_footer": "Bas",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "snd": "Son",
        "mic": "Mic",
        "stop": "Stop",
        "path": "Chemin",
        "path_n": "Chem N",
        "folder": "Dossier",
        "swap": "Echange",
        "target": "Cible",
        "synonyms": "Synonymes",
        "session": "Session",
        "menu": "Menu",
        "quit": "Quitter",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUET",
        "placeholder": "Tapez une commande et Entree (ex: s, g, gt, gf, l, q)…",
        "prompt_placeholder": "Tapez la reponse et Entree…",
        "starting": "demarrage ecoute…",
        "g_swap": "g(echange)",
        "t_target": "t(cible)",
    },
    "de": {
        "sentence": "Satz",
        "audio": "Audio",
        "idiom": "Sprache",
        "edit": "Edit",
        "edit_n": "Edit N",
        "del": "Losch",
        "del_n": "Losch N",
        "fav": "Fav",
        "fav_n": "Fav N",
        "favs": "Favs",
        "list": "Liste",
        "cls": "Leeren",
        "go_top": "Oben",
        "go_footer": "Unten",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "snd": "Ton",
        "mic": "Mic",
        "stop": "Stop",
        "path": "Pfad",
        "path_n": "Pfad N",
        "folder": "Ordner",
        "swap": "Tausch",
        "target": "Ziel",
        "synonyms": "Synonyme",
        "session": "Sitzung",
        "menu": "Menu",
        "quit": "Ende",
        "on": "AN",
        "off": "AUS",
        "live": "LIVE",
        "muted": "STUMM",
        "placeholder": "Befehl eingeben und Enter (z.B. s, g, gt, gf, l, q)…",
        "prompt_placeholder": "Antwort eingeben und Enter…",
        "starting": "hoere zu…",
        "g_swap": "g(tausch)",
        "t_target": "t(ziel)",
    },
    "it": {
        "sentence": "Frase",
        "audio": "Audio",
        "idiom": "Lingua",
        "edit": "Modif",
        "edit_n": "Mod N",
        "del": "Elim",
        "del_n": "Elim N",
        "fav": "Fav",
        "fav_n": "Fav N",
        "favs": "Favs",
        "list": "Lista",
        "cls": "Pulisci",
        "go_top": "Inizio",
        "go_footer": "Fine",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "snd": "Audio",
        "mic": "Mic",
        "stop": "Stop",
        "path": "Path",
        "path_n": "Path N",
        "folder": "Cartella",
        "swap": "Scambia",
        "target": "Target",
        "synonyms": "Sinonimi",
        "session": "Sessione",
        "menu": "Menu",
        "quit": "Esci",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUTO",
        "placeholder": "Digita un comando e Invio (es: s, g, gt, gf, l, q)…",
        "prompt_placeholder": "Digita la risposta e Invio…",
        "starting": "avvio ascolto…",
        "g_swap": "g(scambia)",
        "t_target": "t(target)",
    },
    "zh": {
        "sentence": "句子",
        "audio": "音频",
        "idiom": "语言",
        "edit": "编辑",
        "edit_n": "编辑N",
        "del": "删除",
        "del_n": "删除N",
        "fav": "收藏",
        "fav_n": "收藏N",
        "favs": "收藏夹",
        "list": "列表",
        "cls": "清空",
        "go_top": "顶部",
        "go_footer": "底部",
        "export": "导出",
        "replay": "重播",
        "replay_n": "重播N",
        "snd": "声音",
        "mic": "麦克",
        "stop": "停止",
        "path": "路径",
        "path_n": "路径N",
        "folder": "文件夹",
        "swap": "交换",
        "target": "目标",
        "synonyms": "同义词",
        "session": "会话",
        "menu": "菜单",
        "quit": "退出",
        "on": "开",
        "off": "关",
        "live": "开麦",
        "muted": "静音",
        "placeholder": "输入命令后回车 (如 s, g, gt, gf, l, q)…",
        "prompt_placeholder": "输入回答后回车…",
        "starting": "开始监听…",
        "g_swap": "g(交换)",
        "t_target": "t(目标)",
    },
    "ja": {
        "sentence": "文",
        "audio": "音声",
        "idiom": "言語",
        "edit": "編集",
        "edit_n": "編集N",
        "del": "削除",
        "del_n": "削除N",
        "fav": "お気に",
        "fav_n": "お気N",
        "favs": "一覧",
        "list": "一覧",
        "cls": "消去",
        "go_top": "先頭",
        "go_footer": "末尾",
        "export": "書出",
        "replay": "再生",
        "replay_n": "再生N",
        "snd": "音",
        "mic": "Mic",
        "stop": "停止",
        "path": "Path",
        "path_n": "PathN",
        "folder": "Folder",
        "swap": "入替",
        "target": "対象",
        "synonyms": "類語",
        "session": "Session",
        "menu": "Menu",
        "quit": "終了",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUTE",
        "placeholder": "コマンドを入力してEnter (例: s, g, gt, gf, l, q)…",
        "prompt_placeholder": "回答を入力してEnter…",
        "starting": "待受中…",
        "g_swap": "g(入替)",
        "t_target": "t(対象)",
    },
}


def _source_lang_code() -> str:
    try:
        import config as cfg

        code = (getattr(cfg, "SOURCE_LANG", "en") or "en").lower().strip()
    except Exception:
        code = "en"
    if "-" in code:
        code = code.split("-", 1)[0]
    if code in ("cn", "zh-cn", "zh-tw", "cmn"):
        code = "zh"
    if code in ("jp",):
        code = "ja"
    if code in ("ger", "deu"):
        code = "de"
    if code in ("ita",):
        code = "it"
    if code in ("por", "pt-br", "pt_br"):
        code = "pt"
    return code if code in _FOOTER_I18N else "en"


def _footer_i18n() -> dict:
    """Labels for footer menu / placeholder in current SOURCE_LANG."""
    pack = _FOOTER_I18N.get(_source_lang_code()) or _FOOTER_I18N["en"]
    # Fill any missing keys from English
    base = dict(_FOOTER_I18N["en"])
    base.update(pack)
    return base


# Command palette: titles stay English; help text under each option = SOURCE_LANG.
_PALETTE_HELP_I18N = {
    "en": {
        "theme": "Change the current theme",
        "quit": "Quit the application as soon as possible",
        "keys_show": "Show help for the focused widget and a summary of available keys",
        "keys_hide": "Hide the keys and widget help panel",
        "minimize": "Minimize the widget and restore to normal size",
        "maximize": "Maximize the focused widget",
        "screenshot": "Save screenshot (SVG+PNG) and copy image to clipboard",
    },
    "pt": {
        "theme": "Alterar o tema atual",
        "quit": "Sair da aplicação o mais rápido possível",
        "keys_show": "Mostrar ajuda do widget focado e um resumo das teclas",
        "keys_hide": "Ocultar o painel de teclas e ajuda do widget",
        "minimize": "Minimizar o widget e restaurar o tamanho normal",
        "maximize": "Maximizar o widget focado",
        "screenshot": "Salvar screenshot (SVG+PNG) e copiar imagem para a area de transferencia",
    },
    "es": {
        "theme": "Cambiar el tema actual",
        "quit": "Salir de la aplicación lo antes posible",
        "keys_show": "Mostrar ayuda del widget enfocado y un resumen de teclas",
        "keys_hide": "Ocultar el panel de teclas y ayuda del widget",
        "minimize": "Minimizar el widget y restaurar el tamaño normal",
        "maximize": "Maximizar el widget enfocado",
        "screenshot": "Guardar una 'captura' SVG de la pantalla actual",
    },
    "fr": {
        "theme": "Changer le thème actuel",
        "quit": "Quitter l'application dès que possible",
        "keys_show": "Afficher l'aide du widget focalisé et un résumé des touches",
        "keys_hide": "Masquer le panneau des touches et l'aide du widget",
        "minimize": "Réduire le widget et restaurer la taille normale",
        "maximize": "Agrandir le widget focalisé",
        "screenshot": "Enregistrer une 'capture' SVG de l'écran actuel",
    },
    "de": {
        "theme": "Aktuelles Design ändern",
        "quit": "Anwendung so schnell wie möglich beenden",
        "keys_show": "Hilfe für das fokussierte Widget und Tastenübersicht anzeigen",
        "keys_hide": "Tasten- und Widget-Hilfepanel ausblenden",
        "minimize": "Widget minimieren und Normalgröße wiederherstellen",
        "maximize": "Fokussiertes Widget maximieren",
        "screenshot": "SVG-'Screenshot' des aktuellen Bildschirms speichern",
    },
    "it": {
        "theme": "Cambia il tema corrente",
        "quit": "Esci dall'applicazione il prima possibile",
        "keys_show": "Mostra aiuto del widget attivo e riepilogo dei tasti",
        "keys_hide": "Nascondi il pannello tasti e aiuto del widget",
        "minimize": "Riduci il widget e ripristina la dimensione normale",
        "maximize": "Ingrandisci il widget attivo",
        "screenshot": "Salva uno 'screenshot' SVG della schermata corrente",
    },
    "zh": {
        "theme": "更改当前主题",
        "quit": "尽快退出应用程序",
        "keys_show": "显示焦点控件的帮助和可用快捷键摘要",
        "keys_hide": "隐藏按键与控件帮助面板",
        "minimize": "最小化控件并恢复正常大小",
        "maximize": "最大化焦点控件",
        "screenshot": "保存当前屏幕的 SVG 截图",
    },
    "ja": {
        "theme": "現在のテーマを変更",
        "quit": "できるだけ早くアプリを終了",
        "keys_show": "フォーカス中のウィジェットのヘルプとキー一覧を表示",
        "keys_hide": "キーとウィジェットのヘルプパネルを隠す",
        "minimize": "ウィジェットを最小化して通常サイズに戻す",
        "maximize": "フォーカス中のウィジェットを最大化",
        "screenshot": "現在の画面の SVG スクリーンショットを保存",
    },
}


def _palette_help() -> dict:
    pack = _PALETTE_HELP_I18N.get(_source_lang_code()) or _PALETTE_HELP_I18N["en"]
    base = dict(_PALETTE_HELP_I18N["en"])
    base.update(pack)
    return base

# Classic listen-indicator frames (robot idle / mic active).
_IDLE_FRAMES = (
    "🤖 [ •       ]",
    "🤖 [  •      ]",
    "🤖 [   •     ]",
    "🤖 [    •    ]",
    "🤖 [     •   ]",
    "🤖 [      •  ]",
    "🤖 [       • ]",
    "🤖 [      •  ]",
    "🤖 [     •   ]",
    "🤖 [    •    ]",
    "🤖 [   •     ]",
    "🤖 [  •      ]",
)
_ACTIVE_FRAMES = (
    "🎙️  [  •      ]",
    "🎙️  [  ••     ]",
    "🎙️  [  •••    ]",
    "🎙️  [   •••   ]",
    "🎙️  [    •••  ]",
    "🎙️  [     ••  ]",
    "🎙️  [      •  ]",
    "🎙️  [     ••  ]",
    "🎙️  [    •••  ]",
    "🎙️  [   •••   ]",
    "🎙️  [  •••    ]",
    "🎙️  [  ••     ]",
)


def _lang_code(code: str) -> str:
    c = (code or "?").lower().strip()
    if "-" in c:
        c = c.split("-", 1)[0]
    return c or "?"


def _display_lang_code(code: str) -> str:
    """Header-only label: PT → BR (config/STT still use pt)."""
    c = (code or "?").upper().strip()
    if c == "PT":
        return "BR"
    return c or "?"


def _lang_pair_parts():
    """Return (src_code, tgt_code, src_name, tgt_name, pair_short, pair_long)."""
    try:
        import config as cfg

        src = _lang_code(getattr(cfg, "SOURCE_LANG", "") or "?")
        tgt = _lang_code(getattr(cfg, "TARGET_LANG", "") or "?")
    except Exception:
        src, tgt = "?", "?"
    src_u, tgt_u = src.upper(), tgt.upper()
    src_n = _LANG_NAMES.get(src, src_u)
    tgt_n = _LANG_NAMES.get(tgt, tgt_u)
    # Display codes: PT shown as BR only in UI chrome (not real lang code)
    src_d, tgt_d = _display_lang_code(src_u), _display_lang_code(tgt_u)
    pair_short = f"{src_d} → {tgt_d}"
    pair_long = f"{src_d} ({src_n}) → {tgt_d} ({tgt_n})"
    return src_u, tgt_u, src_n, tgt_n, pair_short, pair_long


class LiveLingoApp(App):
    """Main LiveLingo TUI — fixed listen header (robot + langs) + scrollable log."""

    TITLE = "LiveLingo"
    SUB_TITLE = "real-time voice translation"
    # Continuous Unicode box borders (like Grok TUI). Prefer Windows Terminal /
    # modern conhost — legacy CP437 may show wrong glyphs.
    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }
    /* Soft selection: light highlight + dark text (not solid navy "erase") */
    Screen > .screen--selection {
        background: #f0d78c;
        color: #1a1b26;
        text-style: bold;
    }
    Header {
        dock: top;
        background: $primary;
        color: $text;
        text-style: bold;
        /* Default Header is 3 rows (tall); keep chrome compact */
        height: 1;
    }
    /* Exactly 1 row for robot line — no border (border ate rows / left blank). */
    #listen-header {
        dock: top;
        height: 1;
        min-height: 1;
        max-height: 1;
        background: #e0a020;
        color: #1a1b26;
        padding: 0 1;
        border: none;
        text-style: bold;
        content-align: left middle;
        overflow: hidden;
    }
    #listen-header.sound-on {
        background: #3d9a5f;
        color: #ffffff;
    }
    #listen-header.mic-muted {
        background: #c23b3b;
        color: #ffffff;
    }
    Footer {
        dock: bottom;
        background: $panel;
        height: 1;
    }
    #log {
        height: 1fr;
        margin: 0;
        padding: 0 1;
        background: $surface;
        border: solid $accent;
        scrollbar-size: 1 1;
        width: 1fr;
    }
    /* Menu + command input + 1 row air below input (before Textual Footer) */
    #bottom {
        dock: bottom;
        height: 10;
        layout: vertical;
        background: $panel;
        border-top: solid $accent;
        padding: 0 0 1 0;
    }
    /* Fixed-column cheat-sheet */
    #hint {
        height: 5;
        min-height: 4;
        max-height: 5;
        color: $text;
        padding: 0 1;
        background: $panel;
        content-align: left top;
        overflow: hidden;
    }
    #cmd-row {
        height: 4;
        min-height: 4;
        padding: 0 1 1 1;
        background: $panel;
        layout: vertical;
    }
    #cmd {
        width: 1fr;
        height: 3;
        border: solid $primary;
        background: $surface;
    }
    #cmd:focus {
        border: solid $accent;
    }

    /* ---- Command palette (Ctrl+P): continuous box lines, not hkey/???? ---- */
    CommandPalette #--input {
        border: solid $accent;
    }
    CommandPalette #--input.--list-visible {
        border: solid $accent;
        border-bottom: solid $accent;
    }
    CommandPalette LoadingIndicator {
        border-bottom: solid $accent;
    }
    CommandPalette > Vertical {
        border: solid $accent;
    }
    CommandList {
        border-top: solid $accent;
        border-bottom: solid $accent;
        border-left: solid $accent;
        border-right: solid $accent;
    }
    CommandList:focus {
        border: solid $accent;
    }
    CommandPalette OptionList {
        border: solid $accent;
    }
    """

    # Ctrl+C = selection (or full log if none); Ctrl+Shift+C / F2 = always full log.
    BINDINGS = [
        Binding("ctrl+c", "copy_selection", "Copy", show=True, priority=True),
        Binding(
            "ctrl+shift+c",
            "copy_log",
            "Copy log",
            show=True,
            priority=True,
        ),
        Binding("ctrl+q", "quit_app", "Quit", show=True, priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("f2", "copy_log", "Copy log", show=True, priority=True),
    ]

    ALLOW_SELECT = True
    ENABLE_SELECT_AUTO_SCROLL = True

    def __init__(
        self,
        pipeline,
        synonym_lookup,
        dispatch_command: Callable,
        listen_msgs_fn: Callable,
        help_fn: Callable | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.pipeline = pipeline
        self.synonym_lookup = synonym_lookup
        self._dispatch = dispatch_command
        self._listen_msgs_fn = listen_msgs_fn
        self._help_fn = help_fn
        self._prompt_q: queue.Queue = queue.Queue()
        self._prompt_waiting = threading.Event()
        self._prompt_label = ""
        # When True, force #cmd keystrokes/value to UPPERCASE (command [t] only).
        self._prompt_force_upper = False
        self._cmd_busy = False
        self._frame_i = 0
        self._speaking = False
        self._sound_on = False
        self._mic_muted = False
        self._log_queue: queue.Queue = queue.Queue()
        self._cached_log_width = 120
        # Command history (↑/↓) — list of past submissions; index -1 = draft line
        self._cmd_history: list[str] = []
        self._cmd_history_i: int = -1
        self._cmd_draft: str = ""

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        """
        Command palette entries: English titles; help text = SOURCE_LANG.

        Only the subtitle under each option is localized (user request).
        """
        h = _palette_help()
        # Titles remain English (search/match stable); help follows SOURCE.
        yield SystemCommand("Theme", h["theme"], self.action_change_theme)
        yield SystemCommand("Quit", h["quit"], self.action_quit)

        try:
            has_help = bool(screen.query("HelpPanel"))
        except Exception:
            has_help = False
        if has_help:
            yield SystemCommand(
                "Keys", h["keys_hide"], self.action_hide_help_panel
            )
        else:
            yield SystemCommand(
                "Keys", h["keys_show"], self.action_show_help_panel
            )

        try:
            maximized = screen.maximized is not None
            focused = screen.focused
            allow_max = focused is not None and getattr(
                focused, "allow_maximize", False
            )
        except Exception:
            maximized = False
            allow_max = False
        if maximized:
            yield SystemCommand(
                "Minimize", h["minimize"], screen.action_minimize
            )
        elif allow_max:
            yield SystemCommand(
                "Maximize", h["maximize"], screen.action_maximize
            )

        yield SystemCommand(
            "Screenshot",
            h["screenshot"],
            lambda: self.set_timer(0.1, self.action_livelingo_screenshot),
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Fixed top listen bar — single row only (robot + pair + audio + status)
        yield Static(_footer_i18n()["starting"], id="listen-header", markup=False)
        yield SelectableRichLog(
            id="log",
            highlight=False,  # avoid markup glitches on Windows legacy console
            markup=True,
            wrap=True,
            auto_scroll=True,
            max_lines=5000,
            min_width=20,
        )
        with Vertical(id="bottom"):
            yield Static("", id="hint", markup=True)
            with Vertical(id="cmd-row"):
                yield Input(
                    placeholder=_footer_i18n()["placeholder"],
                    id="cmd",
                )
        yield Footer()

    def on_mount(self) -> None:
        ui_mod.set_log_sink(self._sink_from_worker)
        ui_mod.set_width_provider(self._log_content_width)
        self._load_cmd_history()
        # Drain queued log lines from UI thread (safe for RichLog).
        self.set_interval(0.05, self._drain_log_queue)
        # ~0.15s tick so robot bounce feels smooth (classic was 0.12–0.25s)
        self.set_interval(0.15, self._tick_status)
        self.set_interval(0.5, self._refresh_log_width)
        self.set_interval(1.0, self._refresh_cmd_menu)
        self._refresh_log_width()
        self._refresh_cmd_menu()
        log = self.query_one("#log", SelectableRichLog)
        log.write(
            "[bold cyan]LiveLingo TUI[/] — log rolavel | "
            "[bold yellow]escuta fixa no header[/]"
        )
        log.write(
            "[dim]Fale no microfone — Heard/Translated aparecem aqui. "
            "Comandos: digite + Enter | setas ↑↓ = historico de comandos.[/]"
        )
        log.write(
            "[yellow]Audio OFF por padrao — [s] para ouvir ao vivo | "
            "[r]/[rN] um chunk | [l] lista frases | [g] swap idiomas[/]"
        )
        log.write(
            "[bold green]Copiar:[/] clique e arraste no log → [bold]Ctrl+C[/]  ·  "
            "log inteiro [bold]Ctrl+Shift+C[/] / F2  ·  "
            "ou Shift+arrastar (selecao nativa do Windows Terminal)"
        )
        log.write(
            "[dim]Dica: Windows Terminal recomendado. Sair: Ctrl+Q ou [q].[/]"
        )
        self.query_one("#cmd", Input).focus()
        try:
            self._sound_on = bool(self.pipeline.is_sound_enabled())
            self._mic_muted = bool(self.pipeline.is_mic_muted())
        except Exception:
            pass
        self._tick_status()

    def on_unmount(self) -> None:
        ui_mod.set_log_sink(None)
        ui_mod.set_width_provider(None)
        try:
            self._save_cmd_history()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Command history (↑ / ↓)
    # ------------------------------------------------------------------ #
    def _load_cmd_history(self) -> None:
        """Load past commands from .cache/cmd_history.txt."""
        try:
            if not os.path.isfile(_CMD_HISTORY_PATH):
                return
            with open(_CMD_HISTORY_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip("\n\r") for ln in f.readlines()]
            self._cmd_history = [
                ln for ln in lines if ln.strip()
            ][-_CMD_HISTORY_MAX:]
        except Exception:
            self._cmd_history = []
        self._cmd_history_i = -1
        self._cmd_draft = ""

    def _save_cmd_history(self) -> None:
        try:
            parent = os.path.dirname(_CMD_HISTORY_PATH) or ".cache"
            os.makedirs(parent, exist_ok=True)
            with open(_CMD_HISTORY_PATH, "w", encoding="utf-8") as f:
                for line in self._cmd_history[-_CMD_HISTORY_MAX:]:
                    f.write(line.replace("\n", " ").strip() + "\n")
        except Exception:
            pass

    def _push_cmd_history(self, value: str) -> None:
        """Remember a submitted command (skip empties / consecutive dupes)."""
        v = (value or "").strip()
        if not v:
            return
        if self._cmd_history and self._cmd_history[-1] == v:
            self._cmd_history_i = -1
            self._cmd_draft = ""
            return
        self._cmd_history.append(v)
        if len(self._cmd_history) > _CMD_HISTORY_MAX:
            self._cmd_history = self._cmd_history[-_CMD_HISTORY_MAX:]
        self._cmd_history_i = -1
        self._cmd_draft = ""
        self._save_cmd_history()

    def _history_up(self) -> None:
        if not self._cmd_history:
            return
        inp = self._cmd_input()
        if inp is None:
            return
        if self._cmd_history_i < 0:
            self._cmd_draft = inp.value or ""
            self._cmd_history_i = len(self._cmd_history) - 1
        elif self._cmd_history_i > 0:
            self._cmd_history_i -= 1
        else:
            return  # already at oldest
        try:
            inp.value = self._cmd_history[self._cmd_history_i]
            inp.cursor_position = len(inp.value or "")
        except Exception:
            pass

    def _history_down(self) -> None:
        if self._cmd_history_i < 0:
            return
        inp = self._cmd_input()
        if inp is None:
            return
        if self._cmd_history_i < len(self._cmd_history) - 1:
            self._cmd_history_i += 1
            try:
                inp.value = self._cmd_history[self._cmd_history_i]
                inp.cursor_position = len(inp.value or "")
            except Exception:
                pass
        else:
            # Past newest → restore draft
            self._cmd_history_i = -1
            try:
                inp.value = self._cmd_draft
                inp.cursor_position = len(inp.value or "")
            except Exception:
                pass
            self._cmd_draft = ""

    def _refresh_log_width(self) -> None:
        """Cache log content width on the UI thread (safe for worker reads)."""
        try:
            log = self.query_one("#log")
            cs = getattr(log, "content_size", None)
            if cs is not None:
                cw = int(getattr(cs, "width", 0) or 0)
                if cw >= 24:
                    self._cached_log_width = max(24, cw - 2)
                    return
            w = int(getattr(log.size, "width", 0) or 0)
            if w >= 24:
                self._cached_log_width = max(24, w - 4)
                return
        except Exception:
            pass
        try:
            import os

            self._cached_log_width = max(40, os.get_terminal_size().columns - 12)
        except OSError:
            pass

    def _log_content_width(self) -> int:
        """Usable columns inside #log (thread-safe via cached value)."""
        w = int(getattr(self, "_cached_log_width", 0) or 0)
        return w if w >= 24 else 120

    # ------------------------------------------------------------------ #
    # Logging (thread-safe via queue → UI timer)
    # ------------------------------------------------------------------ #
    def _sink_from_worker(self, kind: str, text: str) -> None:
        try:
            self._log_queue.put_nowait((kind, text))
        except Exception:
            pass

    def post_log(self, kind: str, text: str) -> None:
        """Must run on the UI thread (or via _drain_log_queue)."""
        try:
            log = self.query_one("#log", SelectableRichLog)
        except Exception:
            try:
                log = self.query_one("#log", RichLog)
            except Exception:
                return
        # Preserve intentional blank separators (session list gaps).
        if text is None:
            return
        if text == "" or text.strip() == "":
            try:
                log.write("")
            except Exception:
                pass
            return
        t = text.rstrip("\n")
        # Escape user/chunk text so "[chunk 3]" doesn't break Rich markup.
        try:
            from rich.markup import escape

            safe = escape(t)
        except Exception:
            safe = t.replace("[", "\\[")
        try:
            if kind == "rich":
                # Pre-built Rich markup (caller already escaped user text)
                log.write(t)
            elif kind == "success":
                log.write(f"[green][ok][/] {safe}")
            elif kind == "warn":
                log.write(f"[yellow][!][/] {safe}")
            elif kind == "error":
                log.write(f"[bold red][x][/] {safe}")
            elif kind == "dim":
                log.write(f"[dim]{safe}[/]")
            elif kind == "info":
                log.write(f"[cyan][i][/] {safe}")
            elif kind == "list":
                log.write(f"[bold]{safe}[/]")
            else:
                # raw / plain
                log.write(safe)
        except Exception:
            try:
                log.write(t)  # last resort plain
            except Exception:
                pass

    def _drain_log_queue(self) -> None:
        for _ in range(200):
            try:
                kind, text = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.post_log(kind, text)

    # ------------------------------------------------------------------ #
    # Fixed listen header (robot animation + source/target)
    # ------------------------------------------------------------------ #
    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)

    def set_sound_on(self, on: bool) -> None:
        self._sound_on = bool(on)

    def set_mic_muted(self, muted: bool) -> None:
        self._mic_muted = bool(muted)

    def refresh_source_ui(self) -> None:
        """Re-apply footer/placeholder for current SOURCE_LANG (after [g] swap)."""
        try:
            self._refresh_cmd_menu()
        except Exception:
            pass
        try:
            self._tick_status()
        except Exception:
            pass

    def clear_log(self) -> None:
        """Clear the scrollable log panel (command [cls]). Must run on UI thread."""
        try:
            log = self.query_one("#log", SelectableRichLog)
            log.clear()
            log.write(
                "[dim]Log limpo — [l] histórico · [lo] source · [lt] target[/]"
            )
        except Exception:
            try:
                log = self.query_one("#log", RichLog)
                log.clear()
            except Exception:
                pass

    def _log_widget(self):
        """Return the scrollable log widget, or None."""
        try:
            return self.query_one("#log", SelectableRichLog)
        except Exception:
            try:
                return self.query_one("#log", RichLog)
            except Exception:
                return None

    def scroll_log_top(self) -> None:
        """
        [gt] Go top — jump to start of log.
        Disables auto_scroll so new lines don't yank the viewport back down.
        Must run on UI thread.
        """
        log = self._log_widget()
        if log is None:
            return
        try:
            log.auto_scroll = False
        except Exception:
            pass
        try:
            log.scroll_home(animate=False)
        except Exception:
            try:
                log.scroll_to(0, 0, animate=False)
            except Exception:
                pass

    def scroll_log_footer(self) -> None:
        """
        [gf] Go footer — jump to end of log.
        Re-enables auto_scroll for live follow.
        Must run on UI thread.
        """
        log = self._log_widget()
        if log is None:
            return
        try:
            log.auto_scroll = True
        except Exception:
            pass
        try:
            log.scroll_end(animate=False)
        except Exception:
            try:
                y = int(getattr(log, "max_scroll_y", 0) or 0)
                log.scroll_to(0, y, animate=False)
            except Exception:
                pass

    def _refresh_cmd_menu(self) -> None:
        """
        Footer menu with fixed columns; long groups wrap to continuation rows
        (label only on first row of each group) so columns stay aligned.

        Labels follow SOURCE_LANG (startup + after [g] swap).
        """
        try:
            hint = self.query_one("#hint", Static)
        except Exception:
            return
        t = _footer_i18n()
        try:
            sound = t["on"] if self.pipeline.is_sound_enabled() else t["off"]
        except Exception:
            sound = t["off"]
        try:
            mic = t["muted"] if self.pipeline.is_mic_muted() else t["live"]
        except Exception:
            mic = t["live"]

        # Available width for the hint strip
        try:
            avail = int(getattr(hint.size, "width", 0) or 0)
        except Exception:
            avail = 0
        if avail < 48:
            avail = int(getattr(self, "_cached_log_width", 0) or 0) or 100
        avail = max(48, avail - 2)  # CSS padding 0 1

        lw = 8   # group label column
        cw = 14  # each command cell (fits "[cls] Limpar", "[rN] Replay N")
        cols = max(4, min(8, (avail - lw) // cw))

        def lab_plain(s: str) -> str:
            return (s or "")[:lw].ljust(lw)

        def cell(s: str, w: int = cw) -> str:
            plain = (s or "")
            if len(plain) > w:
                plain = plain[: w - 1] + "…"
            plain = plain.ljust(w)
            return plain.replace("[", "\\[")

        def group_rows(label: str, items: list[str]) -> list[str]:
            """First row has magenta label; overflow rows indent with spaces."""
            rows_out: list[str] = []
            if not items:
                return [f"[bold magenta]{lab_plain(label)}[/]"]
            for i in range(0, len(items), cols):
                chunk = items[i : i + cols]
                cells = "".join(cell(it) for it in chunk)
                if i == 0:
                    rows_out.append(
                        f"[bold magenta]{lab_plain(label)}[/]{cells}"
                    )
                else:
                    rows_out.append(lab_plain("") + cells)
            return rows_out

        lines: list[str] = []
        lines.extend(
            group_rows(
                t["sentence"],
                [
                    f"[e] {t['edit']}",
                    f"[eN] {t['edit_n']}",
                    f"[d] {t['del']}",
                    f"[dN] {t['del_n']}",
                    f"[f] {t['fav']}",
                    f"[F] {t['favs']}",
                    f"[l] {t['list']}",
                    f"[lo] {t['list_src']}",
                    f"[lt] {t['list_tgt']}",
                    f"[co] {t.get('comment', 'Comment')}",
                    f"[coN] {t.get('comment_n', 'Comm N')}",
                    f"[codN] Del #N",
                    f"[cls] {t['cls']}",
                    f"[gt] {t.get('go_top', 'Go top')}",
                    f"[gf] {t.get('go_footer', 'Go foot')}",
                    f"[c] {t['export']}",
                ],
            )
        )
        lines.extend(
            group_rows(
                t["audio"],
                [
                    f"[r] {t['replay']}",
                    f"[rN] {t['replay_n']}",
                    f"[s] {t['snd']} {sound}",
                    f"[n] {t['mic']} {mic}",
                    f"[x] {t['stop']}",
                    f"[a] {t['path']}",
                    f"[aN] {t['path_n']}",
                    f"[p] {t['folder']}",
                ],
            )
        )
        lines.extend(
            group_rows(
                t["idiom"],
                [
                    f"[g] {t['swap']}",
                    f"[t] {t['target']}",
                    f"[o] {t['synonyms']}",
                    f"[v] {t['session']}",
                    f"[m] {t['menu']}",
                    f"[q] {t['quit']}",
                ],
            )
        )
        # No trailing blank line here — #cmd-row sits right under last menu row.
        max_lines = 5
        if len(lines) > max_lines:
            lines = lines[:max_lines]

        hint.update("\n".join(lines))

        # Command field placeholder follows SOURCE_LANG (unless waiting a prompt)
        if not self._prompt_waiting.is_set():
            self._set_placeholder(t["placeholder"])

    def _tick_status(self) -> None:
        """Refresh fixed top header: robot/mic + pair + audio + listen status."""
        try:
            header = self.query_one("#listen-header", Static)
        except Exception:
            return

        # Live flags from pipeline
        try:
            self._sound_on = bool(self.pipeline.is_sound_enabled())
        except Exception:
            pass
        try:
            self._mic_muted = bool(self.pipeline.is_mic_muted())
        except Exception:
            pass

        _src_u, _tgt_u, src_n, tgt_n, pair_short, pair_long = _lang_pair_parts()
        # g/t flanking the pair — PT shown as BR (display only); labels i18n
        ft = _footer_i18n()
        src_d = _display_lang_code(_src_u)
        tgt_d = _display_lang_code(_tgt_u)
        g_lab, t_lab = ft["g_swap"], ft["t_target"]
        lang_block_short = f"{g_lab} {src_d} → {tgt_d} {t_lab}"
        lang_block_long = (
            f"{g_lab} {src_d} ({src_n}) → {tgt_d} ({tgt_n}) {t_lab}"
        )
        # Keep Textual window subtitle in sync with current pair ([g]/[t])
        try:
            self.sub_title = (
                f"{lang_block_short}  ·  ouvir {src_n} → falar {tgt_n}"
            )
        except Exception:
            pass

        header.set_class(self._sound_on and not self._mic_muted, "sound-on")
        header.set_class(self._mic_muted, "mic-muted")

        if self._mic_muted:
            header.update(
                f"🔇  MIC MUTED   {lang_block_short}   |  escuta pausada  |  [n] reativar"
            )
            return

        # Advance animation frame (classic robot idle / mic active)
        frames = _ACTIVE_FRAMES if self._speaking else _IDLE_FRAMES
        self._frame_i = (self._frame_i + 1) % len(frames)
        frame = frames[self._frame_i]

        try:
            idle_msg, active_msg = self._listen_msgs_fn()
        except Exception:
            idle_msg, active_msg = "Waiting...", "Listening..."
        body = active_msg if self._speaking else idle_msg

        if self._sound_on:
            audio_tag = "🔊 ÁUDIO ON"
        else:
            audio_tag = "🔇 ÁUDIO OFF → [s]"

        # robot + g(swap) LANG → LANG t(target) + audio + status
        try:
            width = int(getattr(header.size, "width", 0) or 0)
        except Exception:
            width = 0
        lang_block = lang_block_long if width >= 100 else lang_block_short
        line = f"{frame}  {lang_block}   {audio_tag}   {body}"
        if width >= 24 and len(line) > width:
            line = line[: max(0, width - 1)] + "…"
        header.update(line)

    # ------------------------------------------------------------------ #
    # Prompt / command input
    # ------------------------------------------------------------------ #
    def _wait_for_prompt_line(self) -> str:
        """Called from worker thread when code does sys.stdin.readline()."""
        self._prompt_waiting.set()
        try:
            self.call_from_thread(
                self._set_placeholder,
                self._prompt_label or _footer_i18n()["prompt_placeholder"],
            )
        except Exception:
            pass
        try:
            line = self._prompt_q.get()
        finally:
            self._prompt_waiting.clear()
            try:
                self.call_from_thread(
                    self._set_placeholder,
                    _footer_i18n()["placeholder"],
                )
            except Exception:
                pass
        return line if line.endswith("\n") else line + "\n"

    def _set_placeholder(self, text: str) -> None:
        try:
            self.query_one("#cmd", Input).placeholder = text
        except Exception:
            pass

    def provide_prompt_line(self, line: str) -> None:
        self._prompt_q.put(line)

    def set_prompt_force_upper(self, on: bool) -> None:
        """UI-thread: force language-code entry to UPPERCASE ([t] only)."""
        self._prompt_force_upper = bool(on)
        if not on:
            return
        # Normalize anything already typed in the field
        try:
            inp = self.query_one("#cmd", Input)
            val = inp.value or ""
            up = val.upper()
            if up != val:
                inp.value = up
        except Exception:
            pass

    def _cmd_input(self) -> Input | None:
        try:
            return self.query_one("#cmd", Input)
        except Exception:
            return None

    def _focus_cmd(self) -> Input | None:
        """Focus the command field (classic: type anywhere)."""
        inp = self._cmd_input()
        if inp is None:
            return None
        try:
            if self.focused is not inp:
                inp.focus()
        except Exception:
            pass
        return inp

    def _is_cmd_focused(self) -> bool:
        try:
            focused = self.focused
            return isinstance(focused, Input) and getattr(focused, "id", None) == "cmd"
        except Exception:
            return False

    def on_key(self, event: events.Key) -> None:
        """
        Classic-style command entry: type from any panel (log/header/menu).

        Multi-char commands (r22, e3, aN…) still need the full sequence + Enter —
        same as classic stdin.readline. We only route keystrokes into #cmd when
        focus is elsewhere (e.g. after clicking the log to select/copy).

        With #cmd focused: ↑/↓ walk command history (like bash / Grok).
        """
        # Hard-catch full-log copy (key name is e.g. "ctrl+shift+c").
        key_name = (event.name or "").lower()
        key_raw = (getattr(event, "key", None) or "").lower()
        if key_name in ("ctrl+shift+c", "shift+ctrl+c") or key_raw in (
            "ctrl+shift+c",
            "shift+ctrl+c",
        ):
            event.prevent_default()
            event.stop()
            self.action_copy_log()
            return

        # History navigation when the command field is focused
        if self._is_cmd_focused() and key_name in (
            "up",
            "down",
            "cursor_up",
            "cursor_down",
        ):
            event.prevent_default()
            event.stop()
            if key_name in ("up", "cursor_up"):
                self._history_up()
            else:
                self._history_down()
            return

        # Let other bindings (Ctrl+C selection, Ctrl+Q, F1…) handle non-printables.
        if event.character is None and key_name not in (
            "enter",
            "return",
            "backspace",
            "delete",
        ):
            return
        if self._is_cmd_focused():
            return  # Input handles typing / submit normally

        key = key_name
        ch = event.character

        # Printable → append to command field (incl. digits for r22 / eN)
        if ch and ch.isprintable() and ch not in ("\r", "\n", "\t"):
            inp = self._focus_cmd()
            if inp is None:
                return
            # [t] target-lang prompt: force UPPERCASE keystrokes only
            if self._prompt_force_upper and ch.isalpha():
                ch = ch.upper()
            # Append at cursor end
            try:
                val = inp.value or ""
                pos = int(getattr(inp, "cursor_position", len(val)) or len(val))
                pos = max(0, min(pos, len(val)))
                inp.value = val[:pos] + ch + val[pos:]
                inp.cursor_position = pos + 1
            except Exception:
                try:
                    inp.value = (inp.value or "") + ch
                except Exception:
                    pass
            event.prevent_default()
            event.stop()
            return

        if key in ("enter", "return"):
            inp = self._focus_cmd()
            if inp is None:
                return
            value = (inp.value or "").strip()
            inp.value = ""
            event.prevent_default()
            event.stop()
            self._submit_command_line(value)
            return

        if key in ("backspace", "delete"):
            inp = self._focus_cmd()
            if inp is None:
                return
            try:
                val = inp.value or ""
                if key == "backspace" and val:
                    pos = int(getattr(inp, "cursor_position", len(val)) or len(val))
                    if pos > 0:
                        inp.value = val[: pos - 1] + val[pos:]
                        inp.cursor_position = pos - 1
                elif key == "delete" and val:
                    pos = int(getattr(inp, "cursor_position", 0) or 0)
                    if pos < len(val):
                        inp.value = val[:pos] + val[pos + 1 :]
            except Exception:
                try:
                    inp.value = (inp.value or "")[:-1]
                except Exception:
                    pass
            event.prevent_default()
            event.stop()

    def _submit_command_line(self, value: str) -> None:
        """Shared submit path for Input.Submitted and global Enter."""
        value = (value or "").strip()
        if self._prompt_waiting.is_set():
            # Command [t]: language codes always UPPERCASE
            if self._prompt_force_upper:
                value = value.upper()
            self.provide_prompt_line(value)
            return
        if not value:
            return
        if self._cmd_busy:
            self.post_log("warn", "Aguarde o comando anterior terminar…")
            return
        self._push_cmd_history(value)
        self.post_log("dim", f"> {value}")
        self.run_command(value)

    @on(Input.Changed, "#cmd")
    def on_cmd_changed(self, event: Input.Changed) -> None:
        """Force UPPERCASE while waiting for [t] target-lang prompt only."""
        if not self._prompt_force_upper:
            return
        val = event.value or ""
        up = val.upper()
        if up != val:
            # Preserve cursor near end after case fold
            try:
                pos = int(getattr(event.input, "cursor_position", len(up)) or len(up))
            except Exception:
                pos = len(up)
            event.input.value = up
            try:
                event.input.cursor_position = min(pos, len(up))
            except Exception:
                pass

    @on(Input.Submitted, "#cmd")
    def on_command(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        if self._prompt_force_upper:
            value = value.upper()
        event.input.value = ""
        self._submit_command_line(value)

    @work(thread=True)
    def run_command(self, raw: str) -> None:
        self._cmd_busy = True
        try:
            cmd = raw.lower().strip()
            if cmd in ("q", "quit"):
                self._log_queue.put(("info", "Encerrando…"))
                try:
                    self.pipeline.stop()
                except Exception:
                    pass
                self.call_from_thread(self.exit)
                return

            old_out, old_in = sys.stdout, sys.stdin
            sys.stdout = _StdoutProxy(self, old_out)
            sys.stdin = _StdinProxy(self)
            try:
                self._dispatch(
                    self.pipeline,
                    self.synonym_lookup,
                    raw,
                    cmd,
                    self,
                )
            except Exception as exc:
                tb = traceback.format_exc(limit=4)
                self._log_queue.put(("error", f"Command error: {exc}"))
                self._log_queue.put(("dim", tb))
            finally:
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                sys.stdout = old_out
                sys.stdin = old_in
                try:
                    self._sound_on = bool(self.pipeline.is_sound_enabled())
                    self._mic_muted = bool(self.pipeline.is_mic_muted())
                except Exception:
                    pass

            if getattr(self.pipeline, "switch_session", False) or (
                self.pipeline.stop_event.is_set()
                and cmd in ("v", "q", "quit")
            ):
                self.call_from_thread(self.exit)
                return
        finally:
            self._cmd_busy = False
            try:
                self.call_from_thread(self.query_one("#cmd", Input).focus)
            except Exception:
                pass

    def pause_for_command(self) -> None:
        pass

    def resume_after_command(self) -> None:
        pass

    def is_mic_muted_ui(self) -> bool:
        return self._mic_muted

    def action_livelingo_screenshot(self) -> None:
        """
        Palette Screenshot: save SVG, rasterize to PNG, copy image to clipboard.
        """
        out_dir = os.path.join(".cache", "screenshots")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            out_dir = "."
        try:
            svg_path = self.save_screenshot(path=out_dir)
        except Exception as exc:
            self.post_log("error", f"Screenshot falhou: {exc}")
            return
        svg_abs = os.path.abspath(svg_path)
        share_svg = svg_abs
        try:
            from . import ui as _ui

            share_svg = _ui.resolve_share_path(svg_abs) or svg_abs
        except Exception:
            pass

        png_path = os.path.splitext(svg_abs)[0] + ".png"
        img_ok = False
        clip_ok = False
        if _svg_to_png(svg_abs, png_path):
            img_ok = True
            clip_ok = _clipboard_set_image(png_path)
            # Fallback: also leave PNG path if clipboard image failed
            if not clip_ok:
                try:
                    share_png = png_path
                    try:
                        from . import ui as _ui

                        share_png = _ui.resolve_share_path(png_path) or png_path
                    except Exception:
                        pass
                    self._clipboard_set(share_png)
                except Exception:
                    pass
        else:
            # No rasterizer — still copy SVG path so user can open the file
            self._clipboard_set(share_svg)

        self.post_log("success", f"Screenshot SVG: {share_svg}")
        if img_ok:
            share_png = png_path
            try:
                from . import ui as _ui

                share_png = _ui.resolve_share_path(png_path) or png_path
            except Exception:
                pass
            self.post_log("info", f"Screenshot PNG: {share_png}")
            if clip_ok:
                self.post_log(
                    "success",
                    "Imagem copiada para a area de transferencia "
                    "(Ctrl+V em apps que aceitam imagem).",
                )
                try:
                    self.notify(
                        "Screenshot na area de transferencia",
                        severity="information",
                        timeout=3,
                    )
                except Exception:
                    pass
            else:
                self.post_log(
                    "warn",
                    "PNG gerado, mas falhou copiar imagem ao clipboard "
                    "(path copiado como texto se possivel).",
                )
        else:
            self.post_log(
                "warn",
                "Nao foi possivel rasterizar SVG→PNG "
                "(instale Edge/Chrome ou cairosvg). Path do SVG no clipboard.",
            )

    def action_show_help(self) -> None:
        """F1: banner + startup status (devices, engines, tips) + command summary."""
        if self._help_fn is not None:
            try:
                self._help_fn()
                return
            except Exception as exc:
                self.post_log("error", f"Help error: {exc}")
        # Fallback if no help_fn wired
        self.post_log(
            "info",
            "Sentence: e/eN d/dN f/fN F l lo lt cls gt gf c | "
            "Audio: r/rN s n x a/aN p/pN | "
            "Idiom: g t o | Session: v m q",
        )
        self.post_log(
            "info",
            "Copiar: clique+arraste → Ctrl+C | log inteiro Ctrl+Shift+C / F2 | "
            "sair Ctrl+Q | F1=ajuda",
        )

    def _clipboard_set(self, text: str) -> bool:
        """Copy text via Textual OSC-52 + OS clipboard fallback."""
        text = text or ""
        if not text:
            return False
        ok = False
        try:
            self.copy_to_clipboard(text)
            ok = True
        except Exception:
            pass
        if _os_clipboard(text):
            ok = True
        return ok

    def action_copy_selection(self) -> None:
        """Ctrl+C: copy selection if any; otherwise copy the entire log."""
        selected = None
        try:
            selected = self.screen.get_selected_text()
        except Exception:
            selected = None
        if selected and selected.strip():
            if self._clipboard_set(selected):
                n = len(selected)
                try:
                    self.notify(
                        f"Selecao copiada ({n} chars)",
                        severity="information",
                        timeout=2,
                    )
                except Exception:
                    self.post_log(
                        "success",
                        f"Selecao copiada ({n} chars)",
                    )
            else:
                try:
                    self.notify("Falha ao copiar", severity="error", timeout=3)
                except Exception:
                    self.post_log("error", "Falha ao copiar")
            return
        # No selection → full log (no "please select" nag)
        self.action_copy_log()

    def action_copy_log(self) -> None:
        """
        Ctrl+Shift+C / F2: copy entire scrollback log — no mouse selection needed.
        Pulls plain text from SelectableRichLog buffer and writes the clipboard.
        """
        text = ""
        try:
            log = self.query_one("#log", SelectableRichLog)
            text = log.get_plain_text() or ""
        except Exception:
            text = ""
        if not (text or "").strip():
            # Fallback: rendered strips (in case plain buffer is empty)
            try:
                log = self.query_one("#log", SelectableRichLog)
                text = "\n".join(line.text for line in (log.lines or []))
            except Exception:
                text = ""
        if not (text or "").strip():
            try:
                self.notify("Log vazio — nada para copiar", severity="warning", timeout=2)
            except Exception:
                self.post_log("warn", "Log vazio — nada para copiar")
            return
        if self._clipboard_set(text):
            n = len(text)
            lines = text.count("\n") + 1
            msg = f"Log inteiro copiado ({lines} linhas, {n} chars)"
            try:
                self.notify(msg, severity="information", timeout=3)
            except Exception:
                self.post_log("success", msg)
            # Also echo in log so user sees confirmation even if toast is missed
            self.post_log("success", msg)
        else:
            try:
                self.notify("Falha ao copiar log", severity="error", timeout=3)
            except Exception:
                self.post_log("error", "Falha ao copiar log")

    def action_quit_app(self) -> None:
        try:
            self.pipeline.stop()
        except Exception:
            pass
        self.exit()


def run_tui(
    pipeline,
    synonym_lookup,
    dispatch_command,
    listen_msgs_fn,
    help_fn=None,
) -> None:
    """Block until the TUI exits."""
    app = LiveLingoApp(
        pipeline=pipeline,
        synonym_lookup=synonym_lookup,
        dispatch_command=dispatch_command,
        listen_msgs_fn=listen_msgs_fn,
        help_fn=help_fn,
    )
    app.run()
