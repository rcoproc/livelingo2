"""
Integration: phrase cache + mocked translator path used by the pipeline.

Ensures HIT skips re-translation and MISS path stores for the next chunk.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from livelingo.phrase_cache import PhraseCache


def _translate_with_cache(cache: PhraseCache, translator, source, target, text):
    """Minimal pipeline-like resolve: lookup → else translate → store."""
    hit = cache.lookup(source, target, text)
    if hit is not None:
        return hit, "cache"
    out = translator.translate(text)
    cache.store(source, target, text, out)
    return out, "live"


def test_pipeline_like_hit_miss_with_mock_translator(tmp_db, mock_cfg):
    cache = PhraseCache(mock_cfg)
    translator = MagicMock()
    translator.translate.return_value = "Olá equipe"

    # First: MISS → live
    out1, src1 = _translate_with_cache(
        cache, translator, "en", "pt", "Hello team"
    )
    assert out1 == "Olá equipe"
    assert src1 == "live"
    translator.translate.assert_called_once_with("Hello team")

    # Second: HIT → no second live call
    out2, src2 = _translate_with_cache(
        cache, translator, "en", "pt", "Hello team!"
    )
    assert out2 == "Olá equipe"
    assert src2 == "cache"
    translator.translate.assert_called_once()  # still one call


def test_force_next_forces_live_again(tmp_db, mock_cfg):
    cache = PhraseCache(mock_cfg)
    translator = MagicMock()
    translator.translate.side_effect = ["v1", "v2"]

    out1, _ = _translate_with_cache(cache, translator, "en", "pt", "ping")
    assert out1 == "v1"

    cache.request_force_next()
    assert cache.consume_force_next() is True
    # After force consume, lookup would hit unless we skip before translate:
    # pipeline pattern: if force: skip lookup
    out_live = translator.translate("ping")
    cache.store("en", "pt", "ping", out_live, from_force=True)
    assert out_live == "v2"
    assert cache.lookup("en", "pt", "ping") == "v2"


def test_synthesis_error_type():
    from livelingo.synthesis_error import SynthesisError

    err = SynthesisError("boom")
    assert isinstance(err, Exception)
    assert "boom" in str(err)


def test_chunk_skip_placeholder():
    """_ChunkSkip is a tiny ordered placeholder in pipeline (avoid importing PortAudio)."""
    # Mirror the class contract without loading pipeline (capture→sounddevice).
    class _ChunkSkip:
        __slots__ = ("message", "kind")

        def __init__(self, message="", kind="dim"):
            self.message = message or ""
            self.kind = kind

    skip = _ChunkSkip("hallucination", kind="warn")
    assert skip.message == "hallucination"
    assert skip.kind == "warn"
    # Source still defines the real type
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "livelingo" / "pipeline.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    names = {
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef)
    }
    assert "_ChunkSkip" in names
