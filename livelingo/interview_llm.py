"""
Interview coach LLM clients.

Providers:
  - grok / xai     — OpenAI-compatible (api.x.ai)
  - groq           — OpenAI-compatible (api.groq.com)
  - deepseek       — OpenAI-compatible (api.deepseek.com)
  - claude         — Anthropic Messages API
  - gemini         — Google AI generateContent

All expose ``complete(system, user) -> str``.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

import requests

XAI_URL = "https://api.x.ai/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

# Sensible defaults per provider (override with INTERVIEW_COACH_MODEL)
_DEFAULT_MODELS = {
    "grok": "grok-3-mini",
    "xai": "grok-3-mini",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "claude": "claude-sonnet-4-5-20250929",
    "anthropic": "claude-sonnet-4-5-20250929",
    "gemini": "gemini-2.0-flash",
}


class InterviewLLMError(Exception):
    """Raised when the interview coach LLM request fails."""


class InterviewLLM:
    """Minimal chat client."""

    provider_name = "base"
    api_key = ""
    model = ""

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class OpenAICompatInterviewLLM(InterviewLLM):
    """OpenAI-compatible /v1/chat/completions (Grok, Groq, DeepSeek, …)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        url: str,
        provider_name: str = "openai-compat",
        timeout_s: float = 25.0,
        key_hint: str = "",
    ):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.url = (url or "").strip()
        self.provider_name = provider_name
        self.timeout_s = float(timeout_s or 25.0)
        self._key_hint = key_hint or f"{provider_name.upper()}_API_KEY"

    def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise InterviewLLMError(
                f"{self.provider_name}: API key missing (set {self._key_hint})."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": 0.35,
            "max_tokens": 1400,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = requests.post(
                self.url, headers=headers, json=body, timeout=self.timeout_s
            )
        except requests.RequestException as exc:
            raise InterviewLLMError(
                f"{self.provider_name}: network error: {exc}"
            ) from exc
        if resp.status_code >= 400:
            detail = (resp.text or "")[:240]
            raise InterviewLLMError(
                f"{self.provider_name}: HTTP {resp.status_code}: {detail}"
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise InterviewLLMError(
                f"{self.provider_name}: invalid JSON response"
            ) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InterviewLLMError(
                f"{self.provider_name}: unexpected response shape"
            ) from exc
        # Some models return content as a list of parts
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    parts.append(str(p.get("text") or ""))
                else:
                    parts.append(str(p))
            content = "".join(parts)
        return str(content or "").strip()


class AnthropicInterviewLLM(InterviewLLM):
    """Anthropic Messages API (Claude)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        url: str = ANTHROPIC_URL,
        timeout_s: float = 25.0,
        max_tokens: int = 1400,
    ):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.url = (url or ANTHROPIC_URL).strip()
        self.provider_name = "claude"
        self.timeout_s = float(timeout_s or 25.0)
        self.max_tokens = int(max_tokens or 1400)

    def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise InterviewLLMError(
                "claude: API key missing (set ANTHROPIC_API_KEY or CLAUDE_API_KEY)."
            )
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.35,
            "system": system or "",
            "messages": [{"role": "user", "content": user}],
        }
        try:
            resp = requests.post(
                self.url, headers=headers, json=body, timeout=self.timeout_s
            )
        except requests.RequestException as exc:
            raise InterviewLLMError(f"claude: network error: {exc}") from exc
        if resp.status_code >= 400:
            detail = (resp.text or "")[:240]
            raise InterviewLLMError(f"claude: HTTP {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except Exception as exc:
            raise InterviewLLMError("claude: invalid JSON response") from exc
        # content: [{ "type": "text", "text": "..." }, ...]
        try:
            blocks = data.get("content") or []
            texts = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(str(b.get("text") or ""))
                elif isinstance(b, dict) and "text" in b:
                    texts.append(str(b.get("text") or ""))
            content = "".join(texts).strip()
        except Exception as exc:
            raise InterviewLLMError("claude: unexpected response shape") from exc
        if not content:
            raise InterviewLLMError("claude: empty content in response")
        return content


class GeminiInterviewLLM(InterviewLLM):
    """Google AI Studio generateContent (Gemini)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 25.0,
    ):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.provider_name = "gemini"
        self.timeout_s = float(timeout_s or 25.0)

    def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise InterviewLLMError(
                "gemini: API key missing (set GEMINI_API_KEY or GOOGLE_API_KEY)."
            )
        url = GEMINI_URL_TMPL.format(model=self.model)
        params = {"key": self.api_key}
        # systemInstruction + user content
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user}],
                }
            ],
            "generationConfig": {
                "temperature": 0.35,
                "maxOutputTokens": 1400,
            },
        }
        if (system or "").strip():
            body["systemInstruction"] = {
                "parts": [{"text": system.strip()}],
            }
        try:
            resp = requests.post(
                url, params=params, json=body, timeout=self.timeout_s
            )
        except requests.RequestException as exc:
            raise InterviewLLMError(f"gemini: network error: {exc}") from exc
        if resp.status_code >= 400:
            detail = (resp.text or "")[:240]
            raise InterviewLLMError(f"gemini: HTTP {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except Exception as exc:
            raise InterviewLLMError("gemini: invalid JSON response") from exc
        try:
            candidates = data.get("candidates") or []
            parts = (candidates[0].get("content") or {}).get("parts") or []
            texts = []
            for p in parts:
                if isinstance(p, dict):
                    texts.append(str(p.get("text") or ""))
            content = "".join(texts).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise InterviewLLMError("gemini: unexpected response shape") from exc
        if not content:
            raise InterviewLLMError("gemini: empty content in response")
        return content


def _cfg_str(cfg, *names: str) -> str:
    for name in names:
        val = str(getattr(cfg, name, "") or "").strip()
        if val:
            return val
    return ""


def list_interview_providers() -> list[str]:
    return ["grok", "groq", "deepseek", "claude", "gemini"]


def build_interview_llm(cfg) -> InterviewLLM:
    """Build coach LLM from ``INTERVIEW_COACH_PROVIDER`` + keys."""
    provider = str(getattr(cfg, "INTERVIEW_COACH_PROVIDER", "grok") or "grok").lower()
    # Aliases
    if provider in ("x-ai", "xai"):
        provider = "grok"
    if provider == "anthropic":
        provider = "claude"
    if provider in ("google", "google-ai"):
        provider = "gemini"

    timeout = float(getattr(cfg, "INTERVIEW_COACH_TIMEOUT_S", 25.0) or 25.0)
    model = str(getattr(cfg, "INTERVIEW_COACH_MODEL", "") or "").strip()
    default_model = _DEFAULT_MODELS.get(provider, "grok-3-mini")

    if provider == "grok":
        return OpenAICompatInterviewLLM(
            api_key=_cfg_str(cfg, "XAI_API_KEY", "GROK_API_KEY"),
            model=model or default_model,
            url=_cfg_str(cfg, "XAI_API_URL") or XAI_URL,
            provider_name="grok",
            timeout_s=timeout,
            key_hint="XAI_API_KEY (or GROK_API_KEY)",
        )
    if provider == "groq":
        return OpenAICompatInterviewLLM(
            api_key=_cfg_str(cfg, "GROQ_API_KEY"),
            model=model
            or _cfg_str(cfg, "GROQ_MODEL")
            or default_model,
            url=GROQ_URL,
            provider_name="groq",
            timeout_s=timeout,
            key_hint="GROQ_API_KEY",
        )
    if provider == "deepseek":
        return OpenAICompatInterviewLLM(
            api_key=_cfg_str(cfg, "DEEPSEEK_API_KEY"),
            model=model or default_model,
            url=_cfg_str(cfg, "DEEPSEEK_API_URL") or DEEPSEEK_URL,
            provider_name="deepseek",
            timeout_s=timeout,
            key_hint="DEEPSEEK_API_KEY",
        )
    if provider == "claude":
        return AnthropicInterviewLLM(
            api_key=_cfg_str(cfg, "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
            model=model or default_model,
            url=_cfg_str(cfg, "ANTHROPIC_API_URL") or ANTHROPIC_URL,
            timeout_s=timeout,
        )
    if provider == "gemini":
        return GeminiInterviewLLM(
            api_key=_cfg_str(cfg, "GEMINI_API_KEY", "GOOGLE_API_KEY"),
            model=model or default_model,
            timeout_s=timeout,
        )
    raise InterviewLLMError(
        f"Unknown INTERVIEW_COACH_PROVIDER={provider!r}. "
        f"Use one of: {', '.join(list_interview_providers())}."
    )


def _as_str_list(val: Any, *, limit: int = 4) -> List[str]:
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if str(x).strip()][:limit]


def _as_paragraph(val: Any, *, limit: int = 1200) -> str:
    if isinstance(val, list):
        text = " ".join(str(x).strip() for x in val if str(x).strip())
    else:
        text = str(val or "").strip()
    return text[:limit]


def parse_coach_response(raw: str) -> dict[str, Any]:
    """
    Parse LLM output into EN fields + pt-BR mirror fields.

    Prefers JSON; falls back to treating whole text as spoken (EN).
    """
    text = (raw or "").strip()
    empty = {
        "spoken": "",
        "software_engineer": [],
        "architect": [],
        "tradeoffs": "",
        "spoken_pt": "",
        "software_engineer_pt": [],
        "architect_pt": [],
        "tradeoffs_pt": "",
    }
    if not text:
        return empty

    body = text
    if "```" in body:
        start = body.find("```")
        end = body.rfind("```")
        if end > start:
            chunk = body[start + 3 : end].strip()
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].lstrip()
            body = chunk

    data: Optional[dict] = None
    try:
        data = json.loads(body)
    except Exception:
        i, j = body.find("{"), body.rfind("}")
        if i >= 0 and j > i:
            try:
                data = json.loads(body[i : j + 1])
            except Exception:
                data = None

    if not isinstance(data, dict):
        return {
            **empty,
            "spoken": text[:1200],
        }

    spoken = str(data.get("spoken") or data.get("answer") or "").strip()
    se = _as_str_list(data.get("software_engineer") or data.get("se") or [])
    arch = _as_str_list(data.get("architect") or data.get("architecture") or [])
    tradeoffs = _as_paragraph(
        data.get("tradeoffs") or data.get("trade_offs") or data.get("trade-offs") or ""
    )

    spoken_pt = str(
        data.get("spoken_pt")
        or data.get("spoken_pt_br")
        or data.get("resposta_pt")
        or ""
    ).strip()
    se_pt = _as_str_list(data.get("software_engineer_pt") or data.get("se_pt") or [])
    arch_pt = _as_str_list(data.get("architect_pt") or data.get("arch_pt") or [])
    tradeoffs_pt = _as_paragraph(
        data.get("tradeoffs_pt")
        or data.get("trade_offs_pt")
        or data.get("tradeoffs_pt_br")
        or ""
    )

    if not spoken:
        spoken = text[:1200]
    return {
        "spoken": spoken[:1500],
        "software_engineer": se,
        "architect": arch,
        "tradeoffs": tradeoffs[:1200],
        "spoken_pt": spoken_pt[:1500],
        "software_engineer_pt": se_pt,
        "architect_pt": arch_pt,
        "tradeoffs_pt": tradeoffs_pt[:1200],
    }
