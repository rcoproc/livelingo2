"""Piper voice id helpers."""

from livelingo.piper_voices import fast_voice_for


def test_fast_voice_for_only_when_low_exists():
    assert fast_voice_for("en_US-ryan-medium") == "en_US-ryan-low"
    assert fast_voice_for("en_US-lessac-medium") == "en_US-lessac-low"
    # joe has medium only on Hugging Face — do not invent joe-low (404)
    assert fast_voice_for("en_US-joe-medium") == "en_US-joe-medium"
    assert fast_voice_for("en_US-ryan-low") == "en_US-ryan-low"
