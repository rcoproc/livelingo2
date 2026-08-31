"""
Interview coach LLM clients (OpenAI-compatible chat completions).

MVP: xAI Grok. Interface allows Groq / Gemini / Claude later.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

XAI_URL = "https://api.x.ai/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class InterviewLLMError(Exception):
    """Raised when the interview coach LLM request fails."""


class InterviewLLM:
    """Minimal chat-completions client."""

    provider_name = "base"

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class OpenAICompatInterviewLLM(InterviewLLM):
    """OpenAI-compatible /v1/chat/completions (Grok, Groq, …)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        url: str,
        provider_name: str = "openai-compat",
        timeout_s: float = 25.0,
    ):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.url = (url or "").strip()
        self.provider_name = provider_name
        self.timeout_s = float(timeout_s or 25.0)

    def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise InterviewLLMError(
                f"{self.provider_name}: API key missing "
                f"(set XAI_API_KEY / GROK_API_KEY for Grok)."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": 0.35,
            "max_tokens": 900,
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
            raise InterviewLLMError(f"{self.provider_name}: network error: {exc}") from exc
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
        return str(content or "").strip()


def _xai_key(cfg) -> str:
    return (
        str(getattr(cfg, "XAI_API_KEY", "") or "").strip()
        or str(getattr(cfg, "GROK_API_KEY", "") or "").strip()
    )


def build_interview_llm(cfg) -> InterviewLLM:
    """
    Build coach LLM from config.

    MVP providers: grok (default), groq.
    gemini/claude → clear error until phase-2 adapters land.
    """
    provider = str(getattr(cfg, "INTERVIEW_COACH_PROVIDER", "grok") or "grok").lower()
    timeout = float(getattr(cfg, "INTERVIEW_COACH_TIMEOUT_S", 25.0) or 25.0)
    model = str(getattr(cfg, "INTERVIEW_COACH_MODEL", "") or "").strip()

    if provider in ("grok", "xai", "x-ai"):
        return OpenAICompatInterviewLLM(
            api_key=_xai_key(cfg),
            model=model or "grok-3-mini",
            url=str(getattr(cfg, "XAI_API_URL", "") or XAI_URL),
            provider_name="grok",
            timeout_s=timeout,
        )
    if provider == "groq":
        return OpenAICompatInterviewLLM(
            api_key=str(getattr(cfg, "GROQ_API_KEY", "") or "").strip(),
            model=model or str(getattr(cfg, "GROQ_MODEL", "") or "llama-3.3-70b-versatile"),
            url=GROQ_URL,
            provider_name="groq",
            timeout_s=timeout,
        )
    if provider in ("gemini", "claude", "anthropic"):
        raise InterviewLLMError(
            f"Provider '{provider}' planned for phase 2 — "
            f"use INTERVIEW_COACH_PROVIDER=grok (or groq) for now."
        )
    raise InterviewLLMError(f"Unknown INTERVIEW_COACH_PROVIDER={provider!r}")


def parse_coach_response(raw: str) -> dict[str, Any]:
    """
    Parse LLM output into {spoken, software_engineer, architect}.

    Prefers JSON; falls back to treating whole text as spoken.
    """
    text = (raw or "").strip()
    empty = {
        "spoken": "",
        "software_engineer": [],
        "architect": [],
    }
    if not text:
        return empty

    # Strip markdown fences if present
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
        # Try first {...} substring
        i, j = body.find("{"), body.rfind("}")
        if i >= 0 and j > i:
            try:
                data = json.loads(body[i : j + 1])
            except Exception:
                data = None

    if not isinstance(data, dict):
        return {
            "spoken": text[:1200],
            "software_engineer": [],
            "architect": [],
        }

    spoken = str(data.get("spoken") or data.get("answer") or "").strip()
    se = data.get("software_engineer") or data.get("se") or []
    arch = data.get("architect") or data.get("architecture") or []
    if isinstance(se, str):
        se = [se]
    if isinstance(arch, str):
        arch = [arch]
    se = [str(x).strip() for x in se if str(x).strip()][:4]
    arch = [str(x).strip() for x in arch if str(x).strip()][:4]
    if not spoken:
        spoken = text[:1200]
    return {
        "spoken": spoken[:1500],
        "software_engineer": se,
        "architect": arch,
    }
