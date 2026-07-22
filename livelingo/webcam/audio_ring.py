"""
Thread-safe TTS audio schedule for lip-sync.

The LiveLingo pipeline pushes full TTS waveforms when playback is enqueued.
Rather than dumping samples into a dumb ring (which made ``latest()`` always
return the *end* of the utterance), we **schedule** clips on a wall-clock
timeline so the lip worker reads the same portion that is roughly playing on
Cable Out.

- ``push`` queues a mono clip after any still-scheduled audio (mirrors the
  playback queue order).
- ``latest(seconds)`` returns the window ending *now* on that timeline
  (silence when nothing is scheduled / already finished).
- ``is_playing()`` is True only while a clip covers the current time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class _Clip:
    samples: np.ndarray  # mono float32
    sample_rate: int
    start_t: float  # monotonic seconds
    end_t: float


class AudioRingBuffer:
    """
    Scheduled mono float32 clips at a fixed sample rate.

    - ``push`` is non-blocking and cheap (playback thread).
    - ``latest`` returns a copy of the last ``seconds`` of *playing* audio.
    """

    def __init__(self, sample_rate: int = 24000, max_seconds: float = 2.0):
        self.sample_rate = max(8000, int(sample_rate or 24000))
        # Cap retained clip history (finished clips older than this are pruned).
        self.max_seconds = max(0.5, float(max_seconds or 2.0))
        self._clips: List[_Clip] = []
        self._lock = threading.Lock()
        self._last_push_t = 0.0
        # Optional delay so morph starts with Cable playback (device open lag).
        self.play_delay_s = 0.0

    def clear(self) -> None:
        with self._lock:
            self._clips.clear()

    def set_play_delay(self, seconds: float) -> None:
        with self._lock:
            self.play_delay_s = max(0.0, float(seconds or 0.0))

    def push(self, audio, sample_rate: Optional[int] = None) -> None:
        """Schedule audio after any still-queued clips (same order as TTS queue)."""
        if audio is None:
            return
        try:
            arr = np.asarray(audio, dtype=np.float32)
        except Exception:
            return
        if arr.size == 0:
            return
        if arr.ndim > 1:
            arr = np.mean(arr, axis=-1).astype(np.float32)
        arr = np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)

        sr = int(sample_rate or self.sample_rate)
        if sr > 0 and sr != self.sample_rate and arr.size > 1:
            n_out = max(1, int(round(arr.size * float(self.sample_rate) / float(sr))))
            x_old = np.linspace(0.0, 1.0, num=arr.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            arr = np.interp(x_new, x_old, arr).astype(np.float32)
            sr = self.sample_rate

        duration = float(arr.size) / float(sr)
        now = time.monotonic()
        with self._lock:
            delay = self.play_delay_s
            # Start after the last scheduled clip (or now + delay).
            start = now + delay
            if self._clips:
                last_end = self._clips[-1].end_t
                if last_end > start:
                    start = last_end
            clip = _Clip(
                samples=arr,
                sample_rate=sr,
                start_t=start,
                end_t=start + duration,
            )
            self._clips.append(clip)
            self._last_push_t = now
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        """Drop finished clips older than max_seconds (keep active/upcoming)."""
        keep_after = now - self.max_seconds
        self._clips = [
            c
            for c in self._clips
            if c.end_t >= keep_after or c.end_t > now or c.start_t > now
        ]
        # Hard cap: never keep more than ~30 s of total scheduled audio
        total = sum(c.end_t - c.start_t for c in self._clips)
        while total > 30.0 and len(self._clips) > 1:
            dropped = self._clips.pop(0)
            total -= dropped.end_t - dropped.start_t

    def is_playing(self, now: Optional[float] = None) -> bool:
        """True if wall-clock is inside a scheduled clip (TTS active)."""
        t = float(now if now is not None else time.monotonic())
        with self._lock:
            for c in self._clips:
                if c.start_t <= t < c.end_t:
                    return True
            return False

    def seconds_until_idle(self) -> float:
        """Seconds until the schedule is fully drained (0 if idle)."""
        now = time.monotonic()
        with self._lock:
            if not self._clips:
                return 0.0
            end = max(c.end_t for c in self._clips)
            return max(0.0, end - now)

    def latest(self, seconds: float = 0.35) -> Tuple[np.ndarray, int]:
        """
        Return (samples mono float32, sample_rate) for the trailing wall-clock
        window ending *now*. Empty array when nothing is scheduled to play.
        """
        win = max(0.05, float(seconds))
        n = max(1, int(self.sample_rate * win))
        now = time.monotonic()
        t0 = now - win

        with self._lock:
            self._prune_locked(now)
            clips = list(self._clips)
            sr = self.sample_rate

        if not clips:
            return np.zeros(0, dtype=np.float32), sr

        # Build output buffer: silence by default, fill from overlapping clips.
        out = np.zeros(n, dtype=np.float32)
        any_fill = False
        for clip in clips:
            # Overlap of [clip.start, clip.end) with [t0, now]
            o0 = max(clip.start_t, t0)
            o1 = min(clip.end_t, now)
            if o1 <= o0:
                continue
            # Sample indices in clip
            off0 = int(round((o0 - clip.start_t) * clip.sample_rate))
            off1 = int(round((o1 - clip.start_t) * clip.sample_rate))
            off0 = max(0, min(clip.samples.size, off0))
            off1 = max(off0, min(clip.samples.size, off1))
            if off1 <= off0:
                continue
            # Destination indices in out (relative to t0)
            d0 = int(round((o0 - t0) * sr))
            d1 = d0 + (off1 - off0)
            if d0 < 0:
                skip = -d0
                off0 += skip
                d0 = 0
            if d1 > n:
                cut = d1 - n
                off1 -= cut
                d1 = n
            if off1 <= off0 or d1 <= d0:
                continue
            length = min(off1 - off0, d1 - d0)
            out[d0 : d0 + length] = clip.samples[off0 : off0 + length]
            any_fill = True

        if not any_fill:
            return np.zeros(0, dtype=np.float32), sr
        return np.ascontiguousarray(out, dtype=np.float32), sr

    def rms(self, seconds: float = 0.12) -> float:
        samples, _ = self.latest(seconds)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(samples)) + 1e-12))

    def open_amount(self, seconds: float = 0.12, sensitivity: float = 28.0) -> float:
        """
        0..1 lip-open proxy from current playing window.

        Uses short-window RMS against a longer peak so speech has clear
        open/close dynamics (not flattened peak-norm every frame).
        """
        samples, _ = self.latest(seconds)
        if samples.size < 8:
            return 0.0
        a = samples.astype(np.float32)
        # Longer context for peak (same schedule, up to 0.5s)
        long_s, _ = self.latest(max(seconds, 0.45))
        if long_s.size < 8:
            long_s = a
        peak = float(np.max(np.abs(long_s)) + 1e-8)
        if peak < 1e-5:
            return 0.0
        rms = float(np.sqrt(np.mean(np.square(a)) + 1e-12))
        # Relative energy 0..1 then soft expand for visibility
        rel = float(np.clip(rms / peak, 0.0, 1.0))
        # Speech often sits mid-range — curve boosts mid levels
        curved = float(rel ** 0.65)
        amt = float(np.clip(curved * (float(sensitivity) / 18.0), 0.0, 1.0))
        # Floor while there is real signal so lips keep moving on quiet phonemes
        if rms > peak * 0.02 and 0.0 < amt < 0.18:
            amt = 0.18
        return amt

    def seconds_since_push(self) -> float:
        with self._lock:
            if self._last_push_t <= 0:
                return 1e9
            return float(time.monotonic() - self._last_push_t)
