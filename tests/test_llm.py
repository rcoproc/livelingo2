"""Unit tests for Groq LLM translator (mocked HTTP — no network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from livelingo.llm import LLMError, LLMTranslator


def _ok_json(content: str = "tradução limpa"):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    resp.text = ""
    return resp


def test_translate_empty(mock_cfg):
    with patch("livelingo.llm.requests.Session") as Sess:
        t = LLMTranslator(mock_cfg)
        assert t.translate("") == ""
        Sess.return_value.post.assert_not_called()


def test_translate_success(mock_cfg):
    with patch("livelingo.llm.requests.Session") as Sess:
        sess = Sess.return_value
        sess.post.return_value = _ok_json('  "Hello there"  ')
        t = LLMTranslator(mock_cfg)
        out = t.translate("olá")
        assert out == "Hello there"
        sess.post.assert_called_once()
        args, kwargs = sess.post.call_args
        assert "chat/completions" in args[0]
        assert kwargs["json"]["model"] == mock_cfg.GROQ_MODEL


def test_translate_401(mock_cfg):
    with patch("livelingo.llm.requests.Session") as Sess:
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "unauthorized"
        Sess.return_value.post.return_value = resp
        t = LLMTranslator(mock_cfg)
        with pytest.raises(LLMError, match="401"):
            t.translate("hi")


def test_translate_network_error(mock_cfg):
    import requests as req_lib

    with patch("livelingo.llm.requests.Session") as Sess:
        Sess.return_value.post.side_effect = req_lib.ConnectionError("offline")
        t = LLMTranslator(mock_cfg)
        with pytest.raises(LLMError, match="network"):
            t.translate("hi")


def test_translate_stream_aggregates_tokens(mock_cfg):
    with patch("livelingo.llm.requests.Session") as Sess:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_lines.return_value = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: [DONE]",
        ]
        Sess.return_value.post.return_value = resp
        t = LLMTranslator(mock_cfg)
        tokens = []
        out = t.translate_stream("oi", on_token=tokens.append)
        assert out == "Hello"
        assert tokens[-1] == "Hello"


def test_refresh_prompt_on_lang_swap(mock_cfg):
    with patch("livelingo.llm.requests.Session"):
        t = LLMTranslator(mock_cfg)
        old = t.system_prompt
        t.set_language_pair(source="pt", target="en")
        assert t.system_prompt != old or "Portuguese" in t.system_prompt
        assert "English" in t.system_prompt or "en" in t.system_prompt.lower()
