"""Phase-2 Interview Coach providers: grok/groq/deepseek/claude/gemini."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from livelingo.interview_llm import (
    AnthropicInterviewLLM,
    GeminiInterviewLLM,
    InterviewLLMError,
    OpenAICompatInterviewLLM,
    build_interview_llm,
    list_interview_providers,
)


def _cfg(**kwargs):
    base = dict(
        INTERVIEW_COACH_PROVIDER="grok",
        INTERVIEW_COACH_MODEL="",
        INTERVIEW_COACH_TIMEOUT_S=10,
        XAI_API_KEY="",
        GROK_API_KEY="",
        XAI_API_URL="https://api.x.ai/v1/chat/completions",
        GROQ_API_KEY="",
        GROQ_MODEL="llama-3.3-70b-versatile",
        DEEPSEEK_API_KEY="",
        DEEPSEEK_API_URL="https://api.deepseek.com/v1/chat/completions",
        ANTHROPIC_API_KEY="",
        CLAUDE_API_KEY="",
        ANTHROPIC_API_URL="https://api.anthropic.com/v1/messages",
        GEMINI_API_KEY="",
        GOOGLE_API_KEY="",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_list_providers():
    names = list_interview_providers()
    for p in ("grok", "groq", "deepseek", "claude", "gemini"):
        assert p in names


def test_build_unknown_provider():
    with pytest.raises(InterviewLLMError, match="Unknown"):
        build_interview_llm(_cfg(INTERVIEW_COACH_PROVIDER="nope"))


def test_build_groq_and_deepseek_openai_compat():
    g = build_interview_llm(_cfg(INTERVIEW_COACH_PROVIDER="groq", GROQ_API_KEY="gsk"))
    assert isinstance(g, OpenAICompatInterviewLLM)
    assert g.provider_name == "groq"
    assert "groq.com" in g.url

    d = build_interview_llm(
        _cfg(INTERVIEW_COACH_PROVIDER="deepseek", DEEPSEEK_API_KEY="sk-ds")
    )
    assert d.provider_name == "deepseek"
    assert "deepseek" in d.url


def test_build_claude_and_gemini():
    c = build_interview_llm(
        _cfg(INTERVIEW_COACH_PROVIDER="claude", ANTHROPIC_API_KEY="sk-ant")
    )
    assert isinstance(c, AnthropicInterviewLLM)
    assert c.provider_name == "claude"

    g = build_interview_llm(
        _cfg(INTERVIEW_COACH_PROVIDER="gemini", GEMINI_API_KEY="AIza")
    )
    assert isinstance(g, GeminiInterviewLLM)
    assert g.provider_name == "gemini"


def test_openai_compat_complete_mocked():
    llm = OpenAICompatInterviewLLM(
        api_key="k",
        model="m",
        url="https://example.test/v1/chat/completions",
        provider_name="groq",
    )
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "choices": [{"message": {"content": '{"spoken":"ok"}'}}]
    }
    with patch("livelingo.interview_llm.requests.post", return_value=fake) as post:
        out = llm.complete("sys", "user q")
    assert out == '{"spoken":"ok"}'
    assert post.called
    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert kwargs["json"]["messages"][0]["role"] == "system"


def test_anthropic_complete_mocked():
    llm = AnthropicInterviewLLM(api_key="sk-ant", model="claude-test")
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "content": [{"type": "text", "text": '{"spoken":"from claude"}'}]
    }
    with patch("livelingo.interview_llm.requests.post", return_value=fake) as post:
        out = llm.complete("sys", "hello")
    assert "claude" in out
    headers = post.call_args.kwargs["headers"]
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"
    body = post.call_args.kwargs["json"]
    assert body["system"] == "sys"
    assert body["messages"][0]["content"] == "hello"


def test_gemini_complete_mocked():
    llm = GeminiInterviewLLM(api_key="AIza", model="gemini-2.0-flash")
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "candidates": [
            {"content": {"parts": [{"text": '{"spoken":"from gemini"}'}]}}
        ]
    }
    with patch("livelingo.interview_llm.requests.post", return_value=fake) as post:
        out = llm.complete("sys", "q")
    assert "gemini" in out
    assert "key" in post.call_args.kwargs["params"]
    body = post.call_args.kwargs["json"]
    assert "systemInstruction" in body


def test_missing_key_errors():
    with pytest.raises(InterviewLLMError, match="API key missing"):
        OpenAICompatInterviewLLM(
            api_key="", model="m", url="http://x", provider_name="deepseek"
        ).complete("s", "u")
    with pytest.raises(InterviewLLMError, match="ANTHROPIC"):
        AnthropicInterviewLLM(api_key="", model="m").complete("s", "u")
    with pytest.raises(InterviewLLMError, match="GEMINI"):
        GeminiInterviewLLM(api_key="", model="m").complete("s", "u")


def test_coach_set_provider():
    from livelingo.interview_coach import InterviewCoach

    cfg = _cfg(INTERVIEW_COACH_PROVIDER="grok", XAI_API_KEY="xai-1")
    coach = InterviewCoach(cfg)
    ok, msg = coach.set_provider("claude", "claude-sonnet-4-5-20250929")
    # key missing for claude → still ok rebuild but key_ok false in msg
    assert ok is True
    assert "claude" in msg.lower()
    assert getattr(cfg, "INTERVIEW_COACH_PROVIDER") == "claude"
    ok2, msg2 = coach.set_provider("nope")
    assert ok2 is False
    assert "inválido" in msg2.lower() or "invalid" in msg2.lower()
