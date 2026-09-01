"""Unit tests for Google Translator wrapper (+ MyMemory fallback)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from livelingo.translate import (
    TranslationError,
    Translator,
    _looks_like_translator_garbage,
    normalize_google_lang,
    normalize_mymemory_lang,
)


def test_normalize_google_lang_aliases():
    assert normalize_google_lang("br") == "pt"
    assert normalize_google_lang("pt-BR") == "pt"
    assert normalize_google_lang("EN") == "en"


def test_normalize_mymemory_lang():
    assert normalize_mymemory_lang("en") == "en-US"
    assert normalize_mymemory_lang("pt") == "pt-BR"
    assert normalize_mymemory_lang("br") == "pt-BR"


def test_looks_like_translator_garbage():
    assert _looks_like_translator_garbage("Error 500 (Server Error)!!1500")
    assert _looks_like_translator_garbage("No translation was found")
    assert not _looks_like_translator_garbage("Olá mundo")


def test_translate_empty_returns_empty(mock_cfg):
    with patch("livelingo.translate.GoogleTranslator") as GT:
        GT.return_value = MagicMock()
        t = Translator(mock_cfg)
        assert t.translate("") == ""
        assert t.translate("   ") == ""
        GT.return_value.translate.assert_not_called()


def test_translate_success(mock_cfg):
    with patch("livelingo.translate.GoogleTranslator") as GT:
        inst = MagicMock()
        inst.translate.return_value = "  olá mundo  "
        GT.return_value = inst
        t = Translator(mock_cfg)
        assert t.translate("hello world") == "olá mundo"
        inst.translate.assert_called_once_with("hello world")


def test_translate_none_result_falls_back_mymemory(mock_cfg):
    with (
        patch("livelingo.translate.GoogleTranslator") as GT,
        patch("livelingo.translate.MyMemoryTranslator") as MM,
    ):
        g = MagicMock()
        g.translate.return_value = None
        GT.return_value = g
        m = MagicMock()
        m.translate.return_value = "olá"
        MM.return_value = m
        t = Translator(mock_cfg)
        assert t.translate("hello") == "olá"


def test_translate_google_not_found_uses_mymemory(mock_cfg):
    with (
        patch("livelingo.translate.GoogleTranslator") as GT,
        patch("livelingo.translate.MyMemoryTranslator") as MM,
    ):
        g = MagicMock()
        g.translate.side_effect = RuntimeError(
            "No translation was found using the current translator"
        )
        GT.return_value = g
        m = MagicMock()
        m.translate.return_value = (
            "Em segundo lugar, como você mantém seus tipos de API consistentes?"
        )
        MM.return_value = m
        t = Translator(mock_cfg)
        out = t.translate("Second, how do you keep your API types consistent?")
        assert "tipos de API" in out


def test_translate_all_backends_fail(mock_cfg):
    with (
        patch("livelingo.translate.GoogleTranslator") as GT,
        patch("livelingo.translate.MyMemoryTranslator") as MM,
    ):
        g = MagicMock()
        g.translate.side_effect = RuntimeError("network down")
        GT.return_value = g
        m = MagicMock()
        m.translate.side_effect = RuntimeError("mymemory down")
        MM.return_value = m
        t = Translator(mock_cfg)
        with pytest.raises(TranslationError, match="No translation"):
            t.translate("hello")


def test_set_language_pair_rebuilds_client(mock_cfg):
    with patch("livelingo.translate.GoogleTranslator") as GT:
        GT.return_value = MagicMock()
        t = Translator(mock_cfg)
        assert GT.call_count == 1
        t.set_language_pair(source="pt", target="en")
        assert GT.call_count == 2
        kwargs = GT.call_args.kwargs
        assert kwargs["source"] == "pt"
        assert kwargs["target"] == "en"


def test_set_language_pair_normalizes_br(mock_cfg):
    with patch("livelingo.translate.GoogleTranslator") as GT:
        GT.return_value = MagicMock()
        t = Translator(mock_cfg)
        t.set_language_pair(source="en", target="br")
        kwargs = GT.call_args.kwargs
        assert kwargs["target"] == "pt"
