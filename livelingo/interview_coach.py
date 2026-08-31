"""
Interview Coach — suggest assertive EN answers from LiveCaptions questions.

Triggered on stable LC captions that look like interview questions.
Runs LLM in a background thread; never blocks LC translate / VOZ pipeline.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .interview_llm import (
    InterviewLLM,
    InterviewLLMError,
    build_interview_llm,
    parse_coach_response,
)

_QUESTION_STARTERS = re.compile(
    r"^\s*("
    r"can you|could you|would you|will you|how (?:do|would|did|can|could|have)|"
    r"what (?:is|are|was|were|do|does|did|would|should|about)|"
    r"why (?:do|did|would|is|are)|"
    r"when (?:do|did|would|have)|"
    r"where (?:do|did|have)|"
    r"who (?:do|did|have|is)|"
    r"tell me|describe|explain|walk me through|walk us through|"
    r"have you|do you|are you|did you|give me|share (?:an?|your)|"
    r"talk about|speak about|what's your|what is your|what are your|"
    r"how would you|how have you|in your experience"
    r")\b",
    re.I,
)

_SMALLTALK = re.compile(
    r"^\s*(thanks|thank you|ok|okay|alright|got it|sure|yeah|yep|hi|hello|"
    r"good morning|good afternoon|nice to meet you|one (?:sec|second|moment))\b",
    re.I,
)


def is_interview_question(text: str, *, min_chars: int = 40) -> bool:
    """Heuristic: clear question / ask for experience or opinion."""
    t = " ".join((text or "").split())
    if not t:
        return False
    if len(t) < max(8, int(min_chars or 40)):
        # Short but explicit "?" still counts if long enough for a real Q
        if t.endswith("?") and len(t) >= 20:
            pass
        else:
            return False
    if _SMALLTALK.match(t) and len(t) < 60:
        return False
    if t.rstrip().endswith("?"):
        return True
    if _QUESTION_STARTERS.search(t):
        return True
    return False


def _norm_key(text: str) -> str:
    t = " ".join((text or "").lower().split())
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


SYSTEM_PROMPT = """You are a senior Software Engineer and Systems Architect preparing for a live job interview.
The candidate will speak your answer aloud in English (~30–60 seconds).

The interviewer's question may be in **English or Portuguese**. Always understand the intent,
then produce the spoken answer **only in English**.

Tone: assertive, confident, concrete. No apology openers ("Well…", "I think maybe…").
Prefer outcome + ownership + trade-offs. Avoid buzzword salad.

