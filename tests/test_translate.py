"""Unit tests for Google Translator wrapper (mocked network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from livelingo.translate import TranslationError, Translator


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


def test_translate_none_result_becomes_empty(mock_cfg):
    with patch("livelingo.translate.GoogleTranslator") as GT:
        inst = MagicMock()
        inst.translate.return_value = None
        GT.return_value = inst
        t = Translator(mock_cfg)
        assert t.translate("x") == ""


def test_translate_raises_translation_error(mock_cfg):
    with patch("livelingo.translate.GoogleTranslator") as GT:
        inst = MagicMock()
        inst.translate.side_effect = RuntimeError("network down")
        GT.return_value = inst
        t = Translator(mock_cfg)
        with pytest.raises(TranslationError, match="network down"):
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
