"""
Interview Coach — suggest assertive EN answers from LiveCaptions questions.

Triggered on stable LC captions that look like interview questions.
Runs LLM in a background thread; never blocks LC translate / VOZ pipeline.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from .interview_llm import (
    InterviewLLM,
    InterviewLLMError,
    build_interview_llm,
    list_interview_providers,
    parse_coach_response,
)

# Discovered relative to process CWD (project root when launching main.py).
_DEFAULT_CONTEXT_CANDIDATES = (
    "COACH.md",
    "AGENT.md",
    os.path.join(".livelingo", "coach-context.md"),
)

# Not anchored at ^ — LiveCaptions often prefixes "Specifically,", "So,",
# "Question five,", etc. before the real interrogative.
_QUESTION_STARTERS = re.compile(
    r"(?:"
    r"can you|could you|would you|will you|"
    r"how (?:do|would|did|can|could|have|are|is)|"
    r"what (?:is|are|was|were|do|does|did|would|should|about|tool|strategy|"
    r"approach|library|framework)|"
    r"which (?:tool|library|approach|strategy|one)|"
    r"why (?:do|did|would|is|are)|"
    r"when (?:do|did|would|have)|"
    r"where (?:do|did|have)|"
    r"who (?:do|did|have|is)|"
    r"tell me|describe|explain|walk me through|walk us through|"
    r"have you|do you|are you|did you|give me|share (?:an?|your)|"
    r"talk about|speak about|what's your|what is your|what are your|"
    r"how would you|how have you|in your experience|"
    r"do you (?:use|handle|keep|ensure|manage|prefer|think|convert|sync)"
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
        if "?" in t and len(t) >= 20:
            pass
        else:
            return False
    if _SMALLTALK.match(t) and len(t) < 60:
        return False
    # LC often drops the trailing "?" — still accept if mark appears anywhere
    if "?" in t:
        return True
    if _QUESTION_STARTERS.search(t):
        return True
    return False


def _norm_key(text: str) -> str:
    t = " ".join((text or "").lower().split())
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


SYSTEM_PROMPT = """You are a senior Software Engineer and Systems Architect preparing for a live job interview.
The candidate will speak your answer aloud in English (~45–75 seconds).

CRITICAL LANGUAGE RULES:
- The interviewer's question may be in English OR Portuguese (including via manual airespond).
- Fields spoken, software_engineer, architect, tradeoffs MUST be written in **English only**.
- Fields spoken_pt, software_engineer_pt, architect_pt, tradeoffs_pt MUST be natural **Brazilian Portuguese (pt-BR)** translations of the English fields (same meaning, not literal word salad).
- Never put Portuguese into the English fields, even if the question was in Portuguese.

Tone: assertive, confident, concrete. No apology openers ("Well…", "I think maybe…").
Prefer outcome + ownership + trade-offs. Avoid buzzword salad.

JOB / CANDIDATE CONTEXT (when provided below):
- Treat the context block as ground truth for the role (stack, seniority, domain).
- Prefer technologies and practices named there (e.g. Java, Spring Boot, Angular)
  when giving examples and trade-offs — do not invent unrelated stacks.
- Keep answers honest to that profile; still sound like live interview speech.

EXAMPLES (mandatory in spoken + spoken_pt):
- Weave in 1–2 **simple, concrete examples** tied to the question topic (a small system, incident, migration, API, queue, cache, DB choice, etc.).
- Phrase them as lived experience the candidate can own — e.g. "In one service I …", "For example, when we …", "On a past project …" — so it sounds like they know how to explain with examples, not only theory.
- Keep examples short and speakable (one beat each). Prefer plausible generic scenarios over named employers or fake metrics.
- Mirror the same examples in spoken_pt (natural pt-BR, same stories).
- SE/Arch bullets may reference those examples briefly; do not dump a long case study.

