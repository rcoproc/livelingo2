"""
tui_app.py
==========
Textual TUI for LiveLingo: fixed listen header (robot + source/target) +
four scrollable log tabs (Tradução / Sistema / Novidades / Command list)
+ command input.

Tradução is a vertical split: left = LiveCaptions (LC) only, right = VOZ
chunks + command output. The sash is mouse-draggable; each side can expand
or restore. Scroll is independent per pane.

Pipeline (mic/STT/TTS) keeps running in background threads; this module only
owns the screen. Logs arrive via ui.set_log_sink(kind, text, panel); commands
reuse main dispatch in a worker thread with stdin/stdout proxies.

The Novidades ("What's New") tab shows CHANGELOG.md; the Command list tab
lists all menu commands (Markdown, i18n by SOURCE_LANG).
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
from typing import Callable, Iterable

from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

# Click events on Static bypass badge (#cmd-bypass)
from . import command_help
from . import ui as ui_mod


# --------------------------------------------------------------------------- #
# Mic mute modal — red / white, centered; only [n] unmutes (TUI stays behind)
# --------------------------------------------------------------------------- #
class MicMutedModal(ModalScreen[str]):
    """
    Full-screen dim overlay with a centered red dialog while the mic is muted.

    Background TUI remains visible (dimmed). Sole action: press **n** to unmute.
    """

    CSS = """
    MicMutedModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.55);
    }
    #mic-mute-box {
        width: 56;
        max-width: 92%;
        height: auto;
        background: #c62828;
        color: #ffffff;
        border: tall #ffffff;
        padding: 1 2;
        layout: vertical;
    }
    #mic-mute-box Static {
        width: 100%;
        color: #ffffff;
        background: transparent;
        text-align: center;
    }
    #mic-mute-title {
        text-style: bold;
        text-align: center;
        color: #ffffff;
        padding-bottom: 1;
    }
    #mic-mute-name {
        text-align: center;
        color: #ffebee;
        text-style: italic;
    }
    #mic-mute-msg, #mic-mute-msg2 {
        text-align: center;
        color: #ffffff;
        padding-top: 0;
    }
    #mic-mute-msg {
        padding-top: 1;
    }
    #mic-mute-hint {
        text-align: center;
        text-style: bold;
        color: #ffffff;
        background: #8e0000;
        padding: 1 1;
        margin-top: 1;
        border: solid #ffffff;
    }
    """

    BINDINGS = [
        Binding("n", "unmute", "n desmutar", show=True, priority=True),
        Binding("N", "unmute", "n desmutar", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        title: str = "MIC MUTED",
        mic_name: str = "",
        message: str = "",
        message2: str = "",
        hint: str = "[n]  desmutar",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._title = title or "MIC MUTED"
        self._mic_name = (mic_name or "").strip()
        self._message = (message or "").strip()
        self._message2 = (message2 or "").strip()
        self._hint = hint or "[n]  desmutar"

    def compose(self) -> ComposeResult:
        with Vertical(id="mic-mute-box"):
            yield Static(f"🔇  {self._title}", id="mic-mute-title")
            if self._mic_name:
                yield Static(self._mic_name, id="mic-mute-name")
            if self._message:
                yield Static(self._message, id="mic-mute-msg")
            if self._message2:
                yield Static(self._message2, id="mic-mute-msg2")
            yield Static(self._hint, id="mic-mute-hint")

    def action_unmute(self) -> None:
        """Only way out: dismiss with 'n' so the app unmutes the mic."""
        self.dismiss("n")

    def on_key(self, event: events.Key) -> None:
        # Accept plain n / N even when focus is odd (some terminals)
        key = (event.key or event.name or "").lower()
        ch = (event.character or "").lower()
        if key in ("n",) or ch == "n":
            event.prevent_default()
            event.stop()
            self.action_unmute()


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
    out.append(
        os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    )
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
        if (
            ps_bin.startswith("/") or (len(ps_bin) > 2 and ps_bin[1] == ":")
        ) and not os.path.isfile(ps_bin):
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


def _terminal_log_width(fallback: int = 100) -> int:
    """
    Conservative usable columns for log wrap when a pane has no layout size.

    Subtract enough for TUI chrome (header, tabs, borders, scrollbar, padding)
    so baked lines never exceed the visible panel (avoids === rule wrap).
    """
    try:
        cols = int(os.get_terminal_size().columns)
        if cols >= 40:
            # tabs bar + borders + pad + scrollbar ≈ 12–16 on typical layouts
            return max(60, cols - 16)
    except OSError:
        pass
    return max(60, int(fallback or 100))


def _host_window_hwnd():
    """
    HWND of the visible host window (conhost or Windows Terminal frame).

    Never touch console buffer size here — only used for pixel MoveWindow.
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            GA_ROOT = 2
            try:
                root = user32.GetAncestor(hwnd, GA_ROOT)
                if root:
                    hwnd = root
            except Exception:
                pass
            # Prefer a visible, sizable window
            if user32.IsWindowVisible(hwnd):
                return int(hwnd)

        # Windows Terminal / ConPTY often has no GetConsoleWindow — find by title
        # or use the foreground window (user just pressed F4 here).
        candidates: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd_e, _lparam):
            if not user32.IsWindowVisible(hwnd_e):
                return True
            length = user32.GetWindowTextLengthW(hwnd_e)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd_e, buf, length + 1)
            title = (buf.value or "").lower()
            # LiveLingo title or Windows Terminal host
            if (
                "livelingo" in title
                or "windows terminal" in title
                or title.endswith("powershell")
                or "cmd.exe" in title
            ):
                candidates.append(int(hwnd_e))
            return True

        try:
            user32.EnumWindows(_enum, 0)
        except Exception:
            pass
        if candidates:
            return candidates[0]

        fg = user32.GetForegroundWindow()
        return int(fg) if fg else 0
    except Exception:
        return 0


def _snapshot_window_geom() -> dict | None:
    """Capture terminal char size + host window pixel rect for later restore."""
    snap: dict = {}
    try:
        ts = os.get_terminal_size()
        snap["cols"] = int(ts.columns)
        snap["rows"] = int(ts.lines)
    except OSError:
        snap["cols"] = 120
        snap["rows"] = 40
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = _host_window_hwnd()
            if hwnd:
                rect = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    snap["hwnd"] = hwnd
                    snap["left"] = int(rect.left)
                    snap["top"] = int(rect.top)
                    snap["width"] = int(rect.right - rect.left)
                    snap["height"] = int(rect.bottom - rect.top)
        except Exception:
            pass
    return snap


