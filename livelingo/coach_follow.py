"""
coach_follow.py
===============
Word-by-word follow-along for Spoken (EN) in ``view coach``.

Teleprompter mode: advance the highlight at a fixed reading speed
(``COACH_FOLLOW_WPM``) — you read along; the cursor never waits on STT.

Matcher helpers (``advance_cursor`` / ``words_match``) remain for tests and
optional future hybrid modes; the live engine is timed-only.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, List, Optional, Sequence

_WORD_RE = re.compile(r"\S+")
_SKIPPABLE = frozenset(
    {
        "a",
        "an",
        "the",
        "uh",
        "um",
        "oh",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
    }
)


def tokenize_spoken(text: str) -> List[str]:
    """Split Spoken EN into display tokens (keeps punctuation attached)."""
    return [m.group(0) for m in _WORD_RE.finditer((text or "").strip())]


def normalize_word(token: str) -> str:
    """Lowercase compare key without leading/trailing punctuation."""
    t = (token or "").lower()
    t = re.sub(r"^[^\w]+|[^\w]+$", "", t, flags=re.UNICODE)
    return t


def words_match(expected: str, heard: str, *, threshold: float = 0.75) -> bool:
    """True if heard STT token matches expected Spoken token."""
    a = normalize_word(expected)
    b = normalize_word(heard)
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith(b) or b.startswith(a):
        # Prefix: "design" vs "designing" / partial STT
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if len(shorter) >= 3 and longer.startswith(shorter):
            return True
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio >= float(threshold)


def advance_cursor(
    words: Sequence[str],
    cursor: int,
    stt_text: str,
    *,
    threshold: float = 0.75,
    max_skip: int = 2,
) -> int:
    """
    Advance ``cursor`` (index of *current* word) using tokens from ``stt_text``.

    Returns the new cursor in ``[0, len(words)]`` where ``len(words)`` means done.
    Never moves backward.
    """
    n = len(words)
    if cursor < 0:
        cursor = 0
    if cursor >= n:
        return n
    heard = [normalize_word(t) for t in tokenize_spoken(stt_text)]
    heard = [h for h in heard if h]
    if not heard:
        return cursor

    i = cursor
    for h in heard:
        if i >= n:
            break
        # Direct match
        if words_match(words[i], h, threshold=threshold):
            i += 1
            continue
        # Allow skipping short function words the speaker omitted
        skipped = False
        for skip in range(1, max_skip + 1):
            j = i + skip
            if j >= n:
                break
            if normalize_word(words[i]) in _SKIPPABLE and words_match(
                words[j], h, threshold=threshold
            ):
                i = j + 1
                skipped = True
                break
            if words_match(words[j], h, threshold=threshold):
                # Skip ahead over a missed word (STT drop / paraphrase)
                i = j + 1
                skipped = True
                break
        if skipped:
            continue
        # No progress on this heard token — try next heard token
    return min(i, n)


TRADEOFFS_TITLE = "Trade-offs"


def build_follow_script(
    spoken: str, tradeoffs: str = ""
) -> tuple[List[str], int]:
    """
    Build the teleprompter playlist: Spoken words, then optional
    ``Trade-offs`` title + tradeoff words.

    Returns ``(script, spoken_len)`` where ``spoken_len`` is the index of the
    title (or ``len(script)`` when there is no tradeoffs section).
    """
    spoken_w = tokenize_spoken(spoken)
    trades = (tradeoffs or "").strip()
    if not trades:
        return spoken_w, len(spoken_w)
    trade_w = tokenize_spoken(trades)
    return spoken_w + [TRADEOFFS_TITLE] + trade_w, len(spoken_w)


def render_spoken_markup(
    words: Sequence[str],
    cursor: int,
    *,
    accent: str = "bold cyan",
    tradeoffs: str = "",
    show_tradeoffs: bool = False,
    spoken_len: Optional[int] = None,
) -> str:
    """
    Rich markup for the teleprompter script (Spoken → Trade-offs).

    ``words`` is the full playlist from ``build_follow_script``.
    While still in Spoken (cursor < spoken_len), only Spoken is shown.
    Once the cursor reaches the Trade-offs title, the section appears and
    reading continues through the title and tradeoff words.
    """
    n = len(words)
    cur = max(0, min(int(cursor), n))
    if spoken_len is None:
        # Legacy: words are Spoken-only; optional bolted-on tradeoffs block
        sp_len = n
    else:
        sp_len = max(0, min(int(spoken_len), n))

    if n == 0:
        base = "[dim](sem Spoken EN ainda — aguarde o Coach)[/]"
    else:
        spoken_part = words[:sp_len]
        parts: list[str] = []
        for i, w in enumerate(spoken_part):
            safe = (w or "").replace("[", "\\[")
            if i < cur:
                parts.append(f"[dim]{safe}[/]")
            elif i == cur and cur < sp_len:
                parts.append(f"[{accent}]{safe}[/]")
            else:
                # Past Spoken (in tradeoffs) → all spoken dim
                if cur >= sp_len:
                    parts.append(f"[dim]{safe}[/]")
                else:
                    parts.append(safe)
        base = " ".join(parts) if parts else "[dim](sem Spoken EN ainda — aguarde o Coach)[/]"

    in_tradeoffs = cur >= sp_len and sp_len < n
    if in_tradeoffs or show_tradeoffs:
        title_idx = sp_len  # TRADEOFFS_TITLE in script
        trade_words = list(words[sp_len + 1 :]) if sp_len < n else tokenize_spoken(tradeoffs)
        # Title line
        if sp_len < n and words[sp_len] == TRADEOFFS_TITLE:
            if cur == title_idx:
                title_md = f"[bold #e0a020]── {TRADEOFFS_TITLE} ──[/]"
            else:
                title_md = f"[dim]── {TRADEOFFS_TITLE} ──[/]"
        else:
            title_md = f"[bold #e0a020]── {TRADEOFFS_TITLE} ──[/]"

        tparts: list[str] = []
        for j, w in enumerate(trade_words):
            abs_i = sp_len + 1 + j
            safe = (w or "").replace("[", "\\[")
            if abs_i < cur:
                tparts.append(f"[dim]{safe}[/]")
            elif abs_i == cur and cur < n:
                tparts.append(f"[{accent}]{safe}[/]")
            else:
                tparts.append(safe)
        body = " ".join(tparts) if tparts else "[dim](nenhum Trade-off nesta resposta)[/]"
        return f"{base}\n\n{title_md}\n{body}"
    return base


def wrap_words_to_lines(words: Sequence[str], width: int) -> List[List[str]]:
    """Greedy wrap of tokens into rows of at most ``width`` columns."""
    col_w = max(8, int(width))
    lines: List[List[str]] = []
    current: List[str] = []
    used = 0
    for w in words:
        token = w or ""
        need = len(token) if not current else len(token) + 1
        if current and used + need > col_w:
            lines.append(current)
            current = [token]
            used = len(token)
        else:
            current.append(token)
            used += need
    if current or not lines:
        lines.append(current)
    return lines


def line_index_for_cursor(words: Sequence[str], cursor: int, width: int) -> int:
    """0-based wrapped line that contains the current word (or last line if done)."""
    n = len(words)
    if n == 0:
        return 0
    cur = max(0, min(int(cursor), n))
    # If finished, point at the last spoken word's line
    idx = min(cur, n - 1) if cur >= n else cur
    lines = wrap_words_to_lines(words, width)
    count = 0
    for li, row in enumerate(lines):
        for _ in row:
            if count == idx:
                return li
            count += 1
    return max(0, len(lines) - 1)


def render_current_word_banner(
    words: Sequence[str],
    cursor: int,
    *,
    accent: str = "bold cyan",
    spoken_len: Optional[int] = None,
) -> str:
    """
    Large visual for the *current* word (shown in a tall 3-row strip).

    Terminals cannot scale font to 250%; a dedicated tall row + spaced letters
    approximates ~2–3× emphasis. Trade-offs title uses gold styling.
    """
    n = len(words)
    if not words:
        return "[dim]—[/]"
    cur = max(0, min(int(cursor), n))
    if cur >= n:
        return f"[{accent}]✓ done[/]"
    raw = words[cur] or ""
    if raw == TRADEOFFS_TITLE or (
        spoken_len is not None and cur == int(spoken_len) and raw == TRADEOFFS_TITLE
    ):
        return "[bold #e0a020]── Trade-offs ──[/]"
    safe = raw.replace("[", "\\[")
    # Space letters for visual width ≈ 2× (closer to 250% feel with tall CSS row)
    letters = [c for c in safe if c.strip() or c in ("-", "'")]
    if len(safe) <= 12 and letters:
        spaced = " ".join(safe)
    else:
        spaced = f"  {safe}  "
    return f"[{accent}]{spaced}[/]"


@dataclass
class FollowState:
    words: List[str] = field(default_factory=list)
    cursor: int = 0
    running: bool = False
    paused: bool = False
    status: str = "idle"
    error: str = ""
    wpm: float = 130.0
    spoken_len: int = 0
    tradeoffs_plain: str = ""


class CoachFollowEngine:
    """
    Timed teleprompter for Spoken EN → Trade-offs.

    Advances one word every ``60000 / wpm`` ms through Spoken, then the
    ``Trade-offs`` title, then the tradeoffs paragraph. No mic / STT.
    ``on_update(state)`` may fire from the worker thread — UI must marshal.
    """

    def __init__(
        self,
        *,
        on_update: Optional[Callable[[FollowState], None]] = None,
        wpm: float = 130.0,
        # Legacy kwargs ignored (kept so older call sites don't break)
        threshold: float = 0.75,
        chunk_ms: int = 1000,
        sample_rate: int = 16000,
        stt_mode: str = "off",
    ):
        self.on_update = on_update
        self.wpm = _clamp_wpm(wpm)
        self.threshold = float(threshold)  # unused (matcher helpers only)
        self.chunk_ms = int(chunk_ms)
        self.sample_rate = int(sample_rate)
        self.stt_mode = (stt_mode or "off").lower()
        self.state = FollowState(wpm=self.wpm)
        self._spoken_plain = ""
        self._tradeoffs_plain = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def ms_per_word(self) -> float:
        return 60000.0 / max(1.0, self.wpm)

    def set_wpm(self, wpm: float) -> None:
        self.wpm = _clamp_wpm(wpm)
        with self._lock:
            self.state.wpm = self.wpm
        self._emit()

    def _rebuild_script_locked(self) -> None:
        script, sp_len = build_follow_script(
            self._spoken_plain, self._tradeoffs_plain
        )
        self.state.words = script
        self.state.spoken_len = sp_len
        self.state.tradeoffs_plain = self._tradeoffs_plain
        if script:
            if self.state.cursor > len(script):
                self.state.cursor = len(script)
            if self.state.status in ("empty", "idle"):
                self.state.status = "ready"
        else:
            self.state.cursor = 0
            self.state.status = "empty"

    def set_spoken(self, text: str) -> None:
        with self._lock:
            self._spoken_plain = (text or "").strip()
            self.state.cursor = 0
            self.state.error = ""
            self.state.wpm = self.wpm
            self._rebuild_script_locked()
            if self.state.words:
                self.state.status = "ready"
            else:
                self.state.status = "empty"
        self._emit()

    def set_tradeoffs(self, text: str) -> None:
        """Attach/replace Trade-offs paragraph (continues after Spoken)."""
        with self._lock:
            self._tradeoffs_plain = (text or "").strip()
            self.state.error = ""
            self._rebuild_script_locked()
            if self.state.words and self.state.status in ("empty", "idle"):
                self.state.status = "ready"
        self._emit()

    def restart(self) -> None:
        with self._lock:
            self.state.cursor = 0
            self.state.error = ""
            if self.state.words:
                self.state.status = (
                    self._status_for_cursor(0)
                    if self.state.running
                    else "ready"
                )
        self._emit()

    def _status_for_cursor(self, cursor: int) -> str:
        n = len(self.state.words)
        sp = int(self.state.spoken_len)
        if n == 0:
            return "empty"
        if cursor >= n:
            return "done"
        if cursor >= sp:
            return "tradeoffs"
        return "reading"

    def start(self) -> tuple[bool, str]:
        """Start timed teleprompter. Returns (ok, message)."""
        with self._lock:
            if self.state.running:
                self.state.paused = False
                self.state.status = self._status_for_cursor(self.state.cursor)
                self._emit_locked()
                return True, f"retomado · {self.wpm:.0f} WPM"
            if not self.state.words:
                return False, "Sem Spoken EN para acompanhar"
            # Restart from current cursor if already done
            if self.state.cursor >= len(self.state.words):
                self.state.cursor = 0
        self._stop.clear()
        with self._lock:
            self.state.running = True
            self.state.paused = False
            self.state.status = self._status_for_cursor(self.state.cursor)
            self.state.error = ""
            self.state.wpm = self.wpm
        self._thread = threading.Thread(
            target=self._timed_worker, name="coach-follow-wpm", daemon=True
        )
        self._thread.start()
        self._emit()
        return True, f"reading · {self.wpm:.0f} WPM"

    def pause(self) -> None:
        with self._lock:
            if self.state.running:
                self.state.paused = True
                self.state.status = "paused"
        self._emit()

    def toggle_pause(self) -> None:
        with self._lock:
            if not self.state.running:
                return
            self.state.paused = not self.state.paused
            if self.state.paused:
                self.state.status = "paused"
            else:
                self.state.status = self._status_for_cursor(self.state.cursor)
        self._emit()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self.state.running = False
            self.state.paused = False
            if self.state.status not in ("done", "empty", "error"):
                self.state.status = "ready" if self.state.words else "idle"
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        self._emit()

    # --- internals ---------------------------------------------------------

    def _emit(self) -> None:
        with self._lock:
            self._emit_locked()

    def _emit_locked(self) -> None:
        cb = self.on_update
        if cb is None:
            return
        snap = FollowState(
            words=list(self.state.words),
            cursor=int(self.state.cursor),
            running=bool(self.state.running),
            paused=bool(self.state.paused),
            status=str(self.state.status),
            error=str(self.state.error or ""),
            wpm=float(self.state.wpm or self.wpm),
            spoken_len=int(self.state.spoken_len),
            tradeoffs_plain=str(self.state.tradeoffs_plain or ""),
        )
        try:
            cb(snap)
        except Exception:
            pass

    def _word_delay_s(self, word: str) -> float:
        """
        Seconds to dwell on ``word``.

        Base = 60/wpm; short function words a bit faster, long tokens slower.
        Title gets a slightly longer beat so it can be read as a section header.
        """
        base = self.ms_per_word() / 1000.0
        if (word or "") == TRADEOFFS_TITLE:
            return max(base * 1.8, 0.55)
        key = normalize_word(word)
        if not key:
            return base * 0.5
        if key in _SKIPPABLE:
            return base * 0.65
        n = len(key)
        if n <= 3:
            return base * 0.8
        if n >= 10:
            return base * (1.0 + min(0.5, (n - 9) * 0.06))
        return base

    def _timed_worker(self) -> None:
        """Advance cursor on a WPM clock until done or stopped."""
        # Brief beat so the first word is visible before advancing
        first_hold = min(0.35, self.ms_per_word() / 1000.0)
        if first_hold > 0 and not self._stop.wait(first_hold):
            pass

        while not self._stop.is_set():
            with self._lock:
                paused = bool(self.state.paused)
                running = bool(self.state.running)
                words = list(self.state.words)
                cur = int(self.state.cursor)
            if not running:
                break
            if paused:
                if self._stop.wait(0.05):
                    break
                continue
            if cur >= len(words):
                with self._lock:
                    self.state.status = "done"
                    self.state.running = False
                self._emit()
                break
            delay = self._word_delay_s(words[cur])
            # Sleep in small slices so pause/stop react quickly
            end = time.monotonic() + max(0.05, delay)
            while time.monotonic() < end:
                if self._stop.is_set():
                    return
                with self._lock:
                    if not self.state.running:
                        return
                    if self.state.paused:
                        break
                time.sleep(min(0.05, max(0.01, end - time.monotonic())))
            with self._lock:
                if self.state.paused or not self.state.running:
                    continue
                if self.state.cursor != cur:
                    # restart() moved the cursor — don't double-advance
                    continue
                self.state.cursor = cur + 1
                self.state.status = self._status_for_cursor(self.state.cursor)
                if self.state.status == "done":
                    self.state.running = False
                self._emit_locked()


def _clamp_wpm(wpm: float) -> float:
    try:
        v = float(wpm)
    except (TypeError, ValueError):
        v = 130.0
    return max(40.0, min(300.0, v))