Return ONLY valid JSON (no markdown fences) with this shape:
{
  "spoken": "5-9 sentences the candidate can say aloud in English, including 1–2 simple topic examples",
  "software_engineer": ["up to 4 short bullets — coding/delivery angle, English"],
  "architect": ["up to 4 short bullets — systems/architecture angle, English"],
  "tradeoffs": "One clear paragraph in English explaining main trade-offs (pros vs cons, when A vs B).",
  "spoken_pt": "Same spoken answer in Brazilian Portuguese (pt-BR), including the same examples",
  "software_engineer_pt": ["same SE bullets in pt-BR"],
  "architect_pt": ["same Arch bullets in pt-BR"],
  "tradeoffs_pt": "Same trade-offs paragraph in Brazilian Portuguese (pt-BR)"
}
"""


def resolve_coach_context_path(config=None, *, cwd: str | None = None) -> Optional[Path]:
    """
    Resolve markdown context file path.

    Order: INTERVIEW_COACH_CONTEXT_FILE → COACH.md → AGENT.md →
    .livelingo/coach-context.md (first that exists).
    """
    root = Path(cwd or os.getcwd()).resolve()
    explicit = ""
    if config is not None:
        explicit = str(
            getattr(config, "INTERVIEW_COACH_CONTEXT_FILE", "") or ""
        ).strip()
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit).expanduser()
        candidates.append(p if p.is_absolute() else (root / p))
    for name in _DEFAULT_CONTEXT_CANDIDATES:
        candidates.append(root / name)
    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                return p.resolve()
        except Exception:
            continue
    return None


def load_coach_context(
    config=None, *, cwd: str | None = None, max_chars: int = 12000
) -> Tuple[str, Optional[Path]]:
    """
    Load interview context markdown from disk.

    Returns (text, path_or_None). Empty text if no file found.
    """
    path = resolve_coach_context_path(config, cwd=cwd)
    if path is None:
        return "", None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", path
    text = (raw or "").strip()
    if not text:
        return "", path
    limit = max(500, int(max_chars or 12000))
    if len(text) > limit:
        text = text[: limit - 20].rstrip() + "\n…[truncated]"
    return text, path


def _build_user_prompt(
    question_en: str,
    *,
    question_pt: str = "",
    profile: str = "",
    context: str = "",
    context_path: str = "",
    simulated: bool = False,
) -> str:
    label = "Interviewer's question (EN or PT)"
    if simulated:
        label += " [manual / airespond test — treat like LiveCaptions]"
    parts = [f"{label}:\n{question_en.strip()}"]
    if (question_pt or "").strip() and question_pt.strip() != question_en.strip():
        parts.append(f"Portuguese gloss (context only):\n{question_pt.strip()}")
    ctx = (context or "").strip()
    if ctx:
        src = (context_path or "COACH.md").strip()
        parts.append(
            f"Job / interview context (from {src} — follow this stack/role "
            f"in every answer):\n{ctx}"
        )
    if (profile or "").strip():
        parts.append(
            f"Candidate profile notes (.env INTERVIEW_CANDIDATE_PROFILE):\n"
            f"{profile.strip()}"
        )
    parts.append(
        "Produce the JSON answer now. "
        "Remember: English fields = English only; *_pt fields = Brazilian Portuguese. "
        "If the question is in Portuguese, still write spoken/SE/Arch/tradeoffs in English. "
        "Align examples and trade-offs with the job/interview context when present. "
        "In spoken and spoken_pt, include 1–2 simple examples about this topic "
        "that make the candidate sound experienced and ready to explain with examples."
    )
    return "\n\n".join(parts)


@dataclass
class CoachResult:
    n: int
    question: str
    spoken: str
    software_engineer: List[str] = field(default_factory=list)
    architect: List[str] = field(default_factory=list)
    tradeoffs: str = ""
    spoken_pt: str = ""
    software_engineer_pt: List[str] = field(default_factory=list)
    architect_pt: List[str] = field(default_factory=list)
    tradeoffs_pt: str = ""
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
        # Cached COACH.md / AGENT.md body (reload via coach context reload)
        self._context_text: str = ""
        self._context_path: Optional[Path] = None
        self._context_mtime: float = 0.0
        self.reload_context(quiet=True)

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    def set_enabled(self, on: bool) -> bool:
        self._enabled = bool(on)
        return self._enabled

    def reload_context(self, *, quiet: bool = False) -> tuple[bool, str]:
        """Reload COACH.md / AGENT.md (or INTERVIEW_COACH_CONTEXT_FILE)."""
        text, path = load_coach_context(self.cfg)
        self._context_text = text
        self._context_path = path
        try:
            self._context_mtime = path.stat().st_mtime if path else 0.0
        except Exception:
            self._context_mtime = 0.0
        if path is None:
            msg = (
                "Coach context: nenhum arquivo "
                "(crie COACH.md ou AGENT.md na raiz do projeto)."
            )
            if not quiet:
                return False, msg
            return False, msg
        msg = (
            f"Coach context: {path.name} · {len(text)} chars"
            + (" · vazio" if not text else "")
        )
        return True, msg

    def context_status(self) -> dict[str, Any]:
        path = self._context_path
        return {
            "path": str(path) if path else "",
            "name": path.name if path else "",
            "chars": len(self._context_text or ""),
            "preview": (self._context_text or "")[:240],
        }

    def _ensure_context_fresh(self) -> str:
        """Auto-reload if the markdown file changed on disk."""
        path = self._context_path or resolve_coach_context_path(self.cfg)
        if path is None:
            # Maybe file was created after boot
            self.reload_context(quiet=True)
            return self._context_text or ""
        try:
            mtime = path.stat().st_mtime
        except Exception:
            return self._context_text or ""
        if abs(mtime - float(self._context_mtime or 0.0)) > 0.01:
            self.reload_context(quiet=True)
        return self._context_text or ""

    def set_provider(self, provider: str, model: str = "") -> tuple[bool, str]:
        """
        Switch LLM provider at runtime (``coach provider claude``).

        Rebuilds the client; next ask/airespond uses the new backend.
        """
        prov = (provider or "").strip().lower()
        aliases = {
            "xai": "grok",
            "x-ai": "grok",
            "anthropic": "claude",
            "google": "gemini",
            "google-ai": "gemini",
        }
        prov = aliases.get(prov, prov)
        allowed = list_interview_providers()
        if prov not in allowed:
            return (
                False,
                f"Provider inválido: {provider!r}. Use: {', '.join(allowed)}",
            )
        try:
            setattr(self.cfg, "INTERVIEW_COACH_PROVIDER", prov)
            if (model or "").strip():
                setattr(self.cfg, "INTERVIEW_COACH_MODEL", model.strip())
            self._llm = None  # force rebuild
            llm = self._ensure_llm()
            key_ok = bool(getattr(llm, "api_key", ""))
            msg = (
                f"Coach provider={llm.provider_name} · model={llm.model} · "
                f"key_ok={key_ok}"
            )
            if not key_ok:
                msg += " — configure a API key no .env"
            return True, msg
        except InterviewLLMError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"Falha ao trocar provider: {exc}"

    def status(self) -> dict[str, Any]:
        provider = str(getattr(self.cfg, "INTERVIEW_COACH_PROVIDER", "grok") or "grok")
        model = str(getattr(self.cfg, "INTERVIEW_COACH_MODEL", "") or "")
        key_ok = False
        try:
            llm = self._ensure_llm()
            key_ok = bool(getattr(llm, "api_key", ""))
            provider = getattr(llm, "provider_name", provider)
            model = getattr(llm, "model", model) or model
        except Exception:
            pass
        last_q = (self._last.question[:80] if self._last else "") or ""
        ctx = self.context_status()
        return {
            "enabled": self._enabled,
            "provider": provider,
            "model": model,
            "key_ok": key_ok,
            "busy": self._busy,
            "last_question": last_q,
            "mode": str(getattr(self.cfg, "INTERVIEW_QUESTION_MODE", "auto") or "auto"),
            "providers": list_interview_providers(),
            "context_file": ctx.get("name") or "",
            "context_chars": ctx.get("chars") or 0,
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
            "tradeoffs": "",
            "spoken_pt": "",
            "software_engineer_pt": [],
            "architect_pt": [],
            "tradeoffs_pt": "",
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
            context = self._ensure_context_fresh()
            ctx_path = ""
            try:
                ctx_path = (
                    self._context_path.name if self._context_path is not None else ""
                )
            except Exception:
                ctx_path = ""
            raw = llm.complete(
                SYSTEM_PROMPT,
                _build_user_prompt(
                    en,
                    question_pt=pt,
                    profile=profile,
                    context=context,
                    context_path=ctx_path,
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
                tradeoffs=str(parsed.get("tradeoffs") or ""),
                spoken_pt=str(parsed.get("spoken_pt") or ""),
                software_engineer_pt=list(parsed.get("software_engineer_pt") or []),
                architect_pt=list(parsed.get("architect_pt") or []),
                tradeoffs_pt=str(parsed.get("tradeoffs_pt") or ""),
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
                    tradeoffs=result.tradeoffs,
                    spoken_pt=result.spoken_pt,
                    software_engineer_pt=result.software_engineer_pt,
                    architect_pt=result.architect_pt,
                    tradeoffs_pt=result.tradeoffs_pt,
                    provider=provider,
                )
        except Exception:
            try:
                ui.error(f"[Coach {n}] UI emit failed", panel="app")
            except Exception:
                pass


def build_interview_coach(config) -> InterviewCoach:
    return InterviewCoach(config)
