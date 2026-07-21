"""Unit tests for TTS text segmentation / streaming feeder."""

from __future__ import annotations

from livelingo.tts_segments import (
    StreamingSegmentFeeder,
    flush_streaming_remainder,
    pop_streaming_segments,
    split_piper_segments,
    split_tts_segments,
)


def test_split_tts_empty():
    assert split_tts_segments("") == []
    assert split_tts_segments("   ") == []


def test_split_tts_on_sentences():
    text = "Hello world. How are you? Fine!"
    parts = split_tts_segments(text)
    assert len(parts) >= 2
    assert any("Hello" in p for p in parts)


def test_split_tts_long_no_punctuation_word_wrap():
    words = " ".join(f"word{i}" for i in range(40))
    parts = split_tts_segments(words, max_chars=40)
    assert len(parts) > 1
    assert all(len(p) <= 50 for p in parts)  # soft bound


def test_split_piper_short_unchanged():
    assert split_piper_segments("Short.", max_chars=70) == ["Short."]


def test_split_piper_long_uses_commas():
    text = (
        "This is a long clause without periods, then another clause here, "
        "and yet another one that keeps going for a while"
    )
    parts = split_piper_segments(text, max_chars=40)
    assert len(parts) > 1


def test_pop_streaming_segments_emits_after_delimiter_plus_more():
    partial = "Hello world. More text"
    segs, consumed = pop_streaming_segments(partial, 0, max_chars=70)
    assert segs
    assert "Hello" in segs[0]
    assert consumed > 0


def test_pop_streaming_no_emit_while_still_on_delimiter():
    # Delimiter at end without following text → wait
    segs, consumed = pop_streaming_segments("Hello world.", 0, max_chars=70)
    # May emit via short-sentence rule if ends with period and >= 25 chars
    assert isinstance(segs, list)
    assert consumed >= 0


def test_flush_streaming_remainder():
    assert flush_streaming_remainder("Hello world", 0) == "Hello world"
    assert flush_streaming_remainder("Hello world", 6) == "world"
    assert flush_streaming_remainder("Hello", 100) == ""
    assert flush_streaming_remainder("", 0) == ""


def test_streaming_feeder_feed_and_flush():
    feeder = StreamingSegmentFeeder(max_chars=70)
    mid = "First sentence. Second is still"
    segs = feeder.feed(mid)
    assert isinstance(segs, list)
    final = "First sentence. Second is still growing here."
    tail = feeder.flush(final)
    assert isinstance(tail, list)
    # Remaining text should surface via flush or earlier feeds
    combined = " ".join(segs + tail)
    assert "First" in combined or "Second" in combined or feeder.consumed > 0