def _safe_resize_host_window(
    cols: int,
    rows: int,
    *,
    restore: dict | None = None,
) -> bool:
    """
    Resize the host window height without touching the console screen buffer.

    Previous SetConsoleWindowInfo(1x1)+SetConsoleScreenBufferSize corrupted
    Textual (ghost UI, dead tabs). Safe path:
      1) CSI 8 (Windows Terminal / xterm)
      2) MoveWindow on host HWND (pixel height only)
    """
    cols = max(40, int(cols))
    rows = max(14, int(rows))
    ok = False

    # 1) VT window resize — WT applies this and fires a normal resize event
    try:
        sys.stdout.write(f"\x1b[8;{rows};{cols}t")
        sys.stdout.flush()
        ok = True
    except Exception:
        pass

    # 2) Pixel MoveWindow (works when CSI is ignored; no buffer API)
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            if restore and restore.get("hwnd"):
                hwnd = int(restore["hwnd"])
                left = int(restore["left"])
                top = int(restore["top"])
                width = int(restore["width"])
                height = int(restore["height"])
                if user32.MoveWindow(hwnd, left, top, width, height, True):
                    ok = True
            else:
                hwnd = _host_window_hwnd()
                if hwnd:
                    rect = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        cur_w = max(1, rect.right - rect.left)
                        cur_h = max(1, rect.bottom - rect.top)
                        try:
                            cur_rows = max(1, os.get_terminal_size().lines)
                        except OSError:
                            cur_rows = max(1, rows)
                        # Scale pixel height by row ratio (keep width)
                        cell_h = max(8, cur_h // cur_rows)
                        new_h = max(200, cell_h * rows + 48)
                        if user32.MoveWindow(
                            hwnd, rect.left, rect.top, cur_w, new_h, True
                        ):
                            ok = True
        except Exception:
            pass
    return ok


class TradSash(Static):
    """
    Vertical drag handle between LC (left) and VOZ (right) log columns.

    Mouse-down + drag updates LiveLingoApp._trad_ratio; double-click restores
    50/50. Cursor style hints resize.
    """

    DEFAULT_CSS = """
    TradSash {
        width: 1;
        min-width: 1;
        max-width: 1;
        height: 1fr;
        background: $accent;
        color: $text;
        content-align: center middle;
        dock: none;
    }
    TradSash:hover {
        background: $accent-lighten-2;
    }
    TradSash.-dragging {
        background: $warning;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("║", **kwargs)
        self._dragging = False
        self._start_x = 0
        self._start_ratio = 0.5

    def on_click(self, event: events.Click) -> None:
        """Double-click sash → restore 50/50 split."""
        try:
            if int(getattr(event, "chain", 1) or 1) >= 2:
                event.stop()
                if hasattr(self.app, "trad_restore_split"):
                    self.app.trad_restore_split(ratio=0.5)
        except Exception:
            pass

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self._dragging = True
        self.add_class("-dragging")
        try:
            self._start_x = int(event.screen_x)
        except Exception:
            self._start_x = int(getattr(event, "x", 0) or 0)
        try:
            self._start_ratio = float(getattr(self.app, "_trad_ratio", 0.5) or 0.5)
        except Exception:
            self._start_ratio = 0.5
        try:
            self.capture_mouse()
        except Exception:
            pass

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        self.remove_class("-dragging")
        try:
            self.release_mouse()
        except Exception:
            pass
        try:
            if hasattr(self.app, "_refresh_log_width"):
                self.app._refresh_log_width()
        except Exception:
            pass

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        event.stop()
        app = self.app
        if not hasattr(app, "trad_set_ratio_from_drag"):
            return
        try:
            x = int(event.screen_x)
        except Exception:
            x = int(getattr(event, "x", 0) or 0)
        try:
            app.trad_set_ratio_from_drag(self._start_x, self._start_ratio, x)
        except Exception:
            pass


class CaptionsHSash(Static):
    """
    Horizontal drag handle on the **bottom edge** of the Live Captions strip.

    Divides upper LC captions vs middle log tabs. Drag ↕ to resize;
    double-click restores default height (8 rows).
    """

    DEFAULT_CSS = """
    CaptionsHSash {
        width: 1fr;
        height: 1;
        min-height: 1;
        max-height: 1;
        background: #7aa2f7 45%;
        color: #c0caf5;
        content-align: center middle;
        text-style: bold;
    }
    CaptionsHSash:hover {
        background: #7aa2f7 80%;
        color: #ffffff;
    }
    CaptionsHSash.-dragging {
        background: $warning;
        color: #1a1b26;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("═ ↕ captions ═", **kwargs)
        self._dragging = False

    def on_click(self, event: events.Click) -> None:
        try:
            if int(getattr(event, "chain", 1) or 1) >= 2:
                event.stop()
                if hasattr(self.app, "captions_set_height"):
                    self.app.captions_set_height(8)
        except Exception:
            pass

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self._dragging = True
        self.add_class("-dragging")
        try:
            self.capture_mouse()
        except Exception:
            pass
        try:
            if hasattr(self.app, "captions_set_height_from_screen_y"):
                self.app.captions_set_height_from_screen_y(int(event.screen_y))
        except Exception:
            pass

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        self.remove_class("-dragging")
        try:
            self.release_mouse()
        except Exception:
            pass
        try:
            if hasattr(self.app, "_refresh_log_width"):
                self.app._refresh_log_width()
        except Exception:
            pass

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        event.stop()
        try:
            if hasattr(self.app, "captions_set_height_from_screen_y"):
                self.app.captions_set_height_from_screen_y(int(event.screen_y))
        except Exception:
            pass


class SelectableRichLog(RichLog):
    """
    RichLog with character-level mouse selection + plain-text export.

    Upstream RichLog:
      1) does not implement get_selection()
      2) does not call Strip.apply_offsets() — so the compositor never
         gets content (x,y) under the mouse → Textual falls back to
         SELECT_ALL (entire log blue).
      3) with wrap=True + shrink, lines are baked at the *current* region
         width. Inactive TabPane often has ~0 width → permanent 20-col wrap.

    We mirror the built-in Log widget: apply_offsets + get_selection + highlight,
    and force a sane render width when the pane is hidden / not laid out.

    Optional ``pane_role``: \"lc\" | \"voz\" so click sets Tradução sub-focus.
    """

    def __init__(self, **kwargs):
        # Default min_width is 78 upstream; we used 20 and that became the
        # baked wrap width for inactive tabs. Keep a wide floor.
        kwargs.setdefault("min_width", 100)
        self.pane_role: str | None = kwargs.pop("pane_role", None)
        super().__init__(**kwargs)
        self._plain_lines: list[str] = []
        # Last measured content width while the pane was visible (≥40).
        self._last_good_width = 0
        # Vim-style / search highlight (applied in _render_line)
        self._search_query: str = ""
        self._search_hit_ys: set[int] = set()
        self._search_current_y: int | None = None

    def on_click(self, event: events.Click) -> None:
        """Mark this Tradução sub-pane as focused for search/gg/copy."""
        role = getattr(self, "pane_role", None)
        if role in ("lc", "voz"):
            try:
                if hasattr(self.app, "set_trad_focus"):
                    self.app.set_trad_focus(role)
            except Exception:
                pass

    def _safe_render_width(self) -> int:
        """
        Width to bake wrapped lines at.

        Prefer the live content region when the pane is visible and laid out.
        When the tab is hidden (ContentSwitcher → width ~0), reuse the last
        good width or a conservative terminal floor — never a tiny min_width
        that column-wraps forever, and never wider than the real panel.
        """
        try:
            region_w = int(self.scrollable_content_region.width or 0)
        except Exception:
            region_w = 0
        if region_w >= 40:
            # CSS padding 0 1 + scrollbar fudge — stay inside the visible area
            w = max(40, region_w - 2)
            self._last_good_width = w
            return w

        if int(getattr(self, "_last_good_width", 0) or 0) >= 40:
            return int(self._last_good_width)

        try:
            app_w = int(getattr(self.app.size, "width", 0) or 0)
            if app_w >= 40:
                return max(60, app_w - 16)
        except Exception:
            pass
        return max(60, _terminal_log_width(100))

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

        # Always bake wrap at a sane width. Default shrink+tiny inactive pane
        # width permanently column-wraps lines (telas3/telas4). Explicit width
        # bypasses shrink/expand and ignores a bad region size.
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
        self._plain_lines.clear()
        self.clear_search_highlight(refresh=False)
        try:
            return super().clear()
        except Exception:
            return None

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
        """
        Return content Y indices (rendered strips preferred) matching query.

        Case-insensitive substring search. Used by vim-style `/` log search.
        """
        q = (query or "").casefold()
        if not q:
            return []
        hits: list[int] = []
        # Prefer baked/wrapped strips — Y matches scroll content_y.
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
        # Fallback: logical plain lines (may not match wrap Y exactly)
        for y, line in enumerate(self._plain_lines):
            if q in (line or "").casefold():
                hits.append(y)
        return hits

    def scroll_to_content_y(self, y: int) -> None:
        """Scroll so content row `y` is visible (prefer upper third). UI thread."""
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
        try:
            self.refresh(layout=True)
        except Exception:
            try:
                self.refresh()
            except Exception:
                pass

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

    def _search_spans_on_line(self, text: str) -> list[tuple[int, int]]:
        """Case-insensitive match spans of the current search query on one line."""
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
            # Fallback: simple casefold scan (ASCII-safe)
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

    def _render_line(self, y: int, scroll_x: int, width: int):
        """Render content line y; apply selection + /search highlights."""
        from rich.cells import cell_len
        from rich.style import Style
        from rich.text import Text
        from textual.strip import Strip as TStrip

        if y >= len(self.lines):
            return TStrip.blank(width, self.rich_style)

        selection = self.text_selection
        has_sel = selection is not None and not (
            selection.start is None and selection.end is None
        )
        has_search = bool(
            (self._search_query or "").strip() and y in (self._search_hit_ys or set())
        )

        if not has_sel and not has_search:
            return super()._render_line(y, scroll_x, width)

        try:
            full = self.lines[y]
            raw = full.text if hasattr(full, "text") else str(full)
            line_text = Text(raw, no_wrap=True)

            # 1) Mouse selection (if any)
            if has_sel:
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
                        sel_style = Style(bgcolor="#f0d78c", color="#1a1b26", bold=True)
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

            # 2) /search hits — other matches yellow; current match bright orange
            if has_search:
                other_style = Style(bgcolor="#f0d78c", color="#1a1b26", bold=True)
                current_style = Style(bgcolor="#ff9500", color="#1a1b26", bold=True)
                is_current = self._search_current_y is not None and int(
                    self._search_current_y
                ) == int(y)
                # Soft whole-line wash so the row is easy to spot while scrolling
                if is_current:
                    line_text.stylize(
                        Style(bgcolor="#3d2e12", color=None), 0, len(line_text)
                    )
                else:
                    line_text.stylize(
                        Style(bgcolor="#2a2818", color=None), 0, len(line_text)
                    )
                for a, b in self._search_spans_on_line(raw):
                    a = max(0, min(a, len(line_text)))
                    b = max(a, min(b, len(line_text)))
                    if b > a:
                        line_text.stylize(
                            current_style if is_current else other_style, a, b
                        )

            strip = TStrip(
                line_text.render(self.app.console),
                cell_len(raw),
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
        "enew": "New text",
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
        "cls": "Clear all",
        "cls1": "Clr LC",
        "cls2": "Clr VOZ",
        "go_top": "Go top",
        "go_footer": "Go foot",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "replay_src": "Replay Heard",
        "replay_src_n": "Heard N",
        "snd": "Snd",
        "mic": "Mic",
        "bypass": "Bypass",
        "stop": "Stop",
        "path": "Path",
        "path_n": "Path N",
        "folder": "Folder",
        "list_dev": "Devices",
        "list_voices": "All voices",
        "list_voices_f": "Voices filter",
        "ctts": "Chg TTS",
        "swap": "Swap",
        "target": "Target",
        "synonyms": "Synonyms",
        "session": "Session",
        "menu": "Menu",
        "compact": "Compact",
        "quit": "Quit",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUTED",
        "placeholder": "Type a command and Enter (e.g. s, g, b, enew, ctts, u, q)…",
        "prompt_placeholder": "Type the answer and Enter…",
        "starting": "starting listen…",
        "g_swap": "g(swap)",
        "t_target": "t(target)",
        "cmd_tts": "TTS",
        "tab_news": "What's New",
        "news_header": "Project changelog (CHANGELOG.md)",
        "news_missing": "CHANGELOG.md not found in the project root.",
        "news_read_error": "Could not read CHANGELOG.md",
        "tab_commands": "Command list",
        "search_hit": 'Search: "{q}" — {i}/{n}',
        "search_none": 'No matches: "{q}"',
        "search_no_active": "No active search. Use /text first.  (/n next · /p prev)",
        "search_help": (
            "Log search: /text  ·  /n next  ·  /p previous  ·  /  repeat  ·  "
            "aliases: find text  ·  find:text  ·  s?text"
        ),
        "search_empty_log": "Active log tab is empty — nothing to search.",
        "bypass_on_label": "F2 Your voice",
        "bypass_off_label": "F2 Transl. audio",
        "bypass_tooltip_on": "BYPASS ON — your raw voice → output (no translation). F2 / click / [b] to turn off.",
        "bypass_tooltip_off": "BYPASS OFF — translated audio path. F2 / click / [b] to send your voice directly.",
        # F5: auto-scroll lock for Tradução LC + VOZ panes
        "scroll_on_label": "F5 Auto↓ ON",
        "scroll_off_label": "F5 Auto↓ OFF",
        "scroll_tooltip_on": "AUTO-SCROLL ON — new LC/VOZ lines jump to bottom. F5 / click to lock scroll (read upper history).",
        "scroll_tooltip_off": "AUTO-SCROLL OFF — LC/VOZ stay put while new lines arrive (chunks/commands). F5 / click to resume follow.",
        "scroll_footer_on": "Auto↓ ON",
        "scroll_footer_off": "Auto↓ OFF",
        "scroll_log_on": "[F5] Auto-scroll ON — Tradução LC+VOZ follow new lines.",
        "scroll_log_off": "[F5] Auto-scroll OFF — Tradução LC+VOZ scroll locked (chunks won't yank view).",
        # Pipeline activity bar (left of command box)
        "pipe_mic": "Mic",
        "pipe_stt": "STT",
        "pipe_tr": "Trad",
        "pipe_tts": "TTS",
        "pipe_out": "Out",
        "pipe_mic_active": "Listening",
        "pipe_stt_active": "STT…",
        "pipe_tr_active": "Trans…",
        "pipe_tts_active": "TTS…",
        "pipe_out_active": "Cable",
        "pipe_lc": "LC",
        "pipe_lc_active": "LC●",
        "pipe_bypass": "BYPASS→Out",
        "pipe_muted": "Mic muted",
        "pipe_tip": "VOICE: Mic → STT → Trad → TTS → Cable Out · LC = LiveCaptions",
        # Tradução split chrome (SOURCE_LANG)
        "tab_traducao": "Translation",
        "tab_sistema": "System",
        "trad_lbl_lc": "LC in (LiveCaptions)",
        "trad_lbl_voz": "VOICE mic + commands",
        "expand": "Expand",
        "restore": "Restore",
        "expand_tip": "Maximize VOZ panel (right)",
        "restore_tip": "Restore LC | VOZ split",
        "cls_note_lc": "[dim]LC cleared — stable [LC n] pairs will show here again[/]",
        "cls_note_voz": "[dim]VOZ cleared — [l] history · [lo]/[lt] · F3 System[/]",
        "cls_note_app": "[dim]System cleared — STT/translate/TTS stages will show here again[/]",
        "cls1_note": "[dim]LC (left) cleared — [LC n] Caption/Translated will show here again[/]",
        "cls2_note": "[dim]VOZ (right) cleared — Heard/Translated and commands will show here again[/]",
        "boot_lc_1": (
            "[bold magenta]LC in[/] — [bold]stable[/] pairs "
            "[bold][LC n] Caption / Translated[/]"
        ),
        "boot_lc_2": (
            "[dim]Top strip = live (partial). Final commits only here. "
            "[lc] pause · show/hide[/]"
        ),
        "boot_lc_3": (
            "[dim]Live Captions strip (top): ═ ↕ resize vs middle logs · "
            "║ LC|VOZ width · click to focus.[/]"
        ),
        "boot_voz_1": (
            "[bold cyan]LiveLingo TUI[/] — [bold]VOZ[/] (mic chunks + command output)"
        ),
        "boot_voz_2": (
            "[dim]Speak into the mic — Heard/Translated (VOZ) appear here. "
            "Commands: type + Enter | ↑↓ = history.[/]"
        ),
        "boot_voz_3": (
            "[yellow]Audio OFF by default — [s] live | "
            "[r]/[rN] chunk | [l] list | [g] swap[/]"
        ),
        "boot_voz_4": (
            "[bold green]Copy:[/] selection [bold]Ctrl+C[/]  ·  "
            "focused log [bold]Ctrl+Shift+C[/]  ·  "
            "bypass [bold]F2[/]  ·  "
            "auto-scroll [bold]F5[/]  ·  "
            "search [bold]/text[/]  ·  F3 tabs"
        ),
        "boot_voz_5": (
            "[dim]Split LC|VOZ: drag ║ · Expand top-right of this pane · "
            "captions ↕ at top · sash 50/50 double-click. Quit: Ctrl+Q / [q].[/]"
        ),
        "boot_app_1": (
            "[bold cyan]System[/] — pipeline stages, VAD listen, timestamps "
            "and timing (keeps Translation tab clean)"
        ),
        "boot_app_2": (
            "[dim]Here: Transcribing / Translating / TTS / Listening… "
            "with @time · +s since listen · Δµs. Scroll with bar or mouse.[/]"
        ),
        "boot_app_3": (
            "[dim]F3 cycles Translation → System → What's New → Command list · "
            "Ctrl+Shift+C copies the active log · F2 = voice bypass.[/]"
        ),
    },
    "pt": {
        "sentence": "Frase",
        "audio": "Audio",
        "idiom": "Idioma",
        "edit": "Editar",
        "edit_n": "Edit N",
        "enew": "Novo txt",
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
        "cls": "Limpar tudo",
        "cls1": "Limpa LC",
        "cls2": "Limpa VOZ",
        "go_top": "Topo",
        "go_footer": "Rodape",
        "export": "Export",
        "replay": "Replay",
        "replay_n": "Replay N",
        "replay_src": "Replay Heard",
        "replay_src_n": "Heard N",
        "snd": "Som",
        "mic": "Mic",
        "bypass": "Bypass",
        "stop": "Parar",
        "path": "Path",
        "path_n": "Path N",
        "folder": "Pasta",
        "list_dev": "Devices",
        "list_voices": "Vozes",
        "list_voices_f": "Vozes filt",
        "ctts": "Mudar TTS",
        "swap": "Trocar",
        "target": "Alvo",
        "synonyms": "Sinonimos",
        "session": "Sessao",
        "menu": "Menu",
        "compact": "Compacta",
        "quit": "Sair",
        "on": "ON",
        "off": "OFF",
        "live": "LIVE",
        "muted": "MUDO",
        "placeholder": "Digite um comando e Enter (ex: s, g, b, enew, ctts, u, q)…",
        "prompt_placeholder": "Digite a resposta e Enter…",
        "starting": "iniciando escuta…",
        "g_swap": "g(trocar)",
        "t_target": "t(alvo)",
        "cmd_tts": "TTS",
        "tab_news": "Novidades",
        "news_header": "Changelog do projeto (CHANGELOG.md)",
        "news_missing": "CHANGELOG.md nao encontrado na raiz do projeto.",
        "news_read_error": "Nao foi possivel ler CHANGELOG.md",
        "tab_commands": "Lista de comandos",
        "search_hit": 'Busca: "{q}" — {i}/{n}',
        "search_none": 'Nenhuma ocorrência: "{q}"',
        "search_no_active": "Sem busca ativa. Use /texto primeiro.  (/n próximo · /p anterior)",
        "search_help": (
            "Busca no log: /texto  ·  /n próximo  ·  /p anterior  ·  /  repetir  ·  "
            "aliases: find texto  ·  find:texto  ·  s?texto"
        ),
        "search_empty_log": "Aba de log ativa vazia — nada para buscar.",
        "bypass_on_label": "F2 Sua voz",
        "bypass_off_label": "F2 Áudio trad.",
        "bypass_tooltip_on": "BYPASS ON — sua voz vai direto à saída (sem tradução). F2 / clique / [b] para desligar.",
        "bypass_tooltip_off": "BYPASS OFF — caminho de áudio traduzido. F2 / clique / [b] para enviar sua voz direta.",
        "scroll_on_label": "F5 Auto↓ ON",
        "scroll_off_label": "F5 Auto↓ OFF",
        "scroll_tooltip_on": "AUTO-SCROLL ON — novas linhas LC/VOZ vão ao fim. F5 / clique para travar rolagem (ler histórico).",
        "scroll_tooltip_off": "AUTO-SCROLL OFF — LC/VOZ ficam no lugar com linhas novas (chunks/comandos). F5 / clique para voltar a seguir.",
        "scroll_footer_on": "Auto↓ ON",
        "scroll_footer_off": "Auto↓ OFF",
        "scroll_log_on": "[F5] Auto-scroll ON — Tradução LC+VOZ seguem linhas novas.",
        "scroll_log_off": "[F5] Auto-scroll OFF — Tradução LC+VOZ rolagem travada (chunks não puxam a vista).",
        "pipe_mic": "Mic",
        "pipe_stt": "STT",
        "pipe_tr": "Trad",
        "pipe_tts": "TTS",
        "pipe_out": "Out",
        "pipe_mic_active": "Ouvindo",
        "pipe_stt_active": "STT…",
        "pipe_tr_active": "Traduz…",
        "pipe_tts_active": "TTS…",
        "pipe_out_active": "Cable",
        "pipe_lc": "LC",
        "pipe_lc_active": "LC●",
        "pipe_bypass": "BYPASS→Out",
        "pipe_muted": "Mic mudo",
        "pipe_tip": "VOZ: Mic → STT → Trad → TTS → Cable Out · LC = LiveCaptions",
        "tab_traducao": "Tradução",
        "tab_sistema": "Sistema",
        "trad_lbl_lc": "LC entrada (LiveCaptions)",
        "trad_lbl_voz": "VOZ mic + comandos",
        "expand": "Expandir",
        "restore": "Restaurar",
        "expand_tip": "Maximizar painel VOZ (direita)",
        "restore_tip": "Restaurar split LC | VOZ",
        "cls_note_lc": "[dim]LC limpo — pares estáveis [LC n] voltam a aparecer aqui[/]",
        "cls_note_voz": "[dim]VOZ limpo — [l] histórico · [lo]/[lt] · F3 Sistema[/]",
        "cls_note_app": "[dim]Sistema limpo — etapas STT/tradução/TTS voltam a aparecer aqui[/]",
        "cls1_note": "[dim]LC (esquerda) limpo — [LC n] Caption/Translated voltam aqui[/]",
        "cls2_note": "[dim]VOZ (direita) limpo — Heard/Translated e comandos voltam aqui[/]",
        "boot_lc_1": (
            "[bold magenta]LC entrada[/] — pares [bold]estáveis[/] "
            "[bold][LC n] Caption / Translated[/]"
        ),
        "boot_lc_2": (
            "[dim]Faixa superior = ao vivo (parcial). "
            "Aqui só commits finais. [lc] pause · show/hide[/]"
        ),
        "boot_lc_3": (
            "[dim]Faixa Live Captions (topo): ═ ↕ captions ═ redimensiona vs "
            "logs do meio · ║ largura LC|VOZ · clique p/ foco.[/]"
        ),
        "boot_voz_1": (
            "[bold cyan]LiveLingo TUI[/] — [bold]VOZ[/] (chunks mic + saída de comandos)"
        ),
        "boot_voz_2": (
            "[dim]Fale no microfone — Heard/Translated (VOZ) aparecem aqui. "
            "Comandos: digite + Enter | setas ↑↓ = histórico.[/]"
        ),
        "boot_voz_3": (
            "[yellow]Áudio OFF por padrão — [s] ao vivo | "
            "[r]/[rN] chunk | [l] lista | [g] swap[/]"
        ),
        "boot_voz_4": (
            "[bold green]Copiar:[/] seleção [bold]Ctrl+C[/]  ·  "
            "log do painel focado [bold]Ctrl+Shift+C[/]  ·  "
            "bypass [bold]F2[/]  ·  "
            "auto-scroll [bold]F5[/]  ·  "
            "busca [bold]/texto[/]  ·  F3 abas"
        ),
        "boot_voz_5": (
            "[dim]Split LC|VOZ: arraste ║ · Expandir no canto superior direito "
            "desta janela · captions ↕ no topo · sash 50/50 duplo-clique. "
            "Sair: Ctrl+Q / [q].[/]"
        ),
        "boot_app_1": (
            "[bold cyan]Sistema[/] — etapas do pipeline, escuta VAD, "
            "timestamps e timing (não polui a aba Tradução)"
        ),
        "boot_app_2": (
            "[dim]Aqui: Transcrevendo / Traduzindo / TTS / Escutando… "
            "com @hora · +s desde escuta · Δµs. Role com a barra ou mouse.[/]"
        ),
        "boot_app_3": (
            "[dim]F3 cicla Tradução → Sistema → Novidades → Lista de comandos · "
            "Ctrl+Shift+C copia o log da aba ativa · F2 = bypass de voz.[/]"
        ),
    },
    "es": {
        "sentence": "Frase",
        "audio": "Audio",
        "idiom": "Idioma",
        "edit": "Editar",
        "edit_n": "Edit N",
        "enew": "Nvo txt",
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
        "placeholder": "Escriba un comando y Enter (ej: s, g, gg, GG, l, q)…",
        "prompt_placeholder": "Escriba la respuesta y Enter…",
        "starting": "iniciando escucha…",
        "g_swap": "g(cambiar)",
        "t_target": "t(destino)",
        "cmd_tts": "TTS",
        "tab_news": "Novedades",
        "news_header": "Changelog del proyecto (CHANGELOG.md)",
        "news_missing": "No se encontro CHANGELOG.md en la raiz del proyecto.",
        "news_read_error": "No se pudo leer CHANGELOG.md",
        "tab_commands": "Lista de comandos",
        "search_hit": 'Búsqueda: "{q}" — {i}/{n}',
        "search_none": 'Sin coincidencias: "{q}"',
        "search_no_active": "Sin búsqueda activa. Use /texto primero.  (/n sig. · /p ant.)",
        "search_help": "Buscar en log: /texto  ·  /n siguiente  ·  /p anterior  ·  /  repetir",
        "search_empty_log": "Pestaña de log vacía — nada que buscar.",
        "bypass_on_label": "F2 Tu voz",
        "bypass_off_label": "F2 Audio trad.",
        "bypass_tooltip_on": "BYPASS ON — tu voz va directa a la salida (sin traducción). F2 / clic / [b] para apagar.",
        "bypass_tooltip_off": "BYPASS OFF — ruta de audio traducido. F2 / clic / [b] para voz directa.",
        "scroll_on_label": "F5 Auto↓ ON",
        "scroll_off_label": "F5 Auto↓ OFF",
        "scroll_footer_on": "Auto↓ ON",
        "scroll_footer_off": "Auto↓ OFF",
    },
    "fr": {
        "sentence": "Phrase",
        "audio": "Audio",
        "idiom": "Langue",
        "edit": "Edit",
        "edit_n": "Edit N",
        "enew": "Nouv txt",
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
        "placeholder": "Tapez une commande et Entree (ex: s, g, gg, GG, l, q)…",
        "prompt_placeholder": "Tapez la reponse et Entree…",
        "starting": "demarrage ecoute…",
        "g_swap": "g(echange)",
        "t_target": "t(cible)",
        "cmd_tts": "TTS",
        "tab_news": "Nouveautes",
        "news_header": "Changelog du projet (CHANGELOG.md)",
        "news_missing": "CHANGELOG.md introuvable a la racine du projet.",
        "news_read_error": "Impossible de lire CHANGELOG.md",
        "tab_commands": "Liste des commandes",
        "search_hit": 'Recherche: "{q}" — {i}/{n}',
        "search_none": 'Aucune occurrence: "{q}"',
        "search_no_active": "Pas de recherche active. Utilisez /texte d'abord.  (/n suiv. · /p préc.)",
        "search_help": "Recherche log: /texte  ·  /n suivant  ·  /p précédent  ·  /  répéter",
        "search_empty_log": "Onglet de log vide — rien à chercher.",
        "bypass_on_label": "F2 Votre voix",
        "bypass_off_label": "F2 Audio trad.",
        "bypass_tooltip_on": "BYPASS ON — votre voix va direct à la sortie (sans traduction). F2 / clic / [b] pour arrêter.",
        "bypass_tooltip_off": "BYPASS OFF — chemin audio traduit. F2 / clic / [b] pour voix directe.",
    },
    "de": {
        "sentence": "Satz",
        "audio": "Audio",
        "idiom": "Sprache",
        "edit": "Edit",
        "edit_n": "Edit N",
        "enew": "Neu txt",
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
        "placeholder": "Befehl eingeben und Enter (z.B. s, g, gg, GG, l, q)…",
        "prompt_placeholder": "Antwort eingeben und Enter…",
        "starting": "hoere zu…",
        "g_swap": "g(tausch)",
        "t_target": "t(ziel)",
        "cmd_tts": "TTS",
        "tab_news": "Neuigkeiten",
        "news_header": "Projekt-Changelog (CHANGELOG.md)",
        "news_missing": "CHANGELOG.md im Projektstamm nicht gefunden.",
        "news_read_error": "CHANGELOG.md konnte nicht gelesen werden",
        "tab_commands": "Befehlsliste",
        "search_hit": 'Suche: "{q}" — {i}/{n}',
        "search_none": 'Keine Treffer: "{q}"',
        "search_no_active": "Keine aktive Suche. Zuerst /text.  (/n weiter · /p zurück)",
        "search_help": "Log-Suche: /text  ·  /n weiter  ·  /p zurück  ·  /  wiederholen",
        "search_empty_log": "Aktiver Log-Tab leer — nichts zu suchen.",
        "bypass_on_label": "F2 Stimme",
        "bypass_off_label": "F2 Audio übers.",
        "bypass_tooltip_on": "BYPASS ON — Ihre Stimme geht direkt zum Ausgang (ohne Übersetzung). F2 / Klick / [b] aus.",
        "bypass_tooltip_off": "BYPASS OFF — übersetzter Audiopfad. F2 / Klick / [b] für Direktstimme.",
    },
    "it": {
        "sentence": "Frase",
        "audio": "Audio",
        "idiom": "Lingua",
        "edit": "Modif",
        "edit_n": "Mod N",
        "enew": "Nuovo",
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
        "placeholder": "Digita un comando e Invio (es: s, g, gg, GG, l, q)…",
        "prompt_placeholder": "Digita la risposta e Invio…",
        "starting": "avvio ascolto…",
        "g_swap": "g(scambia)",
        "t_target": "t(target)",
        "cmd_tts": "TTS",
        "tab_news": "Novita",
        "news_header": "Changelog del progetto (CHANGELOG.md)",
        "news_missing": "CHANGELOG.md non trovato nella root del progetto.",
        "news_read_error": "Impossibile leggere CHANGELOG.md",
        "tab_commands": "Elenco comandi",
        "search_hit": 'Ricerca: "{q}" — {i}/{n}',
        "search_none": 'Nessuna occorrenza: "{q}"',
        "search_no_active": "Nessuna ricerca attiva. Usa /testo prima.  (/n succ. · /p prec.)",
        "search_help": "Cerca nel log: /testo  ·  /n successivo  ·  /p precedente  ·  /  ripeti",
        "search_empty_log": "Scheda log vuota — nulla da cercare.",
        "bypass_on_label": "F2 Tua voce",
        "bypass_off_label": "F2 Audio trad.",
        "bypass_tooltip_on": "BYPASS ON — la tua voce va diretta all'uscita (senza traduzione). F2 / clic / [b] per spegnere.",
        "bypass_tooltip_off": "BYPASS OFF — percorso audio tradotto. F2 / clic / [b] per voce diretta.",
    },
    "zh": {
        "sentence": "句子",
        "audio": "音频",
        "idiom": "语言",
        "edit": "编辑",
        "edit_n": "编辑N",
        "enew": "新文本",
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
        "placeholder": "输入命令后回车 (如 s, g, gg, GG, l, q)…",
        "prompt_placeholder": "输入回答后回车…",
        "starting": "开始监听…",
        "g_swap": "g(交换)",
        "t_target": "t(目标)",
        "cmd_tts": "TTS",
        "tab_news": "更新",
        "news_header": "项目更新日志 (CHANGELOG.md)",
        "news_missing": "项目根目录未找到 CHANGELOG.md。",
        "news_read_error": "无法读取 CHANGELOG.md",
        "tab_commands": "命令列表",
        "search_hit": '搜索: "{q}" — {i}/{n}',
        "search_none": '无匹配: "{q}"',
        "search_no_active": "无活动搜索。请先 /文本。  (/n 下一个 · /p 上一个)",
        "search_help": "日志搜索: /文本  ·  /n 下一个  ·  /p 上一个  ·  / 重复",
        "search_empty_log": "当前日志页为空 — 无可搜索内容。",
        "bypass_on_label": "F2 您的声音",
        "bypass_off_label": "F2 翻译音频",
        "bypass_tooltip_on": "BYPASS 开 — 原始人声直达输出（无翻译）。F2 / 点击 / [b] 关闭。",
        "bypass_tooltip_off": "BYPASS 关 — 翻译音频路径。F2 / 点击 / [b] 直送人声。",
    },
    "ja": {
        "sentence": "文",
        "audio": "音声",
        "idiom": "言語",
        "edit": "編集",
        "edit_n": "編集N",
        "enew": "新規文",
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
        "placeholder": "コマンドを入力してEnter (例: s, g, gg, GG, l, q)…",
        "prompt_placeholder": "回答を入力してEnter…",
        "starting": "待受中…",
        "g_swap": "g(入替)",
        "t_target": "t(対象)",
        "cmd_tts": "TTS",
        "tab_news": "新着",
        "news_header": "プロジェクト変更履歴 (CHANGELOG.md)",
        "news_missing": "プロジェクト直下に CHANGELOG.md が見つかりません。",
        "news_read_error": "CHANGELOG.md を読めませんでした",
        "tab_commands": "コマンド一覧",
        "search_hit": '検索: "{q}" — {i}/{n}',
        "search_none": '一致なし: "{q}"',
        "search_no_active": "検索がありません。先に /text。  (/n 次 · /p 前)",
        "search_help": "ログ検索: /text  ·  /n 次  ·  /p 前  ·  / 再実行",
        "search_empty_log": "アクティブログが空です — 検索対象なし。",
        "bypass_on_label": "F2 あなたの声",
        "bypass_off_label": "F2 翻訳音声",
        "bypass_tooltip_on": "BYPASS ON — 生の声を出力へ（翻訳なし）。F2 / クリック / [b] でオフ。",
        "bypass_tooltip_off": "BYPASS OFF — 翻訳音声パス。F2 / クリック / [b] で直送。",
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
    if code in ("por", "pt-br", "pt_br", "br", "bra"):
        code = "pt"
    return code if code in _FOOTER_I18N else "en"


def _footer_i18n() -> dict:
    """Labels for footer menu / placeholder in current SOURCE_LANG."""
    pack = _FOOTER_I18N.get(_source_lang_code()) or _FOOTER_I18N["en"]
    # Fill any missing keys from English
    base = dict(_FOOTER_I18N["en"])
    base.update(pack)
    return base


def _project_root() -> str:
    """LiveLingo project root (parent of livelingo package)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _changelog_path() -> str | None:
    """Return path to CHANGELOG.md if present (case variants)."""
    root = _project_root()
    for name in ("CHANGELOG.md", "changelog.md", "Changelog.md"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return None


def _load_changelog_text() -> tuple[str | None, str | None]:
    """
    Load CHANGELOG.md from project root.

    Returns (text, error_message). On success error_message is None.
    """
    path = _changelog_path()
    i18n = _footer_i18n()
    if not path:
        return None, i18n.get("news_missing", "CHANGELOG.md not found.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except Exception as exc:
        return (
            None,
            f"{i18n.get('news_read_error', 'Could not read CHANGELOG.md')}: {exc}",
        )


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
    """Main LiveLingo TUI — listen header + 4 log tabs + cmd."""

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
    /*
     * Live Captions strip (above log tabs) — resizable bottom edge
     * (CaptionsHSash) so #log-tabs (middle logs) grows/shrinks.
     */
    #captions-panel {
        height: 8;
        min-height: 5;
        max-height: 28;
        layout: vertical;
        width: 1fr;
        margin: 0;
        padding: 0 1 0 1;
        background: #1a1b26;
        border: solid #7aa2f7;
        overflow: hidden;
    }
    #captions-panel.-error {
        border: solid #f7768e;
    }
    #captions-panel.-paused {
        border: solid #e0a020;
    }
    #captions-title {
        height: 1;
        min-height: 1;
        max-height: 1;
        width: 1fr;
        text-style: bold;
        color: #7aa2f7;
        content-align: left middle;
        overflow: hidden;
    }
    #caption-src {
        height: 1fr;
        min-height: 1;
        width: 1fr;
        color: #c0caf5;
        content-align: left top;
        overflow: hidden;
        padding: 0;
    }
    #caption-tgt {
        height: 1fr;
        min-height: 1;
        width: 1fr;
        color: #9ece6a;
        text-style: bold;
        content-align: left top;
        overflow: hidden;
        padding: 0;
    }
    #caption-status {
        height: 1;
        min-height: 1;
        max-height: 1;
        width: 1fr;
        color: #565f89;
        content-align: left middle;
        overflow: hidden;
    }
    #captions-hsash {
        width: 1fr;
        height: 1;
        min-height: 1;
        max-height: 1;
        margin: 0 -1;
    }
    /* Log tabs: Tradução + Sistema + Novidades + Command list (shrink under captions) */
    #log-tabs {
        height: 1fr;
        min-height: 5;
        width: 1fr;
        margin: 0;
        padding: 0;
        background: $surface;
        border: solid $accent;
    }
    #log-tabs > ContentSwitcher {
        height: 1fr;
    }
    #log-tabs TabPane {
        height: 1fr;
        padding: 0;
    }
    /* Tradução: LC | sash | VOZ (resizable + expand/restore) */
    #trad-pane {
        height: 1fr;
        width: 1fr;
        layout: vertical;
    }
    #trad-headers {
        height: 1;
        width: 1fr;
        layout: horizontal;
        background: $panel;
        align: left middle;
    }
    #trad-hdr-lc, #trad-hdr-voz {
        height: 1;
        width: 1fr;
        layout: horizontal;
        padding: 0 1;
        align: left middle;
    }
    #trad-lbl-lc {
        width: 1fr;
        color: #e040fb;
        text-style: bold;
        content-align: left middle;
    }
    #trad-lbl-voz {
        width: 1fr;
        color: #e0af68;
        text-style: bold;
        content-align: left middle;
    }
    /* Expandir inside right (VOZ) log window — top-right corner */
    #trad-btn-voz {
        width: auto;
        min-width: 10;
        height: 1;
        min-height: 1;
        max-height: 1;
        padding: 0 1;
        border: none;
        background: $accent;
        color: $text;
        text-style: bold;
        dock: none;
    }
    #trad-btn-voz:hover {
        background: $accent-lighten-2;
    }
    #trad-split {
        height: 1fr;
        width: 1fr;
        layout: horizontal;
    }
    #trad-lc-col, #trad-voz-col {
        height: 1fr;
        width: 1fr;
        min-width: 12;
        layout: vertical;
    }
    #trad-lc-col.-hidden, #trad-voz-col.-hidden {
        display: none;
    }
    /* Focus is shown via header label colors + sash — no inner box border
       (orange/pink frames around LC/VOZ looked like a nested unnecessary frame). */
    #trad-lc-col.-focused,
    #trad-voz-col.-focused {
        border: none;
    }
    #trad-sash {
        width: 1;
        min-width: 1;
        max-width: 1;
        height: 1fr;
    }
    #log, #log-lc, #log-app, #log-news, #log-cmds {
        height: 1fr;
        margin: 0;
        padding: 0 1;
        background: $surface;
        border: none;
        scrollbar-size: 1 1;
        width: 1fr;
        min-width: 12;
        overflow-y: auto;
        overflow-x: auto;
    }
    /* Keep panes full-width so wrap width is sane when switching tabs */
    #tab-main, #tab-app, #tab-news, #tab-cmds {
        width: 1fr;
        height: 1fr;
    }
    /*
     * Menu + command bar (above docked Footer — do NOT dock #bottom).
     *
     * height 9 content (no top border):
     *   #hint 6 (≈5 menu lines + 1 blank) + #cmd-row 3
     */
    #bottom {
        height: 9;
        layout: vertical;
        background: $panel;
        border: none;
        padding: 0 1 0 1;
    }
    /* Compact UI ([u]): menu hidden — only command row */
    #bottom.-compact {
        height: 3;
    }
    #hint {
        height: 6;
        width: 1fr;
        color: $text;
        padding: 0;
        background: $panel;
        content-align: left top;
        overflow-y: auto;
        overflow-x: hidden;
    }
    #bottom.-compact #hint {
        display: none;
        height: 0;
        min-height: 0;
        max-height: 0;
    }
    #cmd-row {
        height: 3;
        min-height: 3;
        max-height: 3;
        width: 1fr;
        layout: horizontal;
        background: $panel;
        padding: 0 1;
        align: left middle;
    }
    /* Fixed gutters between pipe | command | TTS (not 1fr — that stole cmd width). */
    #cmd-flex-l, #cmd-flex-r {
        width: 2;
        min-width: 2;
        max-width: 2;
        height: 3;
    }
    /*
     * Pipeline activity bar — left edge of command row.
     * Shows Mic → STT → Trad → TTS → Out (+ LC when LiveCaptions is busy).
     */
    #pipe-bar {
        width: auto;
        min-width: 28;
        max-width: 44;
        height: 3;
        min-height: 3;
        max-height: 3;
        margin: 0;
        padding: 0 1;
        background: $surface;
        color: $text;
        border: round #3d4f6f;
        content-align: left middle;
        overflow: hidden;
        text-style: none;
    }
    #pipe-bar.-busy {
        border: round $accent;
    }
    #pipe-bar.-lc {
        border: round #c44dff;
    }
    /*
     * F2 bypass — compact 1-row chip on #bypass-row (between Live Captions
     * and the tab strip). Centered with equal flex pads ≈ above LC|VOZ sash.
     * Do NOT mount into Tabs / TabbedContent (breaks log panes).
     */
    #bypass-row {
        height: 1;
        min-height: 1;
        max-height: 1;
        width: 1fr;
        layout: horizontal;
        background: $panel;
        padding: 0;
        margin: 0;
    }
    #bypass-pad-l, #bypass-pad-r {
        width: 1fr;
        min-width: 0;
        height: 1;
    }
    #cmd-bypass {
        width: auto;
        min-width: 12;
        max-width: 20;
        height: 1;
        min-height: 1;
        max-height: 1;
        margin: 0;
        padding: 0 1;
        text-style: bold;
        content-align: center middle;
        overflow: hidden;
        border: none;
        background: #ffffff;
        color: #1a1b26;
    }
    #cmd-bypass.-off {
        background: #ffffff;
        color: #1a1b26;
    }
    #cmd-bypass.-on {
        background: #2d9a4e;
        color: #ffffff;
    }
    #cmd-bypass:hover {
        text-style: bold underline;
        background: #e8e8e8;
    }
    #cmd-bypass.-on:hover {
        background: #38b05a;
    }
    /*
     * F5 auto-scroll — chip next to F2 on #bypass-row.
     * Green = follow bottom (default); amber = locked (no yank on new lines).
     */
    #cmd-scroll {
        width: auto;
        min-width: 12;
        max-width: 18;
        height: 1;
        min-height: 1;
        max-height: 1;
        margin: 0 0 0 1;
        padding: 0 1;
        text-style: bold;
        content-align: center middle;
        overflow: hidden;
        border: none;
        background: #2d9a4e;
        color: #ffffff;
    }
    #cmd-scroll.-on {
        background: #2d9a4e;
        color: #ffffff;
    }
    #cmd-scroll.-off {
        background: #c47a12;
        color: #ffffff;
    }
    #cmd-scroll:hover {
        text-style: bold underline;
        background: #38b05a;
    }
    #cmd-scroll.-off:hover {
        background: #d4891a;
    }
    /*
     * Command box — takes most of the footer row (1fr); pipe + TTS stay auto.
     * Gutters via #cmd-flex-l/r (2 cols each).
     */
    #cmd-box {
        width: 1fr;
        min-width: 48;
        max-width: 1fr;
        height: 3;
        min-height: 3;
        max-height: 3;
        layout: vertical;
        align: left middle;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }
    #cmd-box:focus-within {
        border: round $primary;
        background: $surface;
    }
    #cmd {
        width: 1fr;
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
        border: none;
    }
    #cmd:focus {
        border: none;
    }
    /* TTS badge: black fill, blue border (same as #cmd-box), white text. */
    #cmd-tts {
        width: auto;
        min-width: 12;
        max-width: 40;
        height: 3;
        min-height: 3;
        max-height: 3;
        margin: 0;
        padding: 0 1;
        background: #0d0d0d;
        color: #ffffff;
        border: round $accent;
        text-style: bold;
        content-align: center middle;
        overflow: hidden;
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

    # Ctrl+C = selection (or full log if none); Ctrl+Shift+C = full log.
    # F2 = voice bypass (compact badge above LC|VOZ sash).
    # F3 = cycle Tradução → Sistema → Novidades → …
    # F4 = compact UI · F5 = auto-scroll lock for Tradução LC+VOZ
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
        Binding("f2", "toggle_bypass", "Bypass", show=True, priority=True),
        Binding("f3", "toggle_log_tab", "Log tab", show=True, priority=True),
        Binding("f4", "toggle_compact_ui", "Compact UI", show=True, priority=True),
        Binding(
            "f5",
            "toggle_trad_auto_scroll",
            "Auto↓ ON",
            show=True,
            priority=True,
        ),
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
        caption_service=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.pipeline = pipeline
        self.synonym_lookup = synonym_lookup
        self._dispatch = dispatch_command
        self._listen_msgs_fn = listen_msgs_fn
        self._help_fn = help_fn
        self.caption_service = caption_service
        self._prompt_q: queue.Queue = queue.Queue()
        self._prompt_waiting = threading.Event()
        self._prompt_label = ""
        # Prefill for #cmd while waiting a prompt (e.g. edit last sentence).
        self._prompt_prefill = ""
        # When True, force #cmd keystrokes/value to UPPERCASE (command [t] only).
        self._prompt_force_upper = False
        self._cmd_busy = False
        # Non-blocking UI actions from worker threads (e.g. open TTS modal).
        self._ui_action_q: queue.Queue = queue.Queue()
        self._frame_i = 0
        self._speaking = False
        self._sound_on = False
        self._mic_muted = False
        self._mic_mute_name: str = ""
        self._passthrough = False
        # F5: when True (default), new Tradução LC/VOZ lines force scroll-to-bottom.
        # When False, both panes keep viewport (chunks / command output won't yank).
        self._trad_follow_scroll: bool = True
        self._log_queue: queue.Queue = queue.Queue()
        self._cached_log_width = 120
        self._cached_log_width_lc = 60
        # Tradução split: ratio left (LC), expand None|"lc"|"voz", focus lc|voz
        self._trad_ratio: float = 0.5
        self._trad_expand: str | None = None  # None = split, "lc"|"voz" = maximized
        self._trad_focus: str = "voz"  # which sub-pane search/gg/copy use
        # Live Captions strip height in rows (drag bottom edge vs middle logs)
        self._captions_height: int = 8
        # VOZ pipe bar: mic → stt → translate → tts → play (Cable Out)
        self._pipe_stage: str = "idle"
        self._pipe_stage_t: float = 0.0
        self._pipe_chunk: int | None = None
        self._pipe_lc_active: bool = False
        self._pipe_lc_busy: bool = False  # translating / partial text
        self._pipe_last_markup: str = ""
        self._pipe_stage_q: queue.Queue = queue.Queue()
        # Live Captions panel state (updated from worker via call_from_thread)
        self._caption_data: dict = {
            "status": "idle",
            "original": "",
            "translated": "",
            "original_live": "",
            "error": None,
            "paused": False,
        }
        self._caption_queue: queue.Queue = queue.Queue()
        # Vim-style log search: /query · /n · /p (active tab)
        self._search_query: str = ""
        self._search_hits: list[int] = []
        self._search_i: int = -1
        self._search_panel: str = "main"
        # Compact UI: hide menu + safe host-window height shrink ([u] / F4).
        # Never touch console buffer APIs (that corrupted Textual before).
        # Initial value from config TUI_MINIMAL (applied on_mount when widgets exist).
        self._compact_ui = False
        try:
            import config as _cfg

            self._want_minimal_start = bool(getattr(_cfg, "TUI_MINIMAL", False))
        except Exception:
            self._want_minimal_start = False
        self._saved_window_geom: dict | None = None
        # Command history (↑/↓) — list of past submissions; index -1 = draft line
        self._cmd_history: list[str] = []
        self._cmd_history_i: int = -1
        self._cmd_draft: str = ""
        # Hold Backspace/Delete → accelerate to whole-word erase
        self._cmd_erase_key: str | None = None
        self._cmd_erase_streak: int = 0
        self._cmd_erase_last_t: float = 0.0

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
            yield SystemCommand("Keys", h["keys_hide"], self.action_hide_help_panel)
        else:
            yield SystemCommand("Keys", h["keys_show"], self.action_show_help_panel)

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
            yield SystemCommand("Minimize", h["minimize"], screen.action_minimize)
        elif allow_max:
            yield SystemCommand("Maximize", h["maximize"], screen.action_maximize)

        yield SystemCommand(
            "Screenshot",
            h["screenshot"],
            lambda: self.set_timer(0.1, self.action_livelingo_screenshot),
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Fixed top listen bar — single row only (robot + pair + audio + status)
        yield Static(_footer_i18n()["starting"], id="listen-header", markup=False)
        # Live Captions strip (Windows LiveCaptions) — above log tabs
        with Vertical(id="captions-panel"):
            yield Static(
                "Live Captions  ·  [lc] pause  ·  [lc show]/[lc hide]",
                id="captions-title",
                markup=False,
            )
            yield Static(
                "SRC  (aguardando Windows LiveCaptions…)",
                id="caption-src",
                markup=False,
            )
            yield Static(
                "TGT  —",
                id="caption-tgt",
                markup=False,
            )
            yield Static(
                "status: idle",
                id="caption-status",
                markup=False,
            )
            # Bottom edge: drag ↕ to split captions (top) vs log tabs (middle)
            yield CaptionsHSash(id="captions-hsash")
        # Four log panels: translation / pipeline / changelog / command list
        # min_width must be wide: inactive TabPane has ~0 layout width, and
        # RichLog bakes wrap at write time (min_width was 20 → column-of-chars).
        _log_min_w = _terminal_log_width(100)
        _fi18n = _footer_i18n()
        _news_label = _fi18n.get("tab_news", "What's New")
        _cmds_label = _fi18n.get(
            "tab_commands", command_help.tab_title(_source_lang_code())
        )
        # F2 + F5 chips: own row under captions / above tabs (never into Tabs)
        with Horizontal(id="bypass-row"):
            yield Static("", id="bypass-pad-l", markup=False)
            yield Static(
                _fi18n.get("bypass_off_label", "F2 Transl. audio"),
                id="cmd-bypass",
                classes="-off",
                markup=False,
            )
            yield Static(
                _fi18n.get("scroll_on_label", "F5 Auto↓ ON"),
                id="cmd-scroll",
                classes="-on",
                markup=False,
            )
            yield Static("", id="bypass-pad-r", markup=False)
        with TabbedContent(id="log-tabs", initial="tab-main"):
            with TabPane(_fi18n.get("tab_traducao", "Translation"), id="tab-main"):
                with Vertical(id="trad-pane"):
                    with Horizontal(id="trad-headers"):
                        with Horizontal(id="trad-hdr-lc"):
                            yield Static(
                                _fi18n.get("trad_lbl_lc", "LC in (LiveCaptions)"),
                                id="trad-lbl-lc",
                                markup=False,
                            )
                        with Horizontal(id="trad-hdr-voz"):
                            yield Static(
                                _fi18n.get("trad_lbl_voz", "VOICE mic + commands"),
                                id="trad-lbl-voz",
                                markup=False,
                            )
                            # Expand: top-right of right (VOZ) log window
                            yield Button(
                                _fi18n.get("expand", "Expand"),
                                id="trad-btn-voz",
                                flat=True,
                                compact=True,
                            )
                    with Horizontal(id="trad-split"):
                        with Vertical(id="trad-lc-col"):
                            yield SelectableRichLog(
                                id="log-lc",
                                highlight=False,
                                markup=True,
                                wrap=True,
                                auto_scroll=True,
                                max_lines=5000,
                                min_width=max(40, _log_min_w // 2),
                                pane_role="lc",
                            )
                        yield TradSash(id="trad-sash")
                        with Vertical(id="trad-voz-col", classes="-focused"):
                            yield SelectableRichLog(
                                id="log",
                                highlight=False,
                                markup=True,
                                wrap=True,
                                auto_scroll=True,
                                max_lines=5000,
                                min_width=max(40, _log_min_w // 2),
                                pane_role="voz",
                            )
            with TabPane(_fi18n.get("tab_sistema", "System"), id="tab-app"):
                yield SelectableRichLog(
                    id="log-app",
                    highlight=False,
                    markup=True,
                    wrap=True,
                    auto_scroll=True,
                    max_lines=8000,
                    min_width=_log_min_w,
                )
            with TabPane(_news_label, id="tab-news"):
                yield SelectableRichLog(
                    id="log-news",
                    highlight=False,
                    markup=True,
                    wrap=True,
                    auto_scroll=False,
                    max_lines=20000,
                    min_width=_log_min_w,
                )
            with TabPane(_cmds_label, id="tab-cmds"):
                yield SelectableRichLog(
                    id="log-cmds",
                    highlight=False,
                    markup=True,
                    wrap=True,
                    auto_scroll=False,
                    max_lines=20000,
                    min_width=_log_min_w,
                )
        with Vertical(id="bottom"):
            yield Static("", id="hint", markup=True)
            with Horizontal(id="cmd-row"):
                # Left: pipeline stages · center: command · right: TTS voice
                yield Static(
                    "[dim]○Mic › ○STT › ○Trad › ○TTS › ○Out[/]",
                    id="pipe-bar",
                    markup=True,
                )
                yield Static("", id="cmd-flex-l", markup=False)
                # Border lives on #cmd-box so left/right sides never clip
                with Vertical(id="cmd-box"):
                    yield Input(
                        placeholder=_footer_i18n()["placeholder"],
                        id="cmd",
                    )
                yield Static("", id="cmd-flex-r", markup=False)
                # Current TTS_VOICE badge (display only; change with [ctts nome])
                yield Static(
                    "TTS ?",
                    id="cmd-tts",
                    markup=False,
                )
        yield Footer()

    def on_mount(self) -> None:
        ui_mod.set_log_sink(self._sink_from_worker)
        ui_mod.set_width_provider(self._log_content_width)
        ui_mod.set_pipeline_stage_sink(self._pipeline_stage_from_worker)
        self._load_cmd_history()
        # One drain tick: logs + deferred UI actions (keep light for STT latency).
        self.set_interval(0.05, self._drain_pending)
        # ~0.15s tick so robot bounce feels smooth (classic was 0.12–0.25s)
        self.set_interval(0.15, self._tick_status)
        self.set_interval(0.5, self._refresh_log_width)
        # Menu is mostly static; refresh less often to free the UI thread for log lines.
        self.set_interval(2.0, self._refresh_cmd_menu)
        self._refresh_log_width()
        self._refresh_cmd_menu()
        self._bind_caption_service()
        self._paint_captions_panel()
        self._paint_pipe_bar(force=True)
        try:
            self._refresh_bypass_badge()
        except Exception:
            pass
        try:
            self._refresh_scroll_follow_ui()
        except Exception:
            pass
        try:
            self.captions_set_height(int(getattr(self, "_captions_height", 8) or 8))
        except Exception:
            pass
        try:
            bar = self.query_one("#pipe-bar", Static)
            tip = _footer_i18n().get(
                "pipe_tip",
                "VOZ: Mic → STT → Trad → TTS → Cable Out · LC = LiveCaptions",
            )
            bar.tooltip = tip
        except Exception:
            pass
        # TUI_MINIMAL=true → open already compact (menu hidden; same as F4/[u])
        if getattr(self, "_want_minimal_start", False):
            try:
                self.set_compact_ui(True)
            except Exception:
                pass
        t = _footer_i18n()
        try:
            lc_log = self.query_one("#log-lc", SelectableRichLog)
            for key in ("boot_lc_1", "boot_lc_2", "boot_lc_3"):
                line = t.get(key)
                if line:
                    lc_log.write(line)
            lc_log.write("")
        except Exception:
            pass
        log = self.query_one("#log", SelectableRichLog)
        for key in ("boot_voz_1", "boot_voz_2", "boot_voz_3"):
            line = t.get(key)
            if line:
                log.write(line)
        # Phrase-cache inventory (pairs / words by language) under the audio tip
        try:
            import config as cfg

            from .phrase_cache import format_cache_inventory_summary, get_phrase_cache

            pc = getattr(getattr(self, "pipeline", None), "phrase_cache", None)
            if pc is None:
                try:
                    pc = get_phrase_cache(cfg)
                except Exception:
                    pc = None
            for line in format_cache_inventory_summary(cfg, pc):
                log.write(line)
        except Exception:
            log.write(
                "[dim]Phrase cache: (summary unavailable) · use [pc] when pipeline starts[/]"
            )
        for key in ("boot_voz_4", "boot_voz_5"):
            line = t.get(key)
            if line:
                log.write(line)
        log.write("")  # blank line after startup tip
        try:
            self._apply_trad_split_layout()
            self.set_trad_focus("voz")
        except Exception:
            pass
        try:
            app_log = self.query_one("#log-app", SelectableRichLog)
            for key in ("boot_app_1", "boot_app_2", "boot_app_3"):
                line = t.get(key)
                if line:
                    app_log.write(line)
        except Exception:
            pass
        try:
            self._fill_news_tab()
        except Exception:
            pass
        try:
            self._fill_commands_tab()
        except Exception:
            pass
        self.query_one("#cmd", Input).focus()
        try:
            self._sound_on = bool(self.pipeline.is_sound_enabled())
            self._mic_muted = bool(self.pipeline.is_mic_muted())
        except Exception:
            pass
        try:
            if hasattr(self.pipeline, "is_passthrough_active"):
                self._passthrough = bool(self.pipeline.is_passthrough_active())
        except Exception:
            pass
        self._cmd_tts_label = ""
        self._refresh_cmd_tts()
        try:
            self._refresh_bypass_badge()
        except Exception:
            pass
        self._tick_status()

    def on_unmount(self) -> None:
        ui_mod.set_log_sink(None)
        ui_mod.set_width_provider(None)
        try:
            ui_mod.set_pipeline_stage_sink(None)
        except Exception:
            pass
        try:
            svc = self.caption_service
            if svc is not None:
                svc.set_display_callback(None)
        except Exception:
            pass
        try:
            self._save_cmd_history()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Live Captions panel (Windows LiveCaptions → strip above log tabs)
    # ------------------------------------------------------------------ #
    def _bind_caption_service(self) -> None:
        """Wire CaptionService → queue → UI thread paint."""
        svc = self.caption_service
        if svc is None:
            try:
                svc = getattr(self.pipeline, "caption_service", None)
                self.caption_service = svc
            except Exception:
                svc = None
        if svc is None:
            err = "CaptionService não iniciado"
            try:
                import config as cfg

                from .livecaptions import is_windows

                if not getattr(cfg, "LIVE_CAPTIONS_ENABLED", True):
                    err = "LIVE_CAPTIONS_ENABLED=false"
                elif not is_windows():
                    err = "Live Captions só no Windows 11"
            except Exception:
                pass
            self._caption_data = {
                "status": "disabled",
                "original": "",
                "translated": "",
                "original_live": "",
                "error": err,
                "paused": False,
            }
            return

        def _on_display(data: dict) -> None:
            try:
                self._caption_queue.put_nowait(dict(data or {}))
            except Exception:
                pass

        try:
            svc.set_display_callback(_on_display)
            # Push current snapshot immediately
            try:
                snap = svc.snapshot()
                self._caption_queue.put_nowait(dict(snap or {}))
            except Exception:
                pass
        except Exception:
            pass

    def post_caption_update(self, data: dict) -> None:
        """Thread-safe: enqueue caption panel update (or call from UI thread)."""
        try:
            self._caption_queue.put_nowait(dict(data or {}))
        except Exception:
            pass

    def _drain_caption_queue(self) -> None:
        latest = None
        try:
            while True:
                latest = self._caption_queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._caption_data.update(latest)
            self._paint_captions_panel()

    def _paint_captions_panel(self) -> None:
        """Refresh #captions-panel widgets from self._caption_data."""
        d = self._caption_data or {}
        status = str(d.get("status") or "idle")
        err = d.get("error")
        paused = bool(d.get("paused"))
        live = (d.get("original_live") or "").strip()
        original = (d.get("original") or "").strip()
        translated = (d.get("translated") or "").strip()
        src_show = live or original or "—"
        tgt_show = translated or "—"

        # Caption pair (default inverted vs voice: EN→BR when voice is BR→EN)
        src_l = (d.get("caption_source_lang") or "").upper()
        tgt_l = (d.get("caption_target_lang") or "").upper()
        if not src_l or not tgt_l:
            try:
                import config as cfg

                from .livecaptions import caption_lang_pair

                s, t = caption_lang_pair(cfg)
                src_l, tgt_l = s.upper(), t.upper()
            except Exception:
                src_l, tgt_l = "SRC", "TGT"
        if src_l == "PT":
            src_l = "BR"
        if tgt_l == "PT":
            tgt_l = "BR"

        # Truncate for fixed-height strip
        def _clip(s: str, n: int = 220) -> str:
            s = (s or "").replace("\n", " ").strip()
            return s if len(s) <= n else s[: n - 1] + "…"

        try:
            panel = self.query_one("#captions-panel", Vertical)
            panel.remove_class("-error")
            panel.remove_class("-paused")
            if status == "error" or err:
                panel.add_class("-error")
            elif paused or status == "paused":
                panel.add_class("-paused")
        except Exception:
            pass

        title_bits = ["Live Captions", f"{src_l}→{tgt_l}"]
        if paused or status == "paused":
            title_bits.append("PAUSED")
        elif status == "translating":
            title_bits.append("traduzindo…")
        elif status == "running":
            title_bits.append("LIVE")
        elif status == "starting":
            title_bits.append("iniciando…")
        elif status == "disabled":
            title_bits.append("OFF")
        elif status == "error":
            title_bits.append("ERRO")
        title_bits.append("[lc] pause · [lc show]/[lc hide]")
        try:
            self.query_one("#captions-title", Static).update("  ·  ".join(title_bits))
        except Exception:
            pass
        # Mirror LC activity onto the command-row pipe bar
        try:
            self._sync_lc_pipe_from_captions()
            self._paint_pipe_bar()
        except Exception:
            pass
        try:
            self.query_one("#caption-src", Static).update(f"{src_l}  {_clip(src_show)}")
        except Exception:
            pass
        try:
            self.query_one("#caption-tgt", Static).update(f"{tgt_l}  {_clip(tgt_show)}")
        except Exception:
            pass
        disp_status = "paused" if paused else status
        st_line = f"status: {disp_status}  ·  traduz {src_l}→{tgt_l}"
        if err:
            st_line += f"  ·  {str(err)[:80]}"
        try:
            self.query_one("#caption-status", Static).update(st_line)
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
            self._cmd_history = [ln for ln in lines if ln.strip()][-_CMD_HISTORY_MAX:]
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
        """Cache VOZ + LC log content widths on the UI thread."""
        floor = _terminal_log_width(100)
        half = max(24, floor // 2)
        # Keep min_width in sync so inactive-tab writes stay wide enough
        for lid in ("#log", "#log-lc", "#log-app", "#log-news", "#log-cmds"):
            try:
                wlog = self.query_one(lid, SelectableRichLog)
                if lid in ("#log", "#log-lc"):
                    wlog.min_width = half
                else:
                    wlog.min_width = floor
            except Exception:
                pass

        def _safe_w(lid: str, fallback: int) -> int:
            try:
                wlog = self.query_one(lid, SelectableRichLog)
                safe = int(wlog._safe_render_width())  # type: ignore[attr-defined]
                if safe >= 12:
                    return safe
            except Exception:
                pass
            try:
                wlog = self.query_one(lid, SelectableRichLog)
                w = int(getattr(wlog.size, "width", 0) or 0)
                if w >= 12:
                    return max(12, w - 2)
            except Exception:
                pass
            return fallback

        self._cached_log_width = _safe_w("#log", half)
        self._cached_log_width_lc = _safe_w("#log-lc", half)

    def _log_content_width(self) -> int:
        """Usable columns inside VOZ #log (thread-safe via cached value)."""
        w = int(getattr(self, "_cached_log_width", 0) or 0)
        return w if w >= 12 else 60

    def _log_content_width_lc(self) -> int:
        """Usable columns inside #log-lc."""
        w = int(getattr(self, "_cached_log_width_lc", 0) or 0)
        return w if w >= 12 else 60

    # ------------------------------------------------------------------ #
    # Tradução split (ratio / expand / focus)
    # ------------------------------------------------------------------ #
    def set_trad_focus(self, role: str) -> None:
        """Mark LC or VOZ sub-pane as target for search / gg / copy."""
        role = "lc" if str(role or "").lower() == "lc" else "voz"
        self._trad_focus = role
        try:
            lc_col = self.query_one("#trad-lc-col")
            voz_col = self.query_one("#trad-voz-col")
            if role == "lc":
                lc_col.add_class("-focused")
                voz_col.remove_class("-focused")
            else:
                voz_col.add_class("-focused")
                lc_col.remove_class("-focused")
        except Exception:
            pass
        try:
            self._update_trad_voz_expand_label()
        except Exception:
            pass

    def trad_set_ratio_from_drag(
        self, start_screen_x: int, start_ratio: float, screen_x: int
    ) -> None:
        """Update split ratio while dragging the sash (screen coordinates)."""
        del start_screen_x, start_ratio  # absolute x → ratio (more stable)
        try:
            split = self.query_one("#trad-split")
            region = split.content_region
            total = int(region.width or 0)
            origin = int(region.x or 0)
        except Exception:
            total = 0
            origin = 0
        if total < 20:
            try:
                total = max(20, int(self.size.width or 80) - 4)
                origin = 0
            except Exception:
                total = 80
                origin = 0
        usable = max(16, total - 1)
        left = int(screen_x) - origin
        min_c = 12
        left = max(min_c, min(usable - min_c, left))
        ratio = max(0.12, min(0.88, left / float(usable)))
        self._trad_ratio = ratio
        if self._trad_expand is not None:
            self._trad_expand = None
        self._apply_trad_split_layout()

    def trad_restore_split(self, ratio: float | None = None) -> None:
        """Leave expand mode; optional ratio (default keep last / 0.5)."""
        if ratio is not None:
            self._trad_ratio = max(0.12, min(0.88, float(ratio)))
        self._trad_expand = None
        self._apply_trad_split_layout()
        self._refresh_log_width()

    def captions_set_height(self, rows: int) -> None:
        """Set Live Captions strip height in terminal rows (5–28)."""
        rows = max(5, min(28, int(rows)))
        self._captions_height = rows
        try:
            panel = self.query_one("#captions-panel", Vertical)
            panel.styles.height = rows
            panel.styles.min_height = rows
            panel.styles.max_height = rows
        except Exception:
            try:
                panel = self.query_one("#captions-panel")
                panel.styles.height = rows
                panel.styles.min_height = rows
                panel.styles.max_height = rows
            except Exception:
                pass
        try:
            hsash = self.query_one("#captions-hsash", CaptionsHSash)
            if rows == 8:
                hsash.update("═ ↕ captions ═")
            else:
                hsash.update(f"═ ↕ captions {rows} ═")
        except Exception:
            pass

    def captions_set_height_from_screen_y(self, screen_y: int) -> None:
        """
        Resize captions strip from bottom-edge drag.

        Pointer Y vs strip top → row height; middle log tabs fill the rest (1fr).
        """
        try:
            panel = self.query_one("#captions-panel")
            top = int(panel.region.y or 0)
        except Exception:
            return
        # Include the sash row under the pointer
        rows = int(screen_y) - top + 1
        self.captions_set_height(rows)

    def _update_trad_voz_expand_label(self) -> None:
        """Sync Expand/Restore on the VOZ header button (SOURCE_LANG)."""
        try:
            btn = self.query_one("#trad-btn-voz", Button)
        except Exception:
            return
        t = _footer_i18n()
        exp = self._trad_expand
        if exp == "voz":
            btn.label = t.get("restore", "Restore")
            try:
                btn.tooltip = t.get("restore_tip", "Restore LC | VOZ split")
            except Exception:
                pass
        else:
            btn.label = t.get("expand", "Expand")
            try:
                btn.tooltip = t.get("expand_tip", "Maximize VOZ panel (right)")
            except Exception:
                pass

    def trad_toggle_expand(self, side: str | None = None) -> None:
        """
        Expand LC or VOZ to full width; second press restores.

        Default side=None from VOZ button → toggle VOZ expand.
        """
        if side is None:
            side = "voz"
        side = "lc" if str(side or "").lower() == "lc" else "voz"
        if self._trad_expand == side:
            self._trad_expand = None
        else:
            self._trad_expand = side
            self.set_trad_focus(side)
        self._apply_trad_split_layout()
        self._refresh_log_width()

    def _apply_trad_split_layout(self) -> None:
        """Apply ratio or expand state to LC/VOZ columns + sash visibility."""
        try:
            lc_col = self.query_one("#trad-lc-col")
            voz_col = self.query_one("#trad-voz-col")
            sash = self.query_one("#trad-sash")
        except Exception:
            return
        exp = self._trad_expand

        if exp == "lc":
            lc_col.remove_class("-hidden")
            voz_col.add_class("-hidden")
            try:
                sash.display = False
            except Exception:
                pass
            lc_col.styles.width = "1fr"
            self._update_trad_voz_expand_label()
            return
        if exp == "voz":
            voz_col.remove_class("-hidden")
            lc_col.add_class("-hidden")
            try:
                sash.display = False
            except Exception:
                pass
            voz_col.styles.width = "1fr"
            self._update_trad_voz_expand_label()
            return

        lc_col.remove_class("-hidden")
        voz_col.remove_class("-hidden")
        try:
            sash.display = True
        except Exception:
            pass
        ratio = max(0.12, min(0.88, float(getattr(self, "_trad_ratio", 0.5) or 0.5)))
        # fr weights so sash (1 cell) does not fight % of full width
        left_w = max(12, min(88, int(round(ratio * 100))))
        right_w = max(12, 100 - left_w)
        lc_col.styles.width = f"{left_w}fr"
        voz_col.styles.width = f"{right_w}fr"
        self._update_trad_voz_expand_label()

    @on(Button.Pressed, "#trad-btn-voz")
    def _on_trad_btn_voz(self, event: Button.Pressed) -> None:
        """Expandir/Restaurar no canto superior direito do painel VOZ."""
        event.stop()
        self.trad_toggle_expand("voz")

    # ------------------------------------------------------------------ #
    # Logging (thread-safe via queue → UI timer)
    # ------------------------------------------------------------------ #
    def _sink_from_worker(self, kind: str, text: str, panel: str = "main") -> None:
        try:
            self._log_queue.put_nowait((kind, text, panel or "main"))
        except Exception:
            pass

    def _resolve_log_widget(self, panel: str = "main"):
        """Return SelectableRichLog for panel main|lc|app|news|cmds (fallback #log)."""
        p = str(panel or "main").lower()
        if p in ("cmds", "commands", "cmd", "help", "comandos"):
            log_id = "#log-cmds"
        elif p in ("news", "changelog", "novidades", "whatsnew"):
            log_id = "#log-news"
        elif p in ("app", "sistema", "system"):
            log_id = "#log-app"
        elif p in ("lc", "main-lc", "livecaptions", "captions", "caption"):
            log_id = "#log-lc"
        else:
            log_id = "#log"
        try:
            return self.query_one(log_id, SelectableRichLog)
        except Exception:
            try:
                return self.query_one(log_id, RichLog)
            except Exception:
                if log_id != "#log":
                    try:
                        return self.query_one("#log", SelectableRichLog)
                    except Exception:
                        try:
                            return self.query_one("#log", RichLog)
                        except Exception:
                            return None
                return None

    def _active_log_panel(self) -> str:
        """Active log for search/gg/copy: lc | main | app | news | cmds."""
        try:
            tabs = self.query_one("#log-tabs", TabbedContent)
            active = str(getattr(tabs, "active", "") or "")
            if active in ("tab-cmds", "log-cmds") or active.endswith("cmds"):
                return "cmds"
            if active in ("tab-news", "log-news") or active.endswith("news"):
                return "news"
            if active in ("tab-app", "log-app") or active.endswith("app"):
                return "app"
            if getattr(self, "_trad_focus", "voz") == "lc":
                return "lc"
        except Exception:
            pass
        return "main"

    def _active_log_widget(self):
        return self._resolve_log_widget(self._active_log_panel())

    def _trad_auto_scroll_enabled(self) -> bool:
        """F5 master switch: follow-to-bottom for Tradução LC + VOZ panes."""
        return bool(getattr(self, "_trad_follow_scroll", True))

    def _is_trad_panel(self, panel: str | None) -> bool:
        """True for Tradução sub-panes (LC left / VOZ right)."""
        panel_key = str(panel or "main").lower()
        return panel_key in (
            "main",
            "traducao",
            "tradução",
            "translation",
            "voz",
            "lc",
            "main-lc",
            "livecaptions",
            "captions",
            "caption",
        )

    def _set_log_auto_scroll(self, log, enabled: bool) -> None:
        if log is None:
            return
        try:
            log.auto_scroll = bool(enabled)
        except Exception:
            pass

    def _apply_trad_auto_scroll_flags(self) -> None:
        """Sync RichLog.auto_scroll on both Tradução panes with F5 state."""
        follow = self._trad_auto_scroll_enabled()
        for panel in ("main", "lc"):
            self._set_log_auto_scroll(self._resolve_log_widget(panel), follow)

    def _follow_log_bottom(self, log, *, force: bool = False) -> None:
        """
        Re-enable live follow and jump to the end of a log panel.

        For Tradução panes (LC/VOZ), respects F5 `_trad_follow_scroll` unless
        force=True (explicit user jump such as GG/gf). Search (/) and gg set
        auto_scroll=False; with F5 ON, new lines still stick to the bottom.
        """
        if log is None:
            return
        # Detect Tradução pane by id/role so F5 applies only there.
        is_trad = False
        try:
            lid = str(getattr(log, "id", "") or "")
            role = str(getattr(log, "pane_role", "") or "")
            is_trad = lid in ("log", "log-lc") or role in ("lc", "voz")
        except Exception:
            is_trad = False
        if is_trad and not force and not self._trad_auto_scroll_enabled():
            self._set_log_auto_scroll(log, False)
            return
        try:
            log.auto_scroll = True if (not is_trad or self._trad_auto_scroll_enabled()) else False
        except Exception:
            pass
        if is_trad and not self._trad_auto_scroll_enabled() and not force:
            return
        for kwargs in (
            {"animate": False, "immediate": True},
            {"animate": False},
            {},
        ):
            try:
                log.scroll_end(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                break
        try:
            y = int(getattr(log, "max_scroll_y", 0) or 0)
            log.scroll_to(0, y, animate=False)
        except Exception:
            pass
        # After an explicit force jump with F5 OFF, keep auto_scroll disabled.
        if is_trad and not self._trad_auto_scroll_enabled():
            self._set_log_auto_scroll(log, False)

    def post_log(self, kind: str, text: str, panel: str = "main") -> None:
        """Must run on the UI thread (or via _drain_log_queue). panel=main|lc|app."""
        log = self._resolve_log_widget(panel)
        if log is None:
            return
        is_trad = self._is_trad_panel(panel)
        if is_trad:
            # F5 OFF → keep viewport; RichLog.write must not auto scroll_end.
            self._set_log_auto_scroll(log, self._trad_auto_scroll_enabled())
        if text is None:
            return
        if text == "" or text.strip() == "":
            try:
                log.write("")
            except Exception:
                pass
            if is_trad:
                self._follow_log_bottom(log)
            return
        t = text.rstrip("\n")
        try:
            from rich.markup import escape

            safe = escape(t)
        except Exception:
            safe = t.replace("[", "\\[")
        try:
            if kind == "rich":
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
                log.write(safe)
        except Exception:
            try:
                log.write(t)
            except Exception:
                pass
        if is_trad:
            self._follow_log_bottom(log)

    def _drain_log_queue(self) -> None:
        for _ in range(200):
            try:
                item = self._log_queue.get_nowait()
            except queue.Empty:
                break
            if not item:
                continue
            if len(item) >= 3:
                kind, text, panel = item[0], item[1], item[2]
            else:
                kind, text = item[0], item[1]
                panel = "main"
            self.post_log(kind, text, panel=panel)

    def _drain_ui_actions(self) -> None:
        """Run UI-only actions posted from worker threads (never block workers)."""
        for _ in range(20):
            try:
                act = self._ui_action_q.get_nowait()
            except queue.Empty:
                break
            if act == "refresh_source_ui":
                try:
                    self.refresh_source_ui()
                except Exception:
                    pass
            elif act == "refocus_cmd":
                try:
                    self._refocus_cmd_if_idle()
                except Exception:
                    pass

    def _drain_pending(self) -> None:
        """Single interval: prioritize log drain (translations) over UI actions."""
        self._drain_log_queue()
        self._drain_caption_queue()
        self._drain_pipe_stage_queue()
        self._drain_ui_actions()

    def _modal_open(self) -> bool:
        """True when a ModalScreen is the active top screen."""
        try:
            return isinstance(self.screen, ModalScreen)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Pipeline activity bar (Mic → STT → Trad → TTS → Cable Out + LC)
    # ------------------------------------------------------------------ #
    _PIPE_ORDER = ("mic", "stt", "translate", "tts", "play")

    def _pipeline_stage_from_worker(self, stage: str, meta: dict | None = None) -> None:
        """Thread-safe: queue stage updates from ui.pipeline_stage / workers."""
        try:
            self._pipe_stage_q.put_nowait((str(stage or "idle"), dict(meta or {})))
        except Exception:
            pass

    def _drain_pipe_stage_queue(self) -> None:
        latest = None
        try:
            while True:
                latest = self._pipe_stage_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            stage, meta = latest
            self._apply_pipeline_stage(stage, meta)

    def _apply_pipeline_stage(self, stage: str, meta: dict | None = None) -> None:
        """UI thread: set VOZ pipe stage (or LC via source=lc)."""
        import time as _time

        meta = meta or {}
        stage = (stage or "idle").strip().lower()
        source = str(meta.get("source") or "voz").lower()

        if source == "lc" or stage in ("lc", "lc_idle", "lc_busy"):
            if stage in ("lc", "lc_busy") or meta.get("busy"):
                self._pipe_lc_busy = True
                self._pipe_lc_active = True
            elif stage in ("lc_idle", "idle"):
                self._pipe_lc_busy = False
            self._paint_pipe_bar()
            return

        # Normalize aliases
        if stage in ("listen", "listening", "vad", "hearing"):
            stage = "mic"
        elif stage in ("trad", "tr", "translating"):
            stage = "translate"
        elif stage in ("cable", "out", "playback", "playing"):
            stage = "play"
        elif stage in ("done", "ready_text", "complete"):
            stage = "idle"
        elif stage == "ready":
            # Audio will refine to play; treat as play for bar feedback
            stage = "play"

        if stage not in self._PIPE_ORDER and stage != "idle":
            stage = "idle"

        self._pipe_stage = stage
        self._pipe_stage_t = _time.monotonic()
        try:
            ch = meta.get("chunk")
            self._pipe_chunk = int(ch) if ch is not None else self._pipe_chunk
        except (TypeError, ValueError):
            pass
        self._paint_pipe_bar()

    def _sync_lc_pipe_from_captions(self) -> None:
        """Derive LC badge from caption panel state."""
        d = self._caption_data or {}
        status = str(d.get("status") or "idle").lower()
        paused = bool(d.get("paused"))
        live = (d.get("original_live") or "").strip()
        original = (d.get("original") or "").strip()
        translated = (d.get("translated") or "").strip()
        has_text = bool(live or original or translated)
        busy = (
            (not paused)
            and status in ("translating", "running", "starting")
            and has_text
        ) or (status == "translating")
        active = (
            status not in ("disabled", "error", "idle", "") or has_text
        ) and status != "disabled"
        # Show LC chip whenever captions service is live/usable
        if status in ("disabled",):
            active = False
            busy = False
        elif status in ("error",) and not has_text:
            active = False
            busy = False
        elif status in ("running", "translating", "starting", "paused"):
            active = True
        elif has_text:
            active = True
        else:
            active = False
        self._pipe_lc_active = bool(active)
        self._pipe_lc_busy = bool(busy and not paused)

    def _paint_pipe_bar(self, *, force: bool = False) -> None:
        """Render compact Mic→…→Out (+ LC) into #pipe-bar."""
        try:
            bar = self.query_one("#pipe-bar", Static)
        except Exception:
            return

        t = _footer_i18n()
        # Special modes
        if self._passthrough:
            markup = f"[bold #2d9a4e]● {t.get('pipe_bypass', 'BYPASS→Out')}[/]"
            self._update_pipe_widget(bar, markup, busy=True, lc=False, force=force)
            return
        if self._mic_muted and self._pipe_stage in ("idle", "mic"):
            markup = f"[dim]○ {t.get('pipe_muted', 'Mic muted')}[/]"
            if self._pipe_lc_active:
                lc_lab = (
                    t.get("pipe_lc_active", "LC●")
                    if self._pipe_lc_busy
                    else t.get("pipe_lc", "LC")
                )
                color = "#e0a0ff" if self._pipe_lc_busy else "#8866aa"
                markup += f" [dim]│[/] [{color}]{lc_lab}[/]"
            self._update_pipe_widget(
                bar, markup, busy=False, lc=self._pipe_lc_busy, force=force
            )
            return

        stage = self._pipe_stage if self._pipe_stage in self._PIPE_ORDER else "idle"
        # idle → highlight mic as "ready to listen" (soft)
        active_idx = self._PIPE_ORDER.index(stage) if stage in self._PIPE_ORDER else -1
        # Pulse glyph for the active step
        pulse_on = (self._frame_i % 2) == 0
        active_dot = "●" if pulse_on else "◉"
        done_dot = "●"
        todo_dot = "○"

        labels_idle = {
            "mic": t.get("pipe_mic", "Mic"),
            "stt": t.get("pipe_stt", "STT"),
            "translate": t.get("pipe_tr", "Trad"),
            "tts": t.get("pipe_tts", "TTS"),
            "play": t.get("pipe_out", "Out"),
        }
        labels_active = {
            "mic": t.get("pipe_mic_active", "Ouvindo"),
            "stt": t.get("pipe_stt_active", "STT…"),
            "translate": t.get("pipe_tr_active", "Traduz…"),
            "tts": t.get("pipe_tts_active", "TTS…"),
            "play": t.get("pipe_out_active", "Cable"),
        }
        # Colors: done green, active amber/cyan, todo dim
        parts: list[str] = []
        for i, key in enumerate(self._PIPE_ORDER):
            if active_idx < 0:
                # Idle listening: soft mic only
                if key == "mic":
                    if self._speaking:
                        lab = labels_active[key]
                        parts.append(f"[bold #4fc3f7]{active_dot}{lab}[/]")
                    else:
                        lab = labels_idle[key]
                        parts.append(f"[dim]{todo_dot}{lab}[/]")
                else:
                    parts.append(f"[dim]{todo_dot}{labels_idle[key]}[/]")
            elif i < active_idx:
                parts.append(f"[#5cbf6a]{done_dot}{labels_idle[key]}[/]")
            elif i == active_idx:
                lab = labels_active[key]
                if key == "mic":
                    color = "#4fc3f7"
                elif key == "stt":
                    color = "#ffd54f"
                elif key == "translate":
                    color = "#81d4fa"
                elif key == "tts":
                    color = "#ce93d8"
                else:  # play / cable
                    color = "#ffab40"
                parts.append(f"[bold {color}]{active_dot}{lab}[/]")
            else:
                parts.append(f"[dim]{todo_dot}{labels_idle[key]}[/]")

        sep = "[dim]›[/]"
        markup = sep.join(parts)

        if self._pipe_lc_active:
            if self._pipe_lc_busy:
                lc_pulse = "●" if pulse_on else "◉"
                markup += (
                    f" [dim]│[/] [bold #e040fb]{lc_pulse}{t.get('pipe_lc', 'LC')}[/]"
                )
            else:
                markup += f" [dim]│ {t.get('pipe_lc', 'LC')}[/]"

        busy = active_idx >= 0 or self._speaking or self._pipe_lc_busy
        self._update_pipe_widget(
            bar, markup, busy=busy, lc=self._pipe_lc_busy, force=force
        )

    def _update_pipe_widget(
        self,
        bar: Static,
        markup: str,
        *,
        busy: bool,
        lc: bool,
        force: bool = False,
    ) -> None:
        if not force and markup == self._pipe_last_markup:
            # Still refresh classes
            try:
                bar.set_class(busy, "-busy")
                bar.set_class(lc, "-lc")
            except Exception:
                pass
            return
        self._pipe_last_markup = markup
        try:
            bar.update(markup)
        except Exception:
            pass
        try:
            bar.set_class(busy, "-busy")
            bar.set_class(lc, "-lc")
        except Exception:
            try:
                if busy:
                    bar.add_class("-busy")
                else:
                    bar.remove_class("-busy")
                if lc:
                    bar.add_class("-lc")
                else:
                    bar.remove_class("-lc")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Fixed listen header (robot animation + source/target)
    # ------------------------------------------------------------------ #
    def set_speaking(self, speaking: bool) -> None:
        """
        Mic VAD: speech started/stopped.

        When speech starts, force the Tradução tab so Heard/Translated are
        visible even if the user was on Sistema / Novidades / Lista de comandos.
        Follow-to-bottom only when F5 auto-scroll is ON.
        """
        speaking = bool(speaking)
        started = speaking and not self._speaking
        stopped = (not speaking) and self._speaking
        self._speaking = speaking
        # Mirror VAD on the pipe bar (mic step); stt follows via listen_progress
        if started:
            self._apply_pipeline_stage("mic", {"source": "voz"})
        elif stopped and self._pipe_stage == "mic":
            # Speech ended — expect STT next (listen_progress also fires stt)
            self._apply_pipeline_stage("stt", {"source": "voz"})
        else:
            try:
                self._paint_pipe_bar()
            except Exception:
                pass
        if not started:
            return
        # UI thread only (call_from_thread from on_listening).
        try:
            if self._active_log_panel() != "main":
                self.focus_log_tab("main")
        except Exception:
            try:
                self.focus_log_tab("main")
            except Exception:
                pass
        # F5 OFF: do not yank VOZ to bottom when speech starts.
        if self._trad_auto_scroll_enabled():
            try:
                self._follow_log_bottom(self._resolve_log_widget("main"))
            except Exception:
                pass

    def set_sound_on(self, on: bool) -> None:
        self._sound_on = bool(on)

    def set_mic_muted(self, muted: bool, mic_name: str = "") -> None:
        """
        Sync UI with mic mute state.

        When muted: show a centered red modal (only [n] unmutes).
        When unmuted: dismiss that modal if open.
        Safe to call from a worker thread.
        """
        self._mic_muted = bool(muted)
        if mic_name:
            self._mic_mute_name = str(mic_name)
        try:
            if muted:
                self.call_from_thread(self._show_mic_mute_modal)
            else:
                self.call_from_thread(self._dismiss_mic_mute_modal)
        except Exception:
            try:
                if muted:
                    self._show_mic_mute_modal()
                else:
                    self._dismiss_mic_mute_modal()
            except Exception:
                pass

    def _mic_mute_i18n(self) -> dict[str, str]:
        """Title / body / hint for the mute modal (SOURCE_LANG)."""
        lang = _source_lang_code()
        if lang == "pt":
            return {
                "title": "MIC MUDO",
                "message": "Microfone mutado — escuta e tradução pausadas.",
                "message2": "",
                "hint": "[n]  desmutar o microfone - Cmd n",
            }
        if lang == "es":
            return {
                "title": "MIC MUTEADO",
                "message": "Micrófono silenciado — escucha y traducción en pausa.",
                "message2": "",
                "hint": "[n]  activar el micrófono - Cmd n",
            }
        return {
            "title": "MIC MUTED",
            "message": "Microphone muted — listening and translation paused.",
            "message2": "",
            "hint": "[n]  unmute microphone - Cmd n",
        }

    def _show_mic_mute_modal(self) -> None:
        """UI thread: push centered red mute dialog if not already open."""
        try:
            if isinstance(self.screen, MicMutedModal):
                return
        except Exception:
            pass
        pack = self._mic_mute_i18n()
        name = getattr(self, "_mic_mute_name", "") or ""
        try:
            if not name and hasattr(self.pipeline, "mic_endpoint_name"):
                name = self.pipeline.mic_endpoint_name() or ""
        except Exception:
            name = name or ""
        modal = MicMutedModal(
            title=pack["title"],
            mic_name=name,
            message=pack.get("message", ""),
            message2=pack.get("message2", ""),
            hint=pack["hint"],
        )
        try:
            self.push_screen(modal, self._on_mic_mute_modal_dismiss)
        except Exception:
            pass

    def _dismiss_mic_mute_modal(self) -> None:
        """UI thread: pop mute modal if it is the top screen."""
        try:
            if isinstance(self.screen, MicMutedModal):
                self.pop_screen()
        except Exception:
            pass

    def _on_mic_mute_modal_dismiss(self, result: str | None) -> None:
        """
        After [n] on the modal: force unmute + refresh header/log.

        If the mic was already unmuted another way, just clean UI state.
        """
        try:
            still_muted = bool(self.pipeline.is_mic_muted())
        except Exception:
            still_muted = True
        if still_muted:
            try:
                muted_now, os_ok, mic_name = self.pipeline.set_mic_muted(False)
            except Exception as exc:
                try:
                    self.post_log("error", f"[n] Unmute failed: {exc}")
                except Exception:
                    pass
                self._mic_muted = False
                return
            self._mic_muted = bool(muted_now)
            self._mic_mute_name = mic_name or ""
            if not muted_now:
                if os_ok:
                    self.post_log(
                        "success",
                        f"Mic LIVE (Windows): '{mic_name}'. "
                        f"Escuta ativa retomada. Pode falar.",
                    )
                else:
                    self.post_log(
                        "success",
                        f"Mic LIVE (app gate): '{mic_name}'. Escuta ativa retomada.",
                    )
                try:
                    self.post_log("raw", "")
                except Exception:
                    pass
        else:
            self._mic_muted = False
        try:
            self._tick_status()
        except Exception:
            pass
        try:
            self._refocus_cmd_if_idle()
        except Exception:
            pass

    def set_passthrough(self, active: bool) -> None:
        """UI cue: direct voice bypass ([b]) is active."""
        self._passthrough = bool(active)
        try:
            self._refresh_bypass_badge()
        except Exception:
            pass

    def _refresh_bypass_badge(self) -> None:
        """Update F2 badge: green = bypass ON, white = OFF (tab bar or footer)."""
        try:
            badge = self.query_one("#cmd-bypass", Static)
        except Exception:
            return
        t = _footer_i18n()
        on = bool(self._passthrough)
        label = t.get(
            "bypass_on_label" if on else "bypass_off_label",
            "(Your voice)" if on else "(Translated audio)",
        )
        tip = t.get(
            "bypass_tooltip_on" if on else "bypass_tooltip_off",
            "",
        )
        try:
            badge.update(label)
        except Exception:
            pass
        try:
            badge.set_class(on, "-on")
            badge.set_class(not on, "-off")
        except Exception:
            try:
                if on:
                    badge.remove_class("-off")
                    badge.add_class("-on")
                else:
                    badge.remove_class("-on")
                    badge.add_class("-off")
            except Exception:
                pass
        try:
            if tip:
                badge.tooltip = tip
        except Exception:
            pass
        self._bypass_badge_label = label

    def _refresh_scroll_follow_ui(self) -> None:
        """
        Update F5 badge + Footer binding label for Tradução auto-scroll state.

        Green chip + footer \"Auto↓ ON\" = follow; amber + \"Auto↓ OFF\" = locked.
        """
        follow = self._trad_auto_scroll_enabled()
        t = _footer_i18n()
        label = t.get(
            "scroll_on_label" if follow else "scroll_off_label",
            "F5 Auto↓ ON" if follow else "F5 Auto↓ OFF",
        )
        tip = t.get(
            "scroll_tooltip_on" if follow else "scroll_tooltip_off",
            "",
        )
        footer_desc = t.get(
            "scroll_footer_on" if follow else "scroll_footer_off",
            "Auto↓ ON" if follow else "Auto↓ OFF",
        )
        try:
            badge = self.query_one("#cmd-scroll", Static)
        except Exception:
            badge = None
        if badge is not None:
            try:
                badge.update(label)
            except Exception:
                pass
            try:
                badge.set_class(follow, "-on")
                badge.set_class(not follow, "-off")
            except Exception:
                try:
                    if follow:
                        badge.remove_class("-off")
                        badge.add_class("-on")
                    else:
                        badge.remove_class("-on")
                        badge.add_class("-off")
                except Exception:
                    pass
            try:
                if tip:
                    badge.tooltip = tip
            except Exception:
                pass
        # Footer key text (below command box) mirrors ON/OFF.
        # Replace Binding in place (frozen dataclass; App.bind would stack).
        try:
            from textual.binding import Binding

            bmap = getattr(self, "_bindings", None)
            key_map = getattr(bmap, "key_to_bindings", None) if bmap else None
            if isinstance(key_map, dict):
                old_list = list(key_map.get("f5") or [])
                new_list = []
                replaced = False
                for b in old_list:
                    act = str(getattr(b, "action", "") or "")
                    if "toggle_trad_auto_scroll" in act:
                        new_list.append(
                            Binding(
                                key=getattr(b, "key", "f5") or "f5",
                                action=act,
                                description=footer_desc,
                                show=True,
                                key_display=getattr(b, "key_display", None),
                                priority=True,
                                tooltip=tip or getattr(b, "tooltip", "") or "",
                                id=getattr(b, "id", None),
                                system=bool(getattr(b, "system", False)),
                                group=getattr(b, "group", None),
                            )
                        )
                        replaced = True
                    else:
                        new_list.append(b)
                if not replaced:
                    new_list.append(
                        Binding(
                            "f5",
                            "toggle_trad_auto_scroll",
                            footer_desc,
                            show=True,
                            priority=True,
                            tooltip=tip or "",
                        )
                    )
                key_map["f5"] = new_list
        except Exception:
            pass
        try:
            self.refresh_bindings()
        except Exception:
            pass
        # Keep pane flags in sync (write path also sets per call).
        try:
            self._apply_trad_auto_scroll_flags()
        except Exception:
            pass

    def _toggle_trad_auto_scroll_from_ui(self) -> None:
        """F5 / click #cmd-scroll — toggle Tradução follow-to-bottom. UI thread."""
        self._trad_follow_scroll = not self._trad_auto_scroll_enabled()
        follow = self._trad_follow_scroll
        try:
            self._refresh_scroll_follow_ui()
        except Exception:
            pass
        t = _footer_i18n()
        if follow:
            # Re-enabling: jump both panes to end so live follow is useful.
            for panel in ("main", "lc"):
                try:
                    self._follow_log_bottom(
                        self._resolve_log_widget(panel), force=True
                    )
                except Exception:
                    pass
            # force=True left auto_scroll True; re-apply F5 ON flags.
            try:
                self._apply_trad_auto_scroll_flags()
            except Exception:
                pass
            msg = t.get(
                "scroll_log_on",
                "[F5] Auto-scroll ON — Tradução LC+VOZ follow new lines.",
            )
            try:
                self.notify(msg, severity="information", timeout=3)
            except Exception:
                self.post_log("info", msg, panel="app")
        else:
            msg = t.get(
                "scroll_log_off",
                "[F5] Auto-scroll OFF — Tradução scroll locked.",
            )
            try:
                self.notify(msg, severity="warning", timeout=3)
            except Exception:
                self.post_log("warn", msg, panel="app")

    def action_toggle_trad_auto_scroll(self) -> None:
        """Footer/F5: toggle Tradução auto-scroll (same as click on F5 badge)."""
        self._toggle_trad_auto_scroll_from_ui()

    @on(events.Click, "#cmd-scroll")
    def on_scroll_badge_click(self, event: events.Click) -> None:
        """Toggle Tradução auto-scroll when the F5 badge is clicked."""
        try:
            event.stop()
        except Exception:
            pass
        self._toggle_trad_auto_scroll_from_ui()

    def _toggle_bypass_from_ui(self) -> None:
        """Click / F2 / [b] on #cmd-bypass — voice bypass toggle. UI thread."""
        try:
            active = bool(self.pipeline.toggle_voice_passthrough())
        except Exception as exc:
            try:
                self.notify(f"[b]/F2 Bypass: {exc}", severity="error", timeout=4)
            except Exception:
                self.post_log("error", f"[b]/F2 Bypass: {exc}")
            return
        self.set_passthrough(active)
        t = _footer_i18n()
        if active:
            self.post_log(
                "warn",
                "[b]/F2 BYPASS ON — "
                + t.get("bypass_on_label", "F2 Sua Voz")
                + " → saída direta (sem tradução).",
            )
        else:
            self.post_log(
                "success",
                "[b]/F2 BYPASS OFF — "
                + t.get("bypass_off_label", "F2 Audio Trad.")
                + " retomado.",
            )
            self.post_log("raw", "")  # blank line after bypass off
        try:
            self._tick_status()
        except Exception:
            pass

    def action_toggle_bypass(self) -> None:
        """Footer/F2: toggle voice bypass (same as click on white badge / [b])."""
        self._toggle_bypass_from_ui()

    @on(events.Click, "#cmd-bypass")
    def on_bypass_badge_click(self, event: events.Click) -> None:
        """Toggle bypass when the green/white badge is clicked."""
        try:
            event.stop()
        except Exception:
            pass
        self._toggle_bypass_from_ui()

    def refresh_source_ui(self) -> None:
        """Re-apply footer/placeholder for current SOURCE_LANG (after [g] swap)."""
        try:
            self._refresh_cmd_menu()
        except Exception:
            pass
        try:
            self._refresh_cmd_tts()
        except Exception:
            pass
        try:
            self._refresh_bypass_badge()
        except Exception:
            pass
        try:
            self._refresh_scroll_follow_ui()
        except Exception:
            pass
        try:
            self._refresh_tab_labels()
        except Exception:
            pass
        try:
            self._refresh_trad_chrome()
        except Exception:
            pass
        try:
            self._fill_news_tab()
        except Exception:
            pass
        try:
            self._fill_commands_tab()
        except Exception:
            pass
        try:
            self._tick_status()
        except Exception:
            pass

    def _set_tab_label(self, pane_id: str, label: str) -> None:
        """Set a TabPane label (Textual version-tolerant)."""
        try:
            pane = self.query_one(pane_id, TabPane)
            if hasattr(pane, "set_label"):
                pane.set_label(label)
            else:
                pane.label = label
        except Exception:
            pass

    def _refresh_tab_labels(self) -> None:
        """Update all tab titles for current SOURCE_LANG."""
        i18n = _footer_i18n()
        lang = _source_lang_code()
        self._set_tab_label("#tab-main", i18n.get("tab_traducao", "Translation"))
        self._set_tab_label("#tab-app", i18n.get("tab_sistema", "System"))
        self._set_tab_label("#tab-news", i18n.get("tab_news", "What's New"))
        self._set_tab_label(
            "#tab-cmds",
            i18n.get("tab_commands", command_help.tab_title(lang)),
        )

    def _refresh_trad_chrome(self) -> None:
        """Update LC/VOZ header labels + Expand button for SOURCE_LANG."""
        t = _footer_i18n()
        try:
            self.query_one("#trad-lbl-lc", Static).update(
                t.get("trad_lbl_lc", "LC in (LiveCaptions)")
            )
        except Exception:
            pass
        try:
            self.query_one("#trad-lbl-voz", Static).update(
                t.get("trad_lbl_voz", "VOICE mic + commands")
            )
        except Exception:
            pass
        try:
            self._update_trad_voz_expand_label()
        except Exception:
            pass

    def _fill_news_tab(self) -> None:
        """Load CHANGELOG.md into the Novidades tab (UI thread)."""
        try:
            log = self.query_one("#log-news", SelectableRichLog)
        except Exception:
            try:
                log = self.query_one("#log-news", RichLog)
            except Exception:
                return
        i18n = _footer_i18n()
        title = i18n.get("tab_news", "What's New")
        header = i18n.get("news_header", "Project changelog (CHANGELOG.md)")
        try:
            log.clear()
        except Exception:
            pass
        try:
            log.write(f"[bold cyan]{title}[/] — {header}")
            log.write("")
        except Exception:
            pass
        text, err = _load_changelog_text()
        if err or not text:
            try:
                log.write(
                    f"[yellow]{err or i18n.get('news_missing', 'CHANGELOG.md not found.')}[/]"
                )
            except Exception:
                pass
            return
        # Prefer Rich Markdown (headers, lists, bold from CHANGELOG.md)
        try:
            from rich.markdown import Markdown

            log.write(Markdown(text))
        except Exception:
            for line in text.splitlines():
                try:
                    # Escape Rich markup in raw changelog lines
                    safe = line.replace("[", "\\[")
                    log.write(safe)
                except Exception:
                    break
        # Changelog is read top-down; keep viewport at the start
        self._scroll_log_home(log)

    def _fill_commands_tab(self) -> None:
        """Fill the Command list tab with Markdown help (SOURCE_LANG)."""
        try:
            log = self.query_one("#log-cmds", SelectableRichLog)
        except Exception:
            try:
                log = self.query_one("#log-cmds", RichLog)
            except Exception:
                return
        lang = _source_lang_code()
        try:
            log.clear()
        except Exception:
            pass
        md = command_help.build_commands_markdown(lang)
        try:
            from rich.markdown import Markdown

            log.write(Markdown(md))
        except Exception:
            for line in md.splitlines():
                try:
                    log.write((line or "").replace("[", "\\["))
                except Exception:
                    break
        self._scroll_log_home(log)

    def _scroll_log_home(self, log) -> None:
        """Scroll a RichLog to the top; disable auto-scroll if possible."""
        try:
            log.auto_scroll = False
        except Exception:
            pass
        for kwargs in (
            {"animate": False, "immediate": True},
            {"animate": False},
            {},
        ):
            try:
                log.scroll_home(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                break

    def _clear_one_log(self, log_id: str, note: str) -> bool:
        """Clear a single RichLog by CSS id; write optional dim note. UI thread."""
        try:
            log = self.query_one(log_id, SelectableRichLog)
            log.clear()
            if note:
                log.write(note)
            return True
        except Exception:
            try:
                log = self.query_one(log_id, RichLog)
                log.clear()
                if note:
                    try:
                        log.write(note)
                    except Exception:
                        pass
                return True
            except Exception:
                return False

    def clear_log(self) -> None:
        """Clear LC + VOZ + Sistema logs (command [cls]). Must run on UI thread."""
        t = _footer_i18n()
        for log_id, key, fallback in (
            (
                "#log-lc",
                "cls_note_lc",
                "[dim]LC cleared[/]",
            ),
            (
                "#log",
                "cls_note_voz",
                "[dim]VOZ cleared[/]",
            ),
            (
                "#log-app",
                "cls_note_app",
                "[dim]System cleared[/]",
            ),
        ):
            self._clear_one_log(log_id, t.get(key, fallback))
        # Hits point at cleared buffers — drop search cursor + paint
        self._search_hits = []
        self._search_i = -1
        self._clear_all_search_highlights()

    def clear_log_side(self, side: int) -> bool:
        """
        Clear one Tradução column (command [cls1]/[cls2]). UI thread only.

        side 1 → left  = LiveCaptions (#log-lc)
        side 2 → right = VOZ mic + commands (#log)
        """
        t = _footer_i18n()
        if side == 1:
            ok = self._clear_one_log(
                "#log-lc",
                t.get("cls1_note", "[dim]LC (left) cleared[/]"),
            )
        elif side == 2:
            ok = self._clear_one_log(
                "#log",
                t.get("cls2_note", "[dim]VOZ (right) cleared[/]"),
            )
        else:
            return False
        if ok:
            # Search hits may reference the cleared pane — drop highlights
            self._search_hits = []
            self._search_i = -1
            self._clear_all_search_highlights()
        return ok

    def _log_widget(self):
        """Return the active (visible) scrollable log widget, or #log."""
        return self._active_log_widget() or self._resolve_log_widget("main")

    def _invalidate_search_if_stale(self, log) -> None:
        """Drop hit list if the active log no longer has the same hit count shape."""
        if not self._search_hits or not self._search_query:
            return
        try:
            n_lines = len(getattr(log, "lines", None) or []) or len(
                getattr(log, "_plain_lines", None) or []
            )
        except Exception:
            n_lines = 0
        if n_lines <= 0:
            self._search_hits = []
            self._search_i = -1
            self._clear_all_search_highlights()
            return
        # Drop out-of-range hits (log truncated by max_lines / clear)
        if any(y < 0 or y >= n_lines for y in self._search_hits):
            self._search_hits = []
            self._search_i = -1
            self._clear_all_search_highlights()

    def _clear_all_search_highlights(self) -> None:
        """Remove /search paint from every log tab / Tradução pane."""
        for lid in ("#log", "#log-lc", "#log-app", "#log-news", "#log-cmds"):
            try:
                w = self.query_one(lid, SelectableRichLog)
                w.clear_search_highlight()
            except Exception:
                pass

    def _apply_search_highlight(
        self, log, query: str, hits: list[int], current_y: int | None
    ) -> None:
        """Paint hits on `log`; clear highlight on other tabs."""
        for lid in ("#log", "#log-lc", "#log-app", "#log-news", "#log-cmds"):
            try:
                w = self.query_one(lid, SelectableRichLog)
            except Exception:
                continue
            if w is log and query and hits:
                try:
                    w.set_search_highlight(query, hits, current_y)
                except Exception:
                    pass
            else:
                try:
                    w.clear_search_highlight()
                except Exception:
                    pass

    def _run_search_on_active(self, query: str, *, start_i: int = 0) -> None:
        """Find query on active tab, store hits, jump to start_i (wrapped)."""
        t = _footer_i18n()
        log = self._log_widget()
        if log is None:
            try:
                self.notify(t.get("search_empty_log", "Empty log."), severity="warning")
            except Exception:
                pass
            return
        query = (query or "").strip()
        if not query:
            try:
                self.notify(
                    t.get("search_help", "Use /text · /n · /p"),
                    severity="information",
                )
            except Exception:
                self.post_log("info", t.get("search_help", "Use /text · /n · /p"))
            return

        hits: list[int] = []
        if hasattr(log, "find_match_ys"):
            try:
                hits = list(log.find_match_ys(query) or [])
            except Exception:
                hits = []
        if not hits:
            # Fallback scan plain text if method missing
            try:
                plain = log.get_plain_text() if hasattr(log, "get_plain_text") else ""
                q = query.casefold()
                for y, line in enumerate((plain or "").splitlines()):
                    if q in line.casefold():
                        hits.append(y)
            except Exception:
                hits = []

        self._search_query = query
        self._search_hits = hits
        self._search_panel = self._active_log_panel()
        if not hits:
            self._search_i = -1
            self._apply_search_highlight(log, query, [], None)
            msg = t.get("search_none", 'No matches: "{q}"').format(q=query)
            try:
                self.notify(msg, severity="warning", timeout=3)
            except Exception:
                self.post_log("warn", msg)
            return

        n = len(hits)
        i = int(start_i) % n
        self._search_i = i
        y = hits[i]
        self._apply_search_highlight(log, query, hits, y)
        if hasattr(log, "scroll_to_content_y"):
            log.scroll_to_content_y(y)
        else:
            try:
                log.auto_scroll = False
                log.scroll_to(0, max(0, y), animate=False)
            except Exception:
                pass
        msg = t.get("search_hit", 'Search: "{q}" — {i}/{n}').format(
            q=query, i=i + 1, n=n
        )
        try:
            self.notify(msg, severity="information", timeout=2)
        except Exception:
            self.post_log("info", msg)

    def _handle_log_search(self, value: str) -> None:
        """
        Vim-style log search on the active tab (UI thread).

          /text   new search
          /n      next match (wrap)
          /p      previous match (wrap)
          /       repeat last query on active tab
        """
        t = _footer_i18n()
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
            query = raw[1:]  # after leading /

        if action == "new":
            if not (query or "").strip():
                try:
                    self.notify(
                        t.get("search_help", "Use /text · /n · /p"),
                        severity="information",
                    )
                except Exception:
                    self.post_log("info", t.get("search_help", "Use /text · /n · /p"))
                return
            self._run_search_on_active(query, start_i=0)
            return

        # next / prev / repeat need an existing query
        if not (query or "").strip():
            try:
                self.notify(
                    t.get(
                        "search_no_active",
                        "No active search. Use /text first.",
                    ),
                    severity="warning",
                    timeout=3,
                )
            except Exception:
                self.post_log("warn", t.get("search_no_active", "No active search."))
            return

        panel_now = self._active_log_panel()
        log = self._log_widget()
        # Tab changed, empty hits, or explicit repeat → rebuild on current tab
        need_refresh = (
            action == "repeat"
            or not self._search_hits
            or panel_now != self._search_panel
        )
        if log is not None and not need_refresh:
            self._invalidate_search_if_stale(log)
            if not self._search_hits:
                need_refresh = True

        if need_refresh:
            # prev after rebuild → last hit (-1 % n == n-1); else first
            start_i = -1 if action == "prev" else 0
            self._run_search_on_active(query, start_i=start_i)
            return

        n = len(self._search_hits)
        if n <= 0:
            self._run_search_on_active(query, start_i=0)
            return
        if action == "next":
            i = (int(self._search_i) + 1) % n
        elif action == "prev":
            i = (int(self._search_i) - 1) % n
        else:
            i = max(0, int(self._search_i)) % n
        self._search_i = i
        y = self._search_hits[i]
        if log is not None:
            self._apply_search_highlight(log, query, self._search_hits, y)
            if hasattr(log, "scroll_to_content_y"):
                log.scroll_to_content_y(y)
        msg = t.get("search_hit", 'Search: "{q}" — {i}/{n}').format(
            q=query, i=i + 1, n=n
        )
        try:
            self.notify(msg, severity="information", timeout=2)
        except Exception:
            self.post_log("info", msg)

    def scroll_log_top(self) -> None:
        """
        [gg]/[gt] Go top — jump to start of the active log tab.
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
        # Prefer immediate scroll when Textual supports it (avoids animation race).
        for kwargs in (
            {"animate": False, "immediate": True},
            {"animate": False},
            {},
        ):
            try:
                log.scroll_home(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                break
        try:
            log.scroll_to(0, 0, animate=False)
        except Exception:
            try:
                log.scroll_y = 0
            except Exception:
                pass
        try:
            log.refresh(layout=True)
        except Exception:
            try:
                log.refresh()
            except Exception:
                pass

    def scroll_log_footer(self) -> None:
        """
        [GG]/[gf] Go bottom — jump to end of the active log tab.
        Explicit user jump (always scrolls once). Tradução panes leave
        auto_scroll according to F5 state (do not re-enable if F5 OFF).
        Must run on UI thread.
        """
        log = self._log_widget()
        if log is None:
            return
        panel = self._active_log_panel()
        is_trad = self._is_trad_panel(panel)
        # Non-trad: keep old behavior (follow on). Trad: respect F5.
        want_follow = True if not is_trad else self._trad_auto_scroll_enabled()
        try:
            log.auto_scroll = bool(want_follow)
        except Exception:
            pass
        for kwargs in (
            {"animate": False, "immediate": True},
            {"animate": False},
            {},
        ):
            try:
                log.scroll_end(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                break
        try:
            y = int(getattr(log, "max_scroll_y", 0) or 0)
            log.scroll_to(0, y, animate=False)
        except Exception:
            try:
                log.scroll_y = int(getattr(log, "max_scroll_y", 0) or 0)
            except Exception:
                pass
        # Explicit jump with F5 OFF: re-lock so next write doesn't stick.
        if is_trad and not want_follow:
            self._set_log_auto_scroll(log, False)
        try:
            log.refresh(layout=True)
        except Exception:
            try:
                log.refresh()
            except Exception:
                pass

    def action_toggle_log_tab(self) -> None:
        """F3: cycle Tradução → Sistema → Novidades → Lista de comandos → …"""
        try:
            tabs = self.query_one("#log-tabs", TabbedContent)
            order = ("tab-main", "tab-app", "tab-news", "tab-cmds")
            cur = str(getattr(tabs, "active", "") or "tab-main")
            try:
                i = order.index(cur)
            except ValueError:
                # Normalize partial ids (e.g. ends with app/news/cmds)
                if cur.endswith("cmds"):
                    i = 3
                elif cur.endswith("news"):
                    i = 2
                elif cur.endswith("app"):
                    i = 1
                else:
                    i = 0
            tabs.active = order[(i + 1) % len(order)]
            # Keep command input focused after tab flip
            try:
                self.query_one("#cmd", Input).focus()
            except Exception:
                pass
        except Exception as exc:
            try:
                self.notify(f"Aba de log: {exc}", severity="warning", timeout=2)
            except Exception:
                pass

    def focus_log_tab(self, panel: str = "main") -> None:
        """Show Tradução / Sistema / Novidades / Command list. UI thread only."""
        try:
            tabs = self.query_one("#log-tabs", TabbedContent)
            p = str(panel or "main").lower()
            if p in ("cmds", "commands", "cmd", "help", "comandos"):
                want = "tab-cmds"
            elif p in ("news", "changelog", "novidades", "whatsnew"):
                want = "tab-news"
            elif p in ("app", "sistema", "system"):
                want = "tab-app"
            else:
                want = "tab-main"
            tabs.active = want
        except Exception:
            pass

    def action_toggle_compact_ui(self) -> None:
        """F4 / command [u]: toggle compact TUI (hide menu, shrink window)."""
        self.toggle_compact_ui()

    def toggle_compact_ui(self) -> None:
        """Toggle compact UI mode (must run on UI thread)."""
        self.set_compact_ui(not bool(getattr(self, "_compact_ui", False)))

    def set_compact_ui(self, compact: bool) -> None:
        """
        Compact mode: hide #hint menu strip, shrink #bottom to the command row,
        and safely shrink the host window height (CSI + MoveWindow only — no
        console buffer APIs).
        """
        compact = bool(compact)
        was = bool(getattr(self, "_compact_ui", False))
        self._compact_ui = compact
        try:
            bottom = self.query_one("#bottom")
            bottom.set_class(compact, "-compact")
            # CSS drives heights; clear leftover inline heights from old builds
            try:
                bottom.styles.height = None
            except Exception:
                pass
        except Exception:
            pass
        try:
            hint = self.query_one("#hint")
            if compact:
                hint.display = False
            else:
                hint.display = True
                try:
                    hint.styles.height = None
                except Exception:
                    pass
        except Exception:
            pass

        # --- Safe host-window resize (no SetConsoleScreenBufferSize) ---
        win_ok = False
        try:
            if compact and not was:
                snap = _snapshot_window_geom()
                self._saved_window_geom = snap
                cols = int(snap.get("cols") or 120)
                rows = int(snap.get("rows") or 40)
                # Hide ~6 menu rows + a little chrome; keep log + cmd usable
                compact_rows = max(16, rows - 7)
                win_ok = _safe_resize_host_window(cols, compact_rows)
            elif not compact and was:
                snap = getattr(self, "_saved_window_geom", None) or {}
                cols = int(snap.get("cols") or 120)
                rows = int(snap.get("rows") or 40)
                win_ok = _safe_resize_host_window(cols, rows, restore=snap)
                self._saved_window_geom = None
        except Exception:
            win_ok = False

        # Let Textual re-layout after the host fires resize (async on WT)
        def _after_resize() -> None:
            try:
                self.refresh(layout=True)
            except Exception:
                try:
                    self.refresh()
                except Exception:
                    pass
            try:
                self.query_one("#cmd", Input).focus()
            except Exception:
                pass

        try:
            self.set_timer(0.12, _after_resize)
        except Exception:
            _after_resize()

        try:
            if compact:
                extra = (
                    " Janela reduzida."
                    if win_ok
                    else " (Se a janela não encolher: use Windows Terminal e "
                    "permita resize por app, ou arraste a borda.)"
                )
                self.post_log(
                    "info",
                    "UI compacta: menu oculto; comando visível."
                    f"{extra} [u]/F4 restaura.",
                )
            else:
                self.post_log(
                    "info",
                    "UI completa: menu visível"
                    + (" · janela restaurada." if win_ok else "."),
                )
        except Exception:
            pass
        if not compact:
            try:
                self._refresh_cmd_menu()
            except Exception:
                pass

    def _refresh_cmd_menu(self) -> None:
        """
        Footer command menu using the full terminal width.

        Packs items left-to-right into rows (no fixed 14-col cells that truncate
        labels). Groups wrap to extra lines; label only on the first row of each
        group. Labels follow SOURCE_LANG (startup + after [g] swap).
        """
        try:
            hint = self.query_one("#hint", Static)
        except Exception:
            return
        # Skip rebuild while compact (menu hidden) — keeps UI tick light
        if getattr(self, "_compact_ui", False):
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

        # Full width of the hint strip (prefer live size, else app/terminal)
        try:
            avail = int(getattr(hint.size, "width", 0) or 0)
        except Exception:
            avail = 0
        if avail < 48:
            try:
                avail = int(getattr(self.size, "width", 0) or 0) - 2
            except Exception:
                avail = 0
        if avail < 48:
            avail = int(
                getattr(self, "_cached_log_width", 0) or 0
            ) or _terminal_log_width(100)
        avail = max(48, avail - 2)  # CSS padding 0 1

        # Group label column — wide enough for "Frase"/"Sentence"/"Audio"/…
        labels = [
            t.get("sentence", "Sentence"),
            t.get("audio", "Audio"),
            t.get("idiom", "Idiom"),
        ]
        lw = max(8, min(12, max(len(s or "") for s in labels) + 1))
        gap = "  "  # space between command cells (readable, not cramped)
        body_budget = max(24, avail - lw)

        def esc_cmd(s: str) -> str:
            """Escape brackets for Rich markup; keep full label (no …)."""
            return (s or "").replace("[", "\\[")

        def lab_plain(s: str) -> str:
            return (s or "")[:lw].ljust(lw)

        def pack_row_cells(items: list[str], budget: int) -> list[list[str]]:
            """Greedy pack full-text items into rows that fit `budget` columns."""
            rows: list[list[str]] = []
            cur: list[str] = []
            cur_len = 0
            gap_len = len(gap)
            for it in items:
                text = (it or "").strip()
                if not text:
                    continue
                # Single item longer than budget → own row (still no mid-label …)
                need = len(text) if not cur else gap_len + len(text)
                if cur and cur_len + need > budget:
                    rows.append(cur)
                    cur = [text]
                    cur_len = len(text)
                else:
                    if cur:
                        cur_len += gap_len
                    cur.append(text)
                    cur_len += len(text)
            if cur:
                rows.append(cur)
            return rows or [[]]

        def group_rows(label: str, items: list[str]) -> list[str]:
            """Magenta group label on first row; continuation rows indent."""
            packed = pack_row_cells(items, body_budget)
            rows_out: list[str] = []
            for i, chunk in enumerate(packed):
                cells = gap.join(esc_cmd(it) for it in chunk)
                if i == 0:
                    rows_out.append(f"[bold magenta]{lab_plain(label)}[/]{cells}")
                else:
                    rows_out.append(f"{lab_plain('')}{cells}")
            return rows_out

        lines: list[str] = []
        lines.extend(
            group_rows(
                t["sentence"],
                [
                    f"[e] {t['edit']}",
                    f"[eN] {t['edit_n']}",
                    f"[enew] {t.get('enew', 'New text')}",
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
                    f"[cls1] {t.get('cls1', 'Clr LC')}",
                    f"[cls2] {t.get('cls2', 'Clr VOZ')}",
                    f"[gg/gt] {t.get('go_top', 'Go top')}",
                    f"[GG/gf] {t.get('go_footer', 'Go foot')}",
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
                    f"[rs] {t.get('replay_src', 'Replay Heard')}",
                    f"[rsN] {t.get('replay_src_n', 'Heard N')}",
                    f"[s] {t['snd']} {sound}",
                    f"[n] {t['mic']} {mic}",
                    f"[b] {t.get('bypass', 'Bypass')}",
                    f"[x] {t['stop']}",
                    f"[a] {t['path']}",
                    f"[aN] {t['path_n']}",
                    f"[p] {t['folder']}",
                    f"[ld] {t.get('list_dev', 'Devices')}",
                    f"[lav] {t.get('list_voices', 'Voices')}",
                    f"[lv] {t.get('list_voices_f', 'Voices filter')}",
                    f"[ctts] {t.get('ctts', 'Chg TTS')}",
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
                    f"[u] {t.get('compact', 'Compact')}",
                    f"[q] {t['quit']}",
                ],
            )
        )
        # #hint is 6 rows (CSS): ~5 menu lines + 1 blank before the command box.
        while lines and not (lines[-1] or "").strip():
            lines.pop()
        max_lines = 5
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        # Exactly one empty line after the last menu group (visual gap only).
        lines.append("")

        hint.update("\n".join(lines))

        # Command field placeholder follows SOURCE_LANG (unless waiting a prompt)
        if not self._prompt_waiting.is_set():
            self._set_placeholder(t["placeholder"])
        # TTS badge only when voice/label may have changed (not every menu tick)
        self._refresh_cmd_tts()

    def _refresh_cmd_tts(self) -> None:
        """Update right-side TTS_VOICE badge next to the command input."""
        try:
            badge = self.query_one("#cmd-tts", Static)
        except Exception:
            return
        try:
            import config as cfg

            voice = (getattr(cfg, "TTS_VOICE", "") or "").strip() or "?"
        except Exception:
            voice = "?"
        ft = _footer_i18n()
        prefix = ft.get("cmd_tts", "TTS")
        text = f"{prefix} {voice}"
        if len(text) > 40:
            text = text[:37] + "…"
        if getattr(self, "_cmd_tts_label", None) == text:
            return
        self._cmd_tts_label = text
        try:
            badge.update(text)
            try:
                badge.tooltip = (
                    f"TTS_VOICE atual\nTrocar: ctts <nome>\nLista: lav / lv\n{voice}"
                )
            except Exception:
                pass
        except Exception:
            pass

    def _refocus_cmd(self) -> None:
        """UI-thread: restore focus to the command field."""
        try:
            self.query_one("#cmd", Input).focus()
        except Exception:
            pass

    def _refocus_cmd_if_idle(self) -> None:
        """UI-thread: focus #cmd only when no modal is open."""
        if self._modal_open():
            return
        self._refocus_cmd()

    def request_refresh_source_ui(self) -> None:
        """Thread-safe non-blocking menu/TTS-badge refresh (after [ctts]/[g]/[t])."""
        try:
            self._ui_action_q.put_nowait("refresh_source_ui")
        except Exception:
            pass

    def _tick_status(self) -> None:
        """Refresh fixed top header: robot/mic + pair + audio + listen status.

        Keep this hot path cheap: do NOT refresh TTS badge / menu here
        (that starved log drain and made translations feel laggy).
        """
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
        try:
            if hasattr(self.pipeline, "is_passthrough_active"):
                self._passthrough = bool(self.pipeline.is_passthrough_active())
        except Exception:
            pass

        # Pipe bar: stale-stage timeout + pulse animation
        try:
            import time as _time

            now = _time.monotonic()
            age = now - float(self._pipe_stage_t or 0.0)
            # After play/idle-ish stages settle, return to listening Mic
            if self._pipe_stage == "play" and age > 2.5:
                if not self._speaking:
                    self._pipe_stage = "idle"
                    self._pipe_stage_t = now
            elif self._pipe_stage in ("stt", "translate", "tts") and age > 12.0:
                # Safety: stuck stage (failed chunk) → idle
                if not self._speaking:
                    self._pipe_stage = "idle"
                    self._pipe_stage_t = now
            self._sync_lc_pipe_from_captions()
            self._paint_pipe_bar()
        except Exception:
            pass

        _src_u, _tgt_u, src_n, tgt_n, pair_short, pair_long = _lang_pair_parts()
        # g/t flanking the pair — PT shown as BR (display only); labels i18n
        ft = _footer_i18n()
        src_d = _display_lang_code(_src_u)
        tgt_d = _display_lang_code(_tgt_u)
        g_lab, t_lab = ft["g_swap"], ft["t_target"]
        lang_block_short = f"{g_lab} {src_d} → {tgt_d} {t_lab}"
        lang_block_long = f"{g_lab} {src_d} ({src_n}) → {tgt_d} ({tgt_n}) {t_lab}"
        # Subtitle only when the pair string changes (Header updates are costly)
        try:
            new_sub = f"{lang_block_short}  ·  ouvir {src_n} → falar {tgt_n}"
            if getattr(self, "_last_sub_title", None) != new_sub:
                self._last_sub_title = new_sub
                self.sub_title = new_sub
        except Exception:
            pass

        header.set_class(self._sound_on and not self._mic_muted, "sound-on")
        header.set_class(self._mic_muted, "mic-muted")

        if self._passthrough:
            by_line = (
                f"🎙️  BYPASS [b]   {lang_block_short}   |  "
                f"voz direta → CABLE (sem tradução)  |  [b] sair"
            )
            if getattr(self, "_last_header_line", None) != by_line:
                self._last_header_line = by_line
                header.update(by_line)
            return

        if self._mic_muted:
            muted_line = f"🔇  MIC MUTED   {lang_block_short}   |  escuta pausada  |  [n] reativar"
            if getattr(self, "_last_header_line", None) != muted_line:
                self._last_header_line = muted_line
                header.update(muted_line)
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
        # Always update when speaking (frame animation); idle can skip duplicates
        if self._speaking or getattr(self, "_last_header_line", None) != line:
            self._last_header_line = line
            header.update(line)

    # ------------------------------------------------------------------ #
    # Prompt / command input
    # ------------------------------------------------------------------ #
    def _wait_for_prompt_line(self) -> str:
        """Called from worker thread when code does sys.stdin.readline()."""
        self._prompt_waiting.set()
        prefill = getattr(self, "_prompt_prefill", "") or ""
        try:
            self.call_from_thread(
                self._arm_prompt_ui,
                self._prompt_label or _footer_i18n()["prompt_placeholder"],
                prefill,
            )
        except Exception:
            pass
        try:
            line = self._prompt_q.get()
        finally:
            self._prompt_waiting.clear()
            self._prompt_prefill = ""
            try:
                self.call_from_thread(self._disarm_prompt_ui)
            except Exception:
                pass
        return line if line.endswith("\n") else line + "\n"

    def _set_placeholder(self, text: str) -> None:
        try:
            self.query_one("#cmd", Input).placeholder = text
        except Exception:
            pass

    def _arm_prompt_ui(self, placeholder: str, prefill: str = "") -> None:
        """UI-thread: placeholder + optional prefill in #cmd for stdin prompts."""
        try:
            inp = self.query_one("#cmd", Input)
        except Exception:
            return
        try:
            if placeholder:
                inp.placeholder = placeholder
        except Exception:
            pass
        text = (
            prefill
            if prefill is not None
            else getattr(self, "_prompt_prefill", "") or ""
        )
        try:
            inp.value = text
            inp.cursor_position = len(text or "")
            inp.focus()
        except Exception:
            pass

    def _disarm_prompt_ui(self) -> None:
        """UI-thread: restore command placeholder after a prompt ends."""
        self._prompt_prefill = ""
        try:
            self._set_placeholder(_footer_i18n()["placeholder"])
        except Exception:
            pass

    def provide_prompt_line(self, line: str) -> None:
        self._prompt_q.put(line)

    def set_prompt_prefill(self, text: str) -> None:
        """
        Prefill #cmd for the next / current stdin prompt (edit sentence, etc.).

        Safe from worker via call_from_thread; also stores on the app so
        _wait_for_prompt_line can apply even if this runs slightly early.
        """
        self._prompt_prefill = text or ""
        try:
            inp = self.query_one("#cmd", Input)
            inp.value = self._prompt_prefill
            inp.cursor_position = len(inp.value or "")
            if self._prompt_waiting.is_set():
                inp.focus()
        except Exception:
            pass

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

    @staticmethod
    def _resolve_key_character(event: events.Key) -> str | None:
        """
        Printable char for a Key event, with fallbacks for terminals/layouts
        that send key *names* without `character` (common for `/` on
        Windows Terminal + ABNT2/WSL: name=slash, character=None).
        """
        ch = event.character
        if ch is not None and ch != "":
            return ch
        name = (event.name or "").lower()
        key = (getattr(event, "key", None) or "").lower()
        token = name or key
        # Strip modifiers if present (e.g. rare "shift+slash")
        if "+" in token:
            token = token.rsplit("+", 1)[-1]
        # Textual / term key identifiers → character
        fallback = {
            "slash": "/",
            "solidus": "/",
            "numpad_divide": "/",
            "divide": "/",
            "backslash": "\\",
            "bar": "|",
            "pipe": "|",
            "minus": "-",
            "numpad_minus": "-",
            "plus": "+",
            "numpad_plus": "+",
            "equals": "=",
            "underscore": "_",
            "period": ".",
            "full_stop": ".",
            "comma": ",",
            "semicolon": ";",
            "colon": ":",
            "apostrophe": "'",
            "quote": "'",
            "quotation_mark": '"',
            "grave_accent": "`",
            "asciitilde": "~",
            "left_square_bracket": "[",
            "right_square_bracket": "]",
            "left_curly_bracket": "{",
            "right_curly_bracket": "}",
            "left_parenthesis": "(",
            "right_parenthesis": ")",
            "asterisk": "*",
            "numpad_multiply": "*",
            "number_sign": "#",
            "at": "@",
            "percent_sign": "%",
            "ampersand": "&",
            "dollar_sign": "$",
            "exclamation_mark": "!",
            "question_mark": "?",
        }
        return fallback.get(token)

    def _insert_cmd_char(self, ch: str) -> bool:
        """Insert one character into #cmd at the cursor. Returns True on success."""
        if not ch:
            return False
        if self._prompt_force_upper and ch.isalpha():
            ch = ch.upper()
        inp = self._focus_cmd()
        if inp is None:
            return False
        # Typing resets erase-hold acceleration
        self._cmd_erase_key = None
        self._cmd_erase_streak = 0
        try:
            val = inp.value or ""
            pos = int(getattr(inp, "cursor_position", len(val)) or len(val))
            pos = max(0, min(pos, len(val)))
            inp.value = val[:pos] + ch + val[pos:]
            inp.cursor_position = pos + 1
            return True
        except Exception:
            try:
                inp.value = (inp.value or "") + ch
                return True
            except Exception:
                return False

    @staticmethod
    def _word_boundary_left(text: str, pos: int) -> int:
        """
        Index of the start of the word (or whitespace run) left of cursor.

        Skips trailing spaces first, then a run of word or non-word chars
        (editor-style Ctrl+Backspace).
        """
        if pos <= 0 or not text:
            return 0
        i = min(pos, len(text)) - 1
        # Consume spaces immediately left of cursor
        while i >= 0 and text[i].isspace():
            i -= 1
        if i < 0:
            return 0
        if text[i].isalnum() or text[i] in ("_", "-"):
            while i >= 0 and (text[i].isalnum() or text[i] in ("_", "-")):
                i -= 1
        else:
            # punctuation / symbols as one "word"
            while (
                i >= 0
                and not text[i].isspace()
                and not (text[i].isalnum() or text[i] in ("_", "-"))
            ):
                i -= 1
        return i + 1

    @staticmethod
    def _word_boundary_right(text: str, pos: int) -> int:
        """Index just past the word (or whitespace run) right of cursor."""
        if not text or pos >= len(text):
            return len(text or "")
        i = max(0, pos)
        n = len(text)
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return n
        if text[i].isalnum() or text[i] in ("_", "-"):
            while i < n and (text[i].isalnum() or text[i] in ("_", "-")):
                i += 1
        else:
            while (
                i < n
                and not text[i].isspace()
                and not (text[i].isalnum() or text[i] in ("_", "-"))
            ):
                i += 1
        return i

    def _cmd_erase_use_word(self, key: str) -> bool:
        """
        True when the user is holding Backspace/Delete (key-repeat streak).

        First few repeats delete one character; continued hold switches to
        whole-word erase for faster editing.
        """
        now = time.monotonic()
        gap = now - float(self._cmd_erase_last_t or 0.0)
        # New press sequence if key changed or gap > key-repeat window
        if key != self._cmd_erase_key or gap > 0.28:
            self._cmd_erase_streak = 1
        else:
            self._cmd_erase_streak = int(self._cmd_erase_streak) + 1
        self._cmd_erase_key = key
        self._cmd_erase_last_t = now
        # After 3 rapid char-deletes, each further repeat removes a word
        return self._cmd_erase_streak >= 4

    def _cmd_delete_left(self, *, word: bool = False) -> bool:
        """Delete one char or one word to the left of the cursor in #cmd."""
        inp = self._focus_cmd()
        if inp is None:
            return False
        try:
            val = inp.value or ""
            pos = int(getattr(inp, "cursor_position", len(val)) or len(val))
            pos = max(0, min(pos, len(val)))
            if pos <= 0:
                return True
            if word:
                new_pos = self._word_boundary_left(val, pos)
            else:
                new_pos = pos - 1
            inp.value = val[:new_pos] + val[pos:]
            inp.cursor_position = new_pos
            return True
        except Exception:
            return False

    def _cmd_delete_right(self, *, word: bool = False) -> bool:
        """Delete one char or one word to the right of the cursor in #cmd."""
        inp = self._focus_cmd()
        if inp is None:
            return False
        try:
            val = inp.value or ""
            pos = int(getattr(inp, "cursor_position", 0) or 0)
            pos = max(0, min(pos, len(val)))
            if pos >= len(val):
                return True
            if word:
                end = self._word_boundary_right(val, pos)
            else:
                end = pos + 1
            inp.value = val[:pos] + val[end:]
            inp.cursor_position = pos
            return True
        except Exception:
            return False

    def _handle_cmd_erase_keys(self, event: events.Key, key_name: str) -> bool:
        """
        Handle Backspace/Delete (+ modifiers) on #cmd.

        - Backspace: char left; hold (key-repeat) → word left
        - Delete: char right; hold → word right
        - Ctrl+Backspace / Ctrl+W / Alt+Backspace: always word left
        - Ctrl+Delete: always word right

        Returns True if the event was handled.
        """
        kn = (key_name or "").lower()
        # Always word-left shortcuts (Ctrl+Backspace / Ctrl+W / Alt+Backspace)
        if kn in ("ctrl+backspace", "ctrl+w", "alt+backspace", "control+backspace") or (
            kn.endswith("+backspace")
            and ("ctrl" in kn or "alt" in kn or "control" in kn)
        ):
            self._cmd_erase_key = None
            self._cmd_erase_streak = 0
            self._cmd_delete_left(word=True)
            return True
        # Always word-right (Ctrl+Delete)
        if kn in ("ctrl+delete", "control+delete") or (
            kn.endswith("+delete") and ("ctrl" in kn or "control" in kn)
        ):
            self._cmd_erase_key = None
            self._cmd_erase_streak = 0
            self._cmd_delete_right(word=True)
            return True
        if kn == "backspace":
            # Tap = 1 char left; hold (key-repeat) → whole words to the left
            use_word = self._cmd_erase_use_word("backspace")
            self._cmd_delete_left(word=use_word)
            return True
        if kn == "delete":
            # Tap = 1 char; hold → words.
            # At end of line (common): delete to the *left* (user request).
            # Mid-line: Delete still erases to the right.
            use_word = self._cmd_erase_use_word("delete")
            try:
                inp = self._cmd_input()
                val = (inp.value if inp else "") or ""
                pos = (
                    int(getattr(inp, "cursor_position", len(val)) or len(val))
                    if inp
                    else 0
                )
            except Exception:
                val, pos = "", 0
            if pos >= len(val):
                self._cmd_delete_left(word=use_word)
            else:
                self._cmd_delete_right(word=use_word)
            return True
        return False

    def on_key(self, event: events.Key) -> None:
        """
        Classic-style command entry: type from any panel (log/header/menu).

        Multi-char commands (r22, e3, aN…) still need the full sequence + Enter —
        same as classic stdin.readline. We only route keystrokes into #cmd when
        focus is elsewhere (e.g. after clicking the log to select/copy).

        With #cmd focused: ↑/↓ walk command history (like bash / Grok).

        Note: some host terminals omit `event.character` for `/` (slash) and
        similar keys — we resolve from key name so ABNT2/WSL can still type them.
        """
        # Any ModalScreen (help panel etc.) owns the keyboard.
        if self._modal_open():
            return

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

        # Word/char erase on #cmd (intercept before Input eats key-repeat).
        # Prefer the longer id (e.g. ctrl+backspace over backspace).
        erase_id = key_name if len(key_name) >= len(key_raw) else key_raw
        if not erase_id:
            erase_id = key_name or key_raw
        if "backspace" in erase_id or "delete" in erase_id or erase_id in ("ctrl+w",):
            if self._handle_cmd_erase_keys(event, erase_id):
                event.prevent_default()
                event.stop()
                return

        ch = self._resolve_key_character(event)
        char_missing = event.character is None or event.character == ""

        # When #cmd is focused: Input normally types. If the terminal sent a
        # key *name* without a character (slash on some WT/WSL layouts), Input
        # inserts nothing — inject ourselves.
        if self._is_cmd_focused():
            if (
                char_missing
                and ch
                and ch.isprintable()
                and ch not in ("\r", "\n", "\t")
            ):
                if self._insert_cmd_char(ch):
                    event.prevent_default()
                    event.stop()
            return

        # Let other bindings (Ctrl+C selection, Ctrl+Q, F1…) handle non-printables.
        if (
            ch is None
            and event.character is None
            and key_name
            not in (
                "enter",
                "return",
                "backspace",
                "delete",
            )
            and "backspace" not in key_name
            and "delete" not in key_name
        ):
            return

        key = key_name

        # Printable → append to command field (incl. digits for r22 / eN)
        if ch and ch.isprintable() and ch not in ("\r", "\n", "\t"):
            if self._insert_cmd_char(ch):
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

        # Log navigation on the UI thread (no worker / call_from_thread race).
        # gg/gt → top; GG/gf → bottom (GG is case-sensitive, like vim G).
        # /text · /n · /p → vim-style search on active log tab.
        low = value.lower()
        if value == "GG" or low == "gf":
            self._push_cmd_history(value)
            self.scroll_log_footer()
            return
        if low in ("gg", "gt"):
            self._push_cmd_history(value)
            self.scroll_log_top()
            return
        # Vim-style search: /query  ·  aliases without slash (keyboard layouts
        # that cannot type / into the TUI): find:query  ·  find query  ·  s?query
        if value.startswith("/"):
            self._push_cmd_history(value)
            self._handle_log_search(value)
            return
        low_full = value.lower()
        if low_full in ("find", "find:", "search", "search:"):
            self._push_cmd_history(value)
            self._handle_log_search("/")
            return
        if low_full.startswith("find:") or low_full.startswith("search:"):
            q = value.split(":", 1)[1]
            self._push_cmd_history(value)
            self._handle_log_search("/" + q)
            return
        if low_full.startswith("find ") or low_full.startswith("search "):
            q = value.split(None, 1)[1] if " " in value else ""
            self._push_cmd_history(value)
            self._handle_log_search("/" + q)
            return
        # s?text — reverse-search mnemonic without requiring /
        if len(value) >= 2 and value[0] in ("s", "S") and value[1] == "?":
            self._push_cmd_history(value)
            self._handle_log_search("/" + value[2:])
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
                self.pipeline.stop_event.is_set() and cmd in ("v", "q", "quit")
            ):
                self.call_from_thread(self.exit)
                return
        finally:
            self._cmd_busy = False
            # Never query DOM from the worker thread (deadlocks with modal).
            # Skip refocus while a modal is open — modal callback will refocus.
            try:
                self.call_from_thread(self._refocus_cmd_if_idle)
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
        """
        F1: banner + startup status + command summary → Sistema tab only.

        Tradução stays clean for Heard/Translated chunks.
        """
        # Open Sistema first so the user sees the help as it streams in
        self.focus_log_tab("app")
        try:
            app_log = self._resolve_log_widget("app")
            if app_log is not None:
                app_log.write(
                    "[bold cyan]—— Ajuda (F1) ——[/]  "
                    "[dim]aba Sistema · Tradução só frases[/]"
                )
                app_log.write("")
        except Exception:
            pass

        if self._help_fn is not None:
            try:
                with ui_mod.log_panel("app"):
                    self._help_fn()
                try:
                    self.query_one("#cmd", Input).focus()
                except Exception:
                    pass
                return
            except Exception as exc:
                self.post_log("error", f"Help error: {exc}", panel="app")
                return
        # Fallback if no help_fn wired
        self.post_log(
            "info",
            "Sentence: e/eN enew d/dN f/fN F l lo lt cls gg/GG gt/gf c | "
            "Audio: r/rN s n x a/aN p/pN ld lav lv ctts | "
            "Idiom: g t o | Session: v m u(compact) q",
            panel="app",
        )
        self.post_log(
            "info",
            "Copiar: clique+arraste → Ctrl+C | log inteiro Ctrl+Shift+C | "
            "bypass F2 | sair Ctrl+Q | F1=ajuda",
            panel="app",
        )
        try:
            self.query_one("#cmd", Input).focus()
        except Exception:
            pass

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
        Ctrl+Shift+C: copy entire scrollback of the active log tab.
        Pulls plain text from SelectableRichLog buffer and writes the clipboard.
        """
        text = ""
        panel = self._active_log_panel()
        if panel == "app":
            label = "Sistema"
        elif panel == "lc":
            label = "Tradução LC"
        elif panel == "news":
            label = "Novidades"
        elif panel == "cmds":
            label = "Comandos"
        else:
            label = "Tradução VOZ"
        try:
            log = self._active_log_widget() or self.query_one("#log", SelectableRichLog)
            text = log.get_plain_text() or ""
        except Exception:
            text = ""
        if not (text or "").strip():
            # Fallback: rendered strips (in case plain buffer is empty)
            try:
                log = self._active_log_widget() or self.query_one(
                    "#log", SelectableRichLog
                )
                text = "\n".join(line.text for line in (log.lines or []))
            except Exception:
                text = ""
        if not (text or "").strip():
            try:
                self.notify(
                    "Log vazio — nada para copiar", severity="warning", timeout=2
                )
            except Exception:
                self.post_log("warn", "Log vazio — nada para copiar")
            return
        if self._clipboard_set(text):
            n = len(text)
            lines = text.count("\n") + 1
            msg = f"Log {label} copiado ({lines} linhas, {n} chars)"
            try:
                self.notify(msg, severity="information", timeout=3)
            except Exception:
                self.post_log("success", msg)
            # Echo on VOZ (command output pane)
            self.post_log("success", msg, panel="main")
        else:
            try:
                self.notify("Falha ao copiar log", severity="error", timeout=3)
            except Exception:
                self.post_log("error", "Falha ao copiar log")

    def action_quit_app(self) -> None:
        try:
            svc = self.caption_service or getattr(
                self.pipeline, "caption_service", None
            )
            if svc is not None:
                svc.stop()
        except Exception:
            pass
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
    caption_service=None,
) -> None:
    """Block until the TUI exits."""
    app = LiveLingoApp(
        pipeline=pipeline,
        synonym_lookup=synonym_lookup,
        dispatch_command=dispatch_command,
        listen_msgs_fn=listen_msgs_fn,
        help_fn=help_fn,
        caption_service=caption_service,
    )
    app.run()
