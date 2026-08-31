"""
Interview coach LLM clients (OpenAI-compatible chat completions).

MVP: xAI Grok. Interface allows Groq / Gemini / Claude later.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

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
            model=model
            or str(getattr(cfg, "GROQ_MODEL", "") or "llama-3.3-70b-versatile"),
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
    se_pt = _as_str_list(
        data.get("software_engineer_pt") or data.get("se_pt") or []
    )
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
