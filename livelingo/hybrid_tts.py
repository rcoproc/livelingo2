"""
hybrid_tts.py
=============
Low-latency live playback: edge-tts for the first clause, Piper for the tail
and for full-utterance synthesis (replay / edit / cache).
"""

import numpy as np

from .piper_tts import PiperSynthesizer
from .synthesize import Synthesizer
from .tts_segments import split_piper_segments


def _resample(audio, src_rate, dst_rate):
    if audio is None or len(audio) == 0 or src_rate == dst_rate:
        return audio
    src_len = len(audio)
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    x_src = np.linspace(0.0, 1.0, src_len, endpoint=False)
    x_dst = np.linspace(0.0, 1.0, dst_len, endpoint=False)
    return np.interp(x_dst, x_src, audio).astype(np.float32)


class HybridSynthesizer:
    """edge-tts first chunk + Piper tail — best time-to-first-audio on slow CPUs."""

    supports_live_streaming = True

    def __init__(self, config, log=print):
        self.cfg = config
        self.log = log
        self.edge = Synthesizer(config)
        self.piper = PiperSynthesizer(config, log=log)
        self.segment_min_chars = self.piper.segment_min_chars
        self._cache_rate = 22050
        self._first_played = False

    def begin_utterance(self):
        """Reset per-chunk state so the first clause uses edge-tts again."""
        self._first_played = False

    def set_voice(self, voice_id):
        """Update edge voice; Piper rebinds via set_language_pair if available."""
        self.edge.set_voice(voice_id)
        if hasattr(self.piper, "set_language_pair"):
            try:
                self.piper.set_language_pair()
            except Exception as exc:
                self.log(f"Piper rebind after language swap failed ({exc}).")

    def _to_cache_rate(self, audio, sample_rate):
        if audio is None:
            return None, sample_rate
        if sample_rate != self._cache_rate:
            audio = _resample(audio, sample_rate, self._cache_rate)
            sample_rate = self._cache_rate
        return audio, sample_rate

    def _synthesize_first_edge(self, text, on_segment=None):
        try:
            audio, sample_rate = self.edge.synthesize(text)
        except Exception as exc:
            self.log(f"Edge TTS failed ({exc}) — using Piper for first chunk.")
            if on_segment is not None:
                return self.piper.synthesize_clause(text, on_segment)
            return self.piper.synthesize(text)

        audio, sample_rate = self._to_cache_rate(audio, sample_rate)
        if audio is not None and len(audio) > 0 and on_segment is not None:
            on_segment(audio, sample_rate)
        return audio, sample_rate

    def synthesize_clause(self, text, on_segment):
        text = (text or "").strip()
        if not text:
            return None, None

        if not self._first_played:
            self._first_played = True
            return self._synthesize_first_edge(text, on_segment)

        audio, sample_rate = self.piper.synthesize_clause(text, on_segment)
        return self._to_cache_rate(audio, sample_rate)

    def synthesize_streaming(self, text, on_segment):
        text = (text or "").strip()
        if not text:
            return None, None

        self.begin_utterance()
        segments = split_piper_segments(text, max_chars=self.segment_min_chars)
        if not segments:
            return None, None

        merge_tail = getattr(self.cfg, "PIPER_MERGE_TAIL", True)
        parts = []
        sample_rate = None

        if merge_tail and len(segments) > 1:
            first, rest = segments[0], " ".join(segments[1:])
            audio, sample_rate = self._synthesize_first_edge(first, on_segment)
            if audio is not None and len(audio) > 0:
                parts.append(audio)
            if rest.strip():
                audio, sample_rate = self.piper.synthesize_clause(rest, on_segment)
                audio, sample_rate = self._to_cache_rate(audio, sample_rate)
                if audio is not None and len(audio) > 0:
                    parts.append(audio)
        else:
            for idx, segment in enumerate(segments):
                if idx == 0:
                    audio, sample_rate = self._synthesize_first_edge(segment, on_segment)
                else:
                    audio, sample_rate = self.piper.synthesize_clause(segment, on_segment)
                    audio, sample_rate = self._to_cache_rate(audio, sample_rate)
                if audio is not None and len(audio) > 0:
                    parts.append(audio)

        if not parts:
            return None, None
        return np.concatenate(parts).astype(np.float32), sample_rate

    def synthesize(self, text):
        """Full Piper utterance for edit/replay paths that need one voice."""
        return self.piper.synthesize(text)