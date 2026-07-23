"""
failover.py
===========
Runtime provider redundancy for STT and translation.

Drop-in wrappers:
  * FailoverTranscriber  — same ``.transcribe(audio)`` as Groq/local Whisper
  * FailoverTranslator   — same ``.translate`` / ``.translate_stream`` as LLM/Google

Design goals (P0):
  * Never block the Textual UI thread (work stays on processor threads).
  * Transient primary failures: ≤1 retry, then secondary.
  * Circuit breaker skips a dead primary for a cooldown window.
  * Permanent errors (401/404) open the circuit without hammering the API.
  * Local Whisper warm-up in a daemon thread; bounded wait only on fallback path.
  * Stream mid-fail → full secondary translate (consistent final text for TTS).
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


def classify_error(exc: BaseException) -> ErrorKind:
    """
    Best-effort classify API/network failures for retry vs circuit policy.

    Inspects type name + message (GroqSTTError / LLMError / TranslationError
    do not always carry HTTP status codes).
    """
    msg = f"{type(exc).__name__}: {exc}".lower()
    # Permanent: bad key / missing model — do not retry-storm.
    if "401" in msg or "rejected the api key" in msg or "unauthorized" in msg:
        return ErrorKind.PERMANENT
    if "403" in msg or "forbidden" in msg:
        return ErrorKind.PERMANENT
    if "404" in msg and "model" in msg:
        return ErrorKind.PERMANENT
    # Transient: rate limit, timeout, network, gateway.
    for token in (
        "429",
        "rate limit",
        "timeout",
        "timed out",
        "connection",
        "network",
        "temporarily",
        "502",
        "503",
        "504",
        "ssl",
        "reset by peer",
        "name resolution",
        "getaddrinfo",
        "max retries",
    ):
        if token in msg:
            return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


class CircuitBreaker:
    """
    closed  → try primary
    open    → skip primary until cooldown
    half_open → allow one probe after cooldown
    """

    __slots__ = (
        "threshold",
        "cooldown_s",
        "_fails",
        "_opened_at",
        "_state",
        "_lock",
        "_permanent",
    )

    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0):
        self.threshold = max(1, int(threshold or 1))
        # Allow sub-second cooldowns (tests / aggressive recover); floor at 0.
        self.cooldown_s = max(
            0.0, float(cooldown_s if cooldown_s is not None else 60.0)
        )
        self._fails = 0
        self._opened_at = 0.0
        self._state = "closed"  # closed | open | half_open
        self._lock = threading.Lock()
        self._permanent = False

    def allow(self) -> bool:
        with self._lock:
            if self._permanent:
                return False
            if self._state == "closed":
                return True
            if self._state == "open":
                if (time.monotonic() - self._opened_at) >= self.cooldown_s:
                    self._state = "half_open"
                    return True
                return False
            # half_open: one probe
            return True

    def success(self) -> None:
        with self._lock:
            self._fails = 0
            self._state = "closed"
            self._permanent = False

    def failure(self, kind: ErrorKind) -> None:
        with self._lock:
            if kind is ErrorKind.PERMANENT:
                self._permanent = True
                self._state = "open"
                self._opened_at = time.monotonic()
                self._fails = self.threshold
                return
            self._fails += 1
            if self._state == "half_open" or self._fails >= self.threshold:
                self._state = "open"
                self._opened_at = time.monotonic()

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "fails": self._fails,
                "permanent": self._permanent,
                "threshold": self.threshold,
                "cooldown_s": self.cooldown_s,
            }


# --------------------------------------------------------------------------- #
# Logging (rate-limited, never raises)
# --------------------------------------------------------------------------- #


class _RateLog:
    def __init__(self, min_interval_s: float = 30.0):
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            prev = self._last.get(key, 0.0)
            if (now - prev) < self.min_interval_s:
                return False
            self._last[key] = now
            return True


_rate = _RateLog(30.0)


def _cfg_flag(cfg, name: str, default):
    try:
        return getattr(cfg, name, default)
    except Exception:
        return default


def _log_event(cfg, message: str, *, level: str = "warn", key: str = "") -> None:
    if not _cfg_flag(cfg, "FAILOVER_LOG", True):
        return
    if key and not _rate.allow(key):
        return
    msg = f"[ha] {message}"
    try:
        from . import ui

        if level == "error":
            ui.error(msg, panel="app")
        elif level == "success":
            ui.success(msg, panel="app")
        elif level == "info":
            ui.info(msg, panel="app")
        elif level == "dim":
            ui.dim(msg, panel="app")
        else:
            ui.warn(msg, panel="app")
    except Exception:
        try:
            print(msg, flush=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# FailoverTranscriber
# --------------------------------------------------------------------------- #


class FailoverTranscriber:
    """
    Primary STT (usually Groq) with automatic fallback to a secondary factory
    (usually local faster-whisper).
    """

    def __init__(
        self,
        primary: Any | None,
        secondary_factory: Callable[[], Any] | None,
        config,
        log: Callable[..., None] = print,
        primary_name: str = "groq",
        secondary_name: str = "local",
    ):
        self.cfg = config
        self.primary = primary
        self._secondary_factory = secondary_factory
        self._secondary: Any | None = None
        self._sec_error: BaseException | None = None
        self._sec_lock = threading.Lock()
        self._sec_ready = threading.Event()
        self._warm_started = False
        self._log = log
        self.primary_name = primary_name
        self.secondary_name = secondary_name
        self._last_backend = primary_name if primary is not None else secondary_name
        self.breaker = CircuitBreaker(
            threshold=int(_cfg_flag(config, "CIRCUIT_FAIL_THRESHOLD", 3)),
            cooldown_s=float(_cfg_flag(config, "CIRCUIT_COOLDOWN_S", 60.0)),
        )
        if primary is None:
            # No primary → never try cloud.
            self.breaker.failure(ErrorKind.PERMANENT)
        # Mirror language attr for pipeline [g] swap (``tr.language = …``).
        self._language = getattr(primary, "language", None) or getattr(
            config, "SOURCE_LANG", None
        )

    # -- language rebind (pipeline sets .language on swap) ----------------- #
    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, value):
        self._language = value
        for eng in (self.primary, self._secondary):
            if eng is not None and hasattr(eng, "language"):
                try:
                    eng.language = value
                except Exception:
                    pass

    def start_warmup(self) -> None:
        """Daemon-load secondary (local Whisper). Safe to call multiple times."""
        if self._secondary_factory is None:
            self._sec_ready.set()
            return
        with self._sec_lock:
            if self._warm_started or self._secondary is not None:
                return
            self._warm_started = True
        t = threading.Thread(
            target=self._warm_worker,
            name="livelingo-stt-warm",
            daemon=True,
        )
        t.start()

    def _warm_worker(self) -> None:
        try:
            self._log(
                f"STT fallback: warming local Whisper "
                f"({getattr(self.cfg, 'WHISPER_MODEL', '?')}) in background…"
            )
        except Exception:
            pass
        try:
            eng = self._secondary_factory()
            with self._sec_lock:
                self._secondary = eng
                self._sec_error = None
            try:
                self._log("STT fallback: local Whisper ready.")
            except Exception:
                pass
        except BaseException as exc:
            with self._sec_lock:
                self._sec_error = exc
            try:
                self._log(f"STT fallback: local Whisper warm-up failed: {exc}")
            except Exception:
                pass
        finally:
            self._sec_ready.set()

    def _get_secondary(self, wait_s: float = 0.0) -> Any | None:
        with self._sec_lock:
            if self._secondary is not None:
                return self._secondary
            if self._sec_error is not None and self._sec_ready.is_set():
                return None
            factory = self._secondary_factory
            warm_started = self._warm_started

        if not warm_started and factory is not None:
            # On-demand load (warmup disabled or not yet started).
            with self._sec_lock:
                if self._secondary is not None:
                    return self._secondary
                if not self._warm_started:
                    self._warm_started = True
                    try:
                        self._secondary = factory()
                        self._sec_error = None
                        self._sec_ready.set()
                        return self._secondary
                    except BaseException as exc:
                        self._sec_error = exc
                        self._sec_ready.set()
                        return None

        if wait_s > 0 and not self._sec_ready.is_set():
            self._sec_ready.wait(timeout=wait_s)

        with self._sec_lock:
            return self._secondary

    def _max_retries(self) -> int:
        return max(0, int(_cfg_flag(self.cfg, "FAILOVER_MAX_RETRIES", 1) or 0))

    def _retry_sleep(self) -> float:
        return max(0.0, float(_cfg_flag(self.cfg, "FAILOVER_RETRY_SLEEP_S", 0.35) or 0))

    def _fallback_wait(self) -> float:
        return max(0.0, float(_cfg_flag(self.cfg, "STT_FALLBACK_WAIT_S", 8.0) or 0))

    def transcribe(self, audio) -> str:
        last_exc: BaseException | None = None

        if self.primary is not None and self.breaker.allow():
            attempts = 1 + self._max_retries()
            for attempt in range(attempts):
                try:
                    text = self.primary.transcribe(audio)
                    was_open = self.breaker.status().get("state") != "closed"
                    self.breaker.success()
                    self._last_backend = self.primary_name
                    if was_open:
                        _log_event(
                            self.cfg,
                            f"STT primary restored ({self.primary_name}).",
                            level="success",
                            key="stt-restored",
                        )
                    return text
                except BaseException as exc:
                    last_exc = exc
                    kind = classify_error(exc)
                    self.breaker.failure(kind)
                    if kind is ErrorKind.PERMANENT:
                        _log_event(
                            self.cfg,
                            f"STT primary permanent error ({self.primary_name}): {exc}",
                            level="error",
                            key="stt-perm",
                        )
                        break
                    if attempt + 1 < attempts and kind in (
                        ErrorKind.TRANSIENT,
                        ErrorKind.UNKNOWN,
                    ):
                        time.sleep(self._retry_sleep())
                        continue
                    _log_event(
                        self.cfg,
                        f"STT primary failed ({self.primary_name}): {exc} → "
                        f"trying {self.secondary_name}",
                        level="warn",
                        key="stt-fallback",
                    )
                    break

        if self._secondary_factory is None and self._secondary is None:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("STT unavailable: no primary and no fallback configured")

        sec = self._get_secondary(wait_s=self._fallback_wait())
        if sec is None:
            err = self._sec_error or last_exc
            if err is not None:
                raise RuntimeError(
                    f"STT unavailable (primary down, fallback not ready): {err}"
                ) from err
            raise RuntimeError(
                "STT unavailable (primary down, fallback still loading — retry next chunk)"
            )

        # Align language before local call.
        if self._language is not None and hasattr(sec, "language"):
            try:
                sec.language = self._language
            except Exception:
                pass
        try:
            text = sec.transcribe(audio)
        except BaseException as exc:
            if last_exc is not None:
                raise RuntimeError(
                    f"STT failed on primary and fallback: primary={last_exc!s}; "
                    f"fallback={exc!s}"
                ) from exc
            raise
        self._last_backend = self.secondary_name
        return text

    def status(self) -> dict:
        return {
            "primary": self.primary_name if self.primary is not None else None,
            "secondary": self.secondary_name if self._secondary_factory else None,
            "last_backend": self._last_backend,
            "secondary_ready": self._secondary is not None,
            "breaker": self.breaker.status(),
        }

    @property
    def active_backend(self) -> str:
        return str(self._last_backend or "")


# --------------------------------------------------------------------------- #
# FailoverTranslator
# --------------------------------------------------------------------------- #


class FailoverTranslator:
    """
    Primary translator (usually Groq LLM) with automatic Google (or other) fallback.
    """

    def __init__(
        self,
        primary: Any | None,
        secondary: Any | None,
        config,
        log: Callable[..., None] = print,
        primary_name: str = "llm",
        secondary_name: str = "google",
    ):
        self.cfg = config
        self.primary = primary
        self.secondary = secondary
        self._log = log
        self.primary_name = primary_name
        self.secondary_name = secondary_name
        self._last_backend = primary_name if primary is not None else secondary_name
        self.breaker = CircuitBreaker(
            threshold=int(_cfg_flag(config, "CIRCUIT_FAIL_THRESHOLD", 3)),
            cooldown_s=float(_cfg_flag(config, "CIRCUIT_COOLDOWN_S", 60.0)),
        )
        if primary is None:
            self.breaker.failure(ErrorKind.PERMANENT)

    def _max_retries(self) -> int:
        return max(0, int(_cfg_flag(self.cfg, "FAILOVER_MAX_RETRIES", 1) or 0))

    def _retry_sleep(self) -> float:
        return max(0.0, float(_cfg_flag(self.cfg, "FAILOVER_RETRY_SLEEP_S", 0.35) or 0))

    def set_language_pair(self, source=None, target=None):
        for eng in (self.primary, self.secondary):
            if eng is None:
                continue
            if hasattr(eng, "set_language_pair"):
                try:
                    eng.set_language_pair(source, target)
                except Exception:
                    pass
            elif hasattr(eng, "refresh_prompt"):
                try:
                    eng.refresh_prompt()
                except Exception:
                    pass

    def refresh_prompt(self):
        for eng in (self.primary, self.secondary):
            if eng is not None and hasattr(eng, "refresh_prompt"):
                try:
                    eng.refresh_prompt()
                except Exception:
                    pass

    def explain_synonyms(self, word):
        """Delegate to LLM primary when present (used by synonym [o] llm mode)."""
        if self.primary is not None and hasattr(self.primary, "explain_synonyms"):
            return self.primary.explain_synonyms(word)
        if self.secondary is not None and hasattr(self.secondary, "explain_synonyms"):
            return self.secondary.explain_synonyms(word)
        raise AttributeError("No translator backend supports explain_synonyms")

    def generate_meeting_summary(self, *args, **kwargs):
        """Pass-through for export summary when primary is LLMTranslator."""
        if self.primary is not None and hasattr(
            self.primary, "generate_meeting_summary"
        ):
            return self.primary.generate_meeting_summary(*args, **kwargs)
        if self.secondary is not None and hasattr(
            self.secondary, "generate_meeting_summary"
        ):
            return self.secondary.generate_meeting_summary(*args, **kwargs)
        raise AttributeError("No translator backend supports generate_meeting_summary")

    def _try_primary(self, fn: Callable[[], str], *, stream: bool) -> Optional[str]:
        """Run primary with retries; return text or None if should use secondary."""
        if self.primary is None or not self.breaker.allow():
            return None
        attempts = 1 + self._max_retries()
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                text = fn()
                was_degraded = self.breaker.status().get("state") != "closed"
                self.breaker.success()
                self._last_backend = self.primary_name
                if was_degraded:
                    _log_event(
                        self.cfg,
                        f"Translation primary restored ({self.primary_name}).",
                        level="success",
                        key="tr-restored",
                    )
                return text
            except BaseException as exc:
                last_exc = exc
                kind = classify_error(exc)
                self.breaker.failure(kind)
                if kind is ErrorKind.PERMANENT:
                    _log_event(
                        self.cfg,
                        f"Translation primary permanent error "
                        f"({self.primary_name}): {exc}",
                        level="error",
                        key="tr-perm",
                    )
                    break
                if attempt + 1 < attempts and kind in (
                    ErrorKind.TRANSIENT,
                    ErrorKind.UNKNOWN,
                ):
                    time.sleep(self._retry_sleep())
                    continue
                _log_event(
                    self.cfg,
                    f"Translation primary failed ({self.primary_name}"
                    f"{' stream' if stream else ''}): {exc} → "
                    f"trying {self.secondary_name}",
                    level="warn",
                    key="tr-fallback",
                )
                break
        self._primary_last_exc = last_exc
        return None

    def translate(self, text: str) -> str:
        self._primary_last_exc = None
        text = (text or "").strip()
        if not text:
            return ""

        def _pri():
            return self.primary.translate(text)

        out = self._try_primary(_pri, stream=False)
        if out is not None:
            return out

        if self.secondary is None:
            exc = getattr(self, "_primary_last_exc", None)
            if exc is not None:
                raise exc
            raise RuntimeError(
                "Translation unavailable: no primary and no fallback configured"
            )

        try:
            result = self.secondary.translate(text)
        except BaseException as exc:
            prev = getattr(self, "_primary_last_exc", None)
            if prev is not None:
                raise RuntimeError(
                    f"Translation failed on primary and fallback: "
                    f"primary={prev!s}; fallback={exc!s}"
                ) from exc
            raise
        self._last_backend = self.secondary_name
        return result

    def translate_stream(self, text: str, on_token=None) -> str:
        """
        Prefer primary streaming; on any failure before/after tokens, fall back
        to secondary full translate and emit one final on_token(full) (policy A).
        """
        self._primary_last_exc = None
        text = (text or "").strip()
        if not text:
            return ""

        if (
            self.primary is not None
            and self.breaker.allow()
            and hasattr(self.primary, "translate_stream")
        ):

            def _pri_stream():
                return self.primary.translate_stream(text, on_token=on_token)

            out = self._try_primary(_pri_stream, stream=True)
            if out is not None:
                return out
        elif self.primary is not None and self.breaker.allow():
            # Primary without stream support.
            out = self._try_primary(lambda: self.primary.translate(text), stream=False)
            if out is not None:
                if on_token:
                    try:
                        on_token(out)
                    except Exception:
                        pass
                return out

        if self.secondary is None:
            exc = getattr(self, "_primary_last_exc", None)
            if exc is not None:
                raise exc
            raise RuntimeError(
                "Translation unavailable: no primary and no fallback configured"
            )

        try:
            if hasattr(self.secondary, "translate_stream"):
                result = self.secondary.translate_stream(text, on_token=on_token)
            else:
                result = self.secondary.translate(text)
                if on_token and result is not None:
                    try:
                        on_token(result)
                    except Exception:
                        pass
        except BaseException as exc:
            prev = getattr(self, "_primary_last_exc", None)
            if prev is not None:
                raise RuntimeError(
                    f"Translation failed on primary and fallback: "
                    f"primary={prev!s}; fallback={exc!s}"
                ) from exc
            raise
        self._last_backend = self.secondary_name
        return result

    def status(self) -> dict:
        return {
            "primary": self.primary_name if self.primary is not None else None,
            "secondary": self.secondary_name if self.secondary is not None else None,
            "last_backend": self._last_backend,
            "breaker": self.breaker.status(),
        }

    @property
    def active_backend(self) -> str:
        return str(self._last_backend or "")


# --------------------------------------------------------------------------- #
# Helpers for main / help status lines
# --------------------------------------------------------------------------- #


def translator_uses_llm(translator) -> bool:
    if translator is None:
        return False
    try:
        from .llm import LLMTranslator
    except Exception:
        LLMTranslator = ()  # type: ignore
    if isinstance(translator, LLMTranslator):
        return True
    if isinstance(translator, FailoverTranslator):
        return translator.primary is not None and isinstance(
            translator.primary, LLMTranslator
        )
    return False


def transcriber_uses_groq(transcriber) -> bool:
    if transcriber is None:
        return False
    try:
        from .groq_transcribe import GroqTranscriber
    except Exception:
        GroqTranscriber = ()  # type: ignore
    if isinstance(transcriber, GroqTranscriber):
        return True
    if isinstance(transcriber, FailoverTranscriber):
        return transcriber.primary is not None and isinstance(
            transcriber.primary, GroqTranscriber
        )
    return False
