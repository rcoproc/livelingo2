"""
tts_segments.py
===============
Split translated text into speakable segments for streaming TTS playback.
"""

import re


def split_tts_segments(text, max_chars=120):
    """Split text into clauses/sentences for incremental synthesis."""
    text = (text or "").strip()
    if not text:
        return []

    parts = re.split(r"(?<=[.!?…])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) == 1 and len(text) > max_chars:
        parts = re.split(r",\s+", text)
        parts = [p.strip() for p in parts if p.strip()]

    if len(parts) == 1 and len(text) > max_chars:
        words = text.split()
        parts = []
        current = []
        length = 0
        for word in words:
            word_len = len(word) + (1 if current else 0)
            if current and length + word_len > max_chars:
                parts.append(" ".join(current))
                current = [word]
                length = len(word)
            else:
                current.append(word)
                length += word_len
        if current:
            parts.append(" ".join(current))

    return parts


def split_piper_segments(text, max_chars=70):
    """
    Aggressive splitting for local Piper TTS.
    Long single sentences (no periods) are split on commas so the first
    clause can play while later clauses are still synthesizing.
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    parts = split_tts_segments(text, max_chars=max_chars)
    if len(parts) > 1:
        return parts

    comma_parts = [p.strip() for p in re.split(r",\s*", text) if p.strip()]
    if len(comma_parts) > 1:
        merged = []
        current = ""
        for part in comma_parts:
            candidate = f"{current}, {part}" if current else part
            if current and len(candidate) > max_chars:
                merged.append(current)
                current = part
            else:
                current = candidate
        if current:
            merged.append(current)
        if len(merged) > 1:
            return merged

    return [text]


class StreamingSegmentFeeder:
    """
    Pull speakable clauses from a growing partial translation (LLM stream).
    Emits a segment once a clause delimiter is confirmed (text follows it).
    """

    def __init__(self, max_chars=70, first_chars=0):
        self.max_chars = max_chars
        self.first_chars = first_chars
        self.consumed = 0

    def feed(self, partial):
        """Return newly completed segments from the latest partial text."""
        segments, self.consumed = pop_streaming_segments(
            partial,
            self.consumed,
            max_chars=self.max_chars,
            first_chars=self.first_chars,
        )
        return segments

    def flush(self, final):
        """Return any trailing text after the stream ends."""
        remainder = flush_streaming_remainder(final, self.consumed)
        if remainder:
            self.consumed = len(final)
        return [remainder] if remainder else []


def pop_streaming_segments(partial, consumed, max_chars=70, first_chars=0):
    """
    Extract completed clauses from partial translation text.

    A clause is ready when a delimiter (comma/semicolon/period) is followed by
    more text, proving the model is not still typing that delimiter.
    """
    partial = partial or ""
    segments = []

    while True:
        rest = partial[consumed:]
        if not rest.strip():
            break

        if consumed == 0 and first_chars > 0 and not segments:
            leading_ws = len(rest) - len(rest.lstrip())
            stripped = rest.lstrip()
            if len(stripped) >= first_chars:
                window = stripped[: min(len(stripped), first_chars + 10)]
                cut = window.rfind(" ")
                if cut >= max(12, first_chars // 2):
                    segment = stripped[:cut].strip()
                    if segment:
                        segments.append(segment)
                        consumed += leading_ws + cut
                        continue

        match = re.search(r"[,;.!?…](?:\s+)", rest)
        if match:
            abs_end = consumed + match.end()
            if abs_end < len(partial):
                segment = partial[consumed:abs_end].strip()
                if segment:
                    segments.append(segment)
                    consumed = abs_end
                    continue

        if consumed == 0 and not segments:
            leading = len(partial) - len(rest)
            stripped = rest.strip()
            if len(stripped) >= 25 and stripped[-1] in ".!?…":
                segments.append(stripped)
                consumed = leading + len(rest)
                continue

        stripped = rest.lstrip()
        if len(stripped) > max_chars:
            window = stripped[:max_chars]
            last_comma = window.rfind(",")
            if last_comma > max_chars // 3:
                cut = len(rest) - len(stripped) + last_comma + 1
            else:
                last_space = window.rfind(" ")
                cut = (
                    len(rest) - len(stripped) + last_space
                    if last_space > 0
                    else max_chars
                )
            abs_end = consumed + cut
            segment = partial[consumed:abs_end].strip()
            if segment:
                segments.append(segment)
                consumed = abs_end
                continue
        break

    return segments, consumed


def flush_streaming_remainder(final, consumed):
    """Return leftover text once the translation stream is complete."""
    final = (final or "").strip()
    if not final or consumed >= len(final):
        return ""
    return final[consumed:].strip()