Return ONLY valid JSON (no markdown fences) with this shape:
{
  "spoken": "4-8 sentences the candidate can say aloud in English",
  "software_engineer": ["up to 4 short bullets — coding/delivery angle"],
  "architect": ["up to 4 short bullets — systems/architecture angle"]
}
"""


def _build_user_prompt(
    question_en: str,
    *,
    question_pt: str = "",
    profile: str = "",
    simulated: bool = False,
) -> str:
    label = "Interviewer's question (EN or PT)"
    if simulated:
        label += " [manual / airespond test — treat like LiveCaptions]"
    parts = [f"{label}:\n{question_en.strip()}"]
    if (question_pt or "").strip() and question_pt.strip() != question_en.strip():
        parts.append(f"Portuguese gloss (context only):\n{question_pt.strip()}")
    if (profile or "").strip():
        parts.append(f"Candidate profile (use if relevant):\n{profile.strip()}")
    parts.append("Produce the JSON answer now.")
    return "\n\n".join(parts)


@dataclass
class CoachResult:
    n: int
    question: str
    spoken: str
    software_engineer: List[str] = field(default_factory=list)
    architect: List[str] = field(default_factory=list)
    provider: str = ""
    error: str = ""


class InterviewCoach:
    """Orchestrates question detection + async LLM coaching."""

    def __init__(self, config, llm: Optional[InterviewLLM] = None):
        self.cfg = config
        self._llm = llm
        self._lock = threading.Lock()
        self._enabled = bool(getattr(config, "INTERVIEW_COACH_ENABLED", False))
        self._seen: set[str] = set()
        self._last: Optional[CoachResult] = None
        self._last_lc_en: str = ""
        self._last_lc_pt: str = ""
        self._last_lc_n: int = 0
        self._sim_n: int = 9000  # fake LC ids for airespond / coach ask
        self._gen = 0
        self._busy = False
        # When set, next _run_job marks the prompt as simulated LC
        self._next_simulated: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    def set_enabled(self, on: bool) -> bool:
        self._enabled = bool(on)
        return self._enabled

    def status(self) -> dict[str, Any]:
        provider = str(getattr(self.cfg, "INTERVIEW_COACH_PROVIDER", "grok") or "grok")
        model = str(getattr(self.cfg, "INTERVIEW_COACH_MODEL", "") or "")
        key_ok = False
        try:
            llm = self._ensure_llm()
            key_ok = bool(getattr(llm, "api_key", ""))
            provider = getattr(llm, "provider_name", provider)
            model = getattr(llm, "model", model)
        except Exception:
            pass
        last_q = (self._last.question[:80] if self._last else "") or ""
        return {
            "enabled": self._enabled,
            "provider": provider,
            "model": model,
            "key_ok": key_ok,
            "busy": self._busy,
            "last_question": last_q,
            "mode": str(getattr(self.cfg, "INTERVIEW_QUESTION_MODE", "auto") or "auto"),
        }

    def remember_lc(self, n: int, en: str, pt: str = "") -> None:
        """Track latest stable LC pair for F7 / coach last."""
        with self._lock:
            self._last_lc_n = int(n or 0)
            self._last_lc_en = (en or "").strip()
            self._last_lc_pt = (pt or "").strip()

    def last_result(self) -> Optional[CoachResult]:
        return self._last

    def _ensure_llm(self) -> InterviewLLM:
        if self._llm is None:
            self._llm = build_interview_llm(self.cfg)
        return self._llm

    def maybe_handle(
        self,
        n: int,
        question_en: str,
        question_pt: str = "",
        *,
        force: bool = False,
    ) -> bool:
        """
        Optionally start a coach job for this LC caption.

        Returns True if a job was scheduled.
        """
        en = (question_en or "").strip()
        pt = (question_pt or "").strip()
        self.remember_lc(n, en, pt)
        if not self._enabled and not force:
            return False
        if not en:
            return False

        mode = str(getattr(self.cfg, "INTERVIEW_QUESTION_MODE", "auto") or "auto").lower()
        min_chars = int(getattr(self.cfg, "INTERVIEW_MIN_CHARS", 40) or 40)
        if not force:
            if mode == "manual":
                return False
            if mode == "auto" and not is_interview_question(en, min_chars=min_chars):
                return False
            # mode == always → every stable caption (still respects min via detector soft)

        key = _norm_key(en)
        with self._lock:
            if not force and key in self._seen:
                return False
            self._seen.add(key)
            # Bound memory
            if len(self._seen) > 200:
                self._seen = set(list(self._seen)[-100:])
            self._gen += 1
            gen = self._gen
            self._busy = True

        thread = threading.Thread(
            target=self._run_job,
            args=(gen, int(n or 0), en, pt),
            name="interview-coach",
            daemon=True,
        )
        thread.start()
        return True

    def force_last(self) -> tuple[bool, str]:
        """F7 / coach force — re-run on last LC EN."""
        with self._lock:
            en = self._last_lc_en
            pt = self._last_lc_pt
            n = self._last_lc_n
        if not en:
            return False, "Nenhum LC estável ainda — aguarde uma caption."
        if not self._enabled:
            self._enabled = True
        ok = self.maybe_handle(n, en, pt, force=True)
        if ok:
            return True, f"Coach forçado no LC {n or '?'}…"
        return False, "Não agendou coach (texto vazio?)."

    def ask(
        self,
        text: str,
        *,
        simulate_lc: bool = False,
        translated_pt: str = "",
    ) -> tuple[bool, str]:
        """
        Manual question: ``coach ask …`` / ``airespond …``.

        ``simulate_lc=True`` (airespond): mirror an LC pair into the LC panel
        then run the same coach path as a stable LiveCaptions question.
        Accepts PT or EN — spoken answer is always English.
        """
        q = (text or "").strip()
        if not q:
            return False, "Uso: airespond <pergunta>  ou  coach ask <pergunta>"
        if not self._enabled:
            self._enabled = True

        with self._lock:
            self._sim_n += 1
            n = int(self._sim_n)
            self._next_simulated = True

        pt = (translated_pt or "").strip()
        if simulate_lc:
            try:
                from . import ui

                # Caption = typed question (as LC would show EN/source);
                # Translated = PT gloss or marker so the LC column looks real.
                tgt = pt or "[simulado · airespond]"
                ui.live_caption_block(n, q, tgt, from_cache=None)
                ui.dim(
                    f"[airespond] LC simulado #{n} → Coach…",
                    panel="app",
                )
            except Exception:
                pass

        ok = self.maybe_handle(n, q, pt, force=True)
        if ok:
            return True, f"airespond/Coach agendado (#{n}) — aguarde o painel Coach."
        return False, "Falha ao agendar coach."

    def _run_job(self, gen: int, n: int, en: str, pt: str) -> None:
        from . import ui

        try:
            ui.dim(
                f"[Coach] gerando resposta EN · Q={en[:70]}…",
                panel="app",
            )
        except Exception:
            pass
        try:
            ui.info(f"[Coach {n}] Thinking…", panel="coach")
        except Exception:
            pass

        err = ""
        parsed = {
            "spoken": "",
            "software_engineer": [],
            "architect": [],
        }
        provider = ""
        simulated = False
        try:
            with self._lock:
                simulated = bool(self._next_simulated)
                self._next_simulated = False
            llm = self._ensure_llm()
            provider = getattr(llm, "provider_name", "") or ""
            profile = str(getattr(self.cfg, "INTERVIEW_CANDIDATE_PROFILE", "") or "")
            raw = llm.complete(
                SYSTEM_PROMPT,
                _build_user_prompt(
                    en,
                    question_pt=pt,
                    profile=profile,
                    simulated=simulated,
                ),
            )
            parsed = parse_coach_response(raw)
        except InterviewLLMError as exc:
            err = str(exc)
        except Exception as exc:
            err = f"coach failed: {exc}"

        with self._lock:
            if gen != self._gen:
                # Superseded by a newer question
                return
            result = CoachResult(
                n=n,
                question=en,
                spoken=parsed.get("spoken") or "",
                software_engineer=list(parsed.get("software_engineer") or []),
                architect=list(parsed.get("architect") or []),
                provider=provider,
                error=err,
            )
            self._last = result
            self._busy = False

        try:
            if err:
                ui.warn(f"[Coach {n}] {err}", panel="coach")
                ui.warn(f"[Coach] {err}", panel="app")
            else:
                ui.coach_block(
                    n,
                    en,
                    result.spoken,
                    result.software_engineer,
                    result.architect,
                    provider=provider,
                )
        except Exception:
            try:
                ui.error(f"[Coach {n}] UI emit failed", panel="app")
            except Exception:
                pass


def build_interview_coach(config) -> InterviewCoach:
    return InterviewCoach(config)
