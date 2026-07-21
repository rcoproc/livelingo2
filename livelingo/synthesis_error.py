"""Shared TTS exception for edge and Piper backends."""


class SynthesisError(Exception):
    """Raised when text-to-speech fails for a chunk."""