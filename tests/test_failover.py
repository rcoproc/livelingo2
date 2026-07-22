"""
Minimal unit tests for livelingo.failover (no network, no Whisper load).

Covers: error classification, circuit breaker, STT/translation wrappers,
stream policy A (secondary full replace), and helper type checks.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from livelingo.failover import (
    CircuitBreaker,
    ErrorKind,
    FailoverTranscriber,
    FailoverTranslator,
    classify_error,
    transcriber_uses_groq,
    translator_uses_llm,
)


@pytest.fixture
def ha_cfg(mock_cfg):
    """mock_cfg + HA knobs used by Failover* wrappers."""
    return SimpleNamespace(
        **vars(mock_cfg),
        FAILOVER_MAX_RETRIES=0,
        FAILOVER_RETRY_SLEEP_S=0.0,
        CIRCUIT_FAIL_THRESHOLD=2,
        CIRCUIT_COOLDOWN_S=60.0,
        STT_FALLBACK_WAIT_S=0.5,
        STT_WARMUP_LOCAL=False,
        FAILOVER_LOG=False,
    )


# --------------------------------------------------------------------------- #
# classify_error
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "msg,kind",
    [
        ("Groq rejected the API key (401)", ErrorKind.PERMANENT),
        ("unauthorized", ErrorKind.PERMANENT),
        ("model 'x' not found (404)", ErrorKind.PERMANENT),
        ("rate limit reached (429)", ErrorKind.TRANSIENT),
        ("network error contacting Groq", ErrorKind.TRANSIENT),
        ("HTTPSConnectionPool timeout", ErrorKind.TRANSIENT),
        ("NameResolutionError getaddrinfo failed", ErrorKind.TRANSIENT),
        ("max retries exceeded", ErrorKind.TRANSIENT),
        ("something weird happened", ErrorKind.UNKNOWN),
    ],
)
def test_classify_error(msg, kind):
    assert classify_error(RuntimeError(msg)) is kind


# --------------------------------------------------------------------------- #
# CircuitBreaker
# --------------------------------------------------------------------------- #


def test_circuit_opens_after_threshold():
    b = CircuitBreaker(threshold=2, cooldown_s=60.0)
    assert b.allow() is True
    b.failure(ErrorKind.TRANSIENT)
    assert b.allow() is True
    b.failure(ErrorKind.TRANSIENT)
    assert b.allow() is False
    assert b.status()["state"] == "open"


def test_circuit_half_open_after_cooldown_then_success():
    b = CircuitBreaker(threshold=1, cooldown_s=0.05)
    b.failure(ErrorKind.TRANSIENT)
    assert b.allow() is False
    time.sleep(0.08)
    assert b.allow() is True  # half_open probe
    assert b.status()["state"] == "half_open"
    b.success()
    assert b.allow() is True
    assert b.status()["state"] == "closed"
    assert b.status()["fails"] == 0


def test_circuit_permanent_never_allows():
    b = CircuitBreaker(threshold=5, cooldown_s=0.01)
    b.failure(ErrorKind.PERMANENT)
    assert b.allow() is False
    time.sleep(0.03)
    assert b.allow() is False  # permanent stays closed to primary
    assert b.status()["permanent"] is True


def test_circuit_half_open_fail_reopens():
    b = CircuitBreaker(threshold=1, cooldown_s=0.05)
    b.failure(ErrorKind.TRANSIENT)
    time.sleep(0.08)
    assert b.allow() is True
    b.failure(ErrorKind.TRANSIENT)
    assert b.allow() is False
    assert b.status()["state"] == "open"


# --------------------------------------------------------------------------- #
# FailoverTranslator
# --------------------------------------------------------------------------- #


class _Boom(Exception):
    pass


def test_translator_primary_ok(ha_cfg):
    pri = MagicMock()
    pri.translate.return_value = "from-llm"
    sec = MagicMock()
    sec.translate.return_value = "from-google"
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    assert ft.translate("hello") == "from-llm"
    pri.translate.assert_called_once_with("hello")
    sec.translate.assert_not_called()
    assert ft.active_backend == "llm"


def test_translator_falls_back_on_primary_fail(ha_cfg):
    pri = MagicMock()
    pri.translate.side_effect = _Boom("network timeout")
    sec = MagicMock()
    sec.translate.return_value = "from-google"
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    assert ft.translate("hello") == "from-google"
    sec.translate.assert_called_once_with("hello")
    assert ft.active_backend == "google"


def test_translator_empty_short_circuit(ha_cfg):
    pri = MagicMock()
    sec = MagicMock()
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    assert ft.translate("") == ""
    assert ft.translate("   ") == ""
    pri.translate.assert_not_called()


def test_translator_stream_fallback_policy_a(ha_cfg):
    """Primary stream fails → secondary full translate + one on_token(full)."""
    pri = MagicMock()
    pri.translate_stream.side_effect = _Boom("429 rate limit")
    sec = MagicMock()
    sec.translate.return_value = "FULL"
    # no translate_stream on secondary
    del sec.translate_stream

    tokens: list[str] = []
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    out = ft.translate_stream("oi", on_token=tokens.append)
    assert out == "FULL"
    assert tokens == ["FULL"]
    assert ft.active_backend == "google"


def test_translator_both_fail_raises(ha_cfg):
    pri = MagicMock()
    pri.translate.side_effect = _Boom("network error")
    sec = MagicMock()
    sec.translate.side_effect = _Boom("getaddrinfo failed")
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    with pytest.raises(RuntimeError, match="primary and fallback"):
        ft.translate("x")


def test_translator_no_secondary_reraises_primary(ha_cfg):
    pri = MagicMock()
    pri.translate.side_effect = _Boom("network timeout")
    ft = FailoverTranslator(pri, None, ha_cfg, log=lambda *_: None)
    with pytest.raises(_Boom, match="timeout"):
        ft.translate("x")


def test_translator_circuit_skips_primary_after_failures(ha_cfg):
    ha_cfg.CIRCUIT_FAIL_THRESHOLD = 1
    pri = MagicMock()
    pri.translate.side_effect = _Boom("429 rate limit")
    sec = MagicMock()
    sec.translate.return_value = "g"

    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    assert ft.translate("a") == "g"
    assert pri.translate.call_count == 1
    # Circuit open → secondary only
    assert ft.translate("b") == "g"
    assert pri.translate.call_count == 1
    assert sec.translate.call_count == 2


def test_translator_set_language_pair_propagates(ha_cfg):
    pri = MagicMock()
    sec = MagicMock()
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    ft.set_language_pair("pt", "en")
    pri.set_language_pair.assert_called_once_with("pt", "en")
    sec.set_language_pair.assert_called_once_with("pt", "en")


def test_translator_explain_synonyms_delegates(ha_cfg):
    pri = MagicMock()
    pri.explain_synonyms.return_value = "syns"
    sec = MagicMock()
    ft = FailoverTranslator(pri, sec, ha_cfg, log=lambda *_: None)
    assert ft.explain_synonyms("fast") == "syns"
    pri.explain_synonyms.assert_called_once_with("fast")


# --------------------------------------------------------------------------- #
# FailoverTranscriber
# --------------------------------------------------------------------------- #


def test_stt_primary_ok(ha_cfg):
    pri = MagicMock()
    pri.transcribe.return_value = "heard"
    factory = MagicMock(return_value=MagicMock())
    fs = FailoverTranscriber(pri, factory, ha_cfg, log=lambda *_: None)
    assert fs.transcribe(object()) == "heard"
    factory.assert_not_called()
    assert fs.active_backend == "groq"


def test_stt_falls_back_to_local(ha_cfg):
    pri = MagicMock()
    pri.transcribe.side_effect = _Boom("429 rate limit")
    local = MagicMock()
    local.transcribe.return_value = "local-heard"
    fs = FailoverTranscriber(pri, lambda: local, ha_cfg, log=lambda *_: None)
    assert fs.transcribe(object()) == "local-heard"
    assert fs.active_backend == "local"


def test_stt_permanent_skips_retry_goes_secondary(ha_cfg):
    ha_cfg.FAILOVER_MAX_RETRIES = 2
    pri = MagicMock()
    pri.transcribe.side_effect = _Boom("rejected the API key (401)")
    local = MagicMock()
    local.transcribe.return_value = "ok"
    fs = FailoverTranscriber(pri, lambda: local, ha_cfg, log=lambda *_: None)
    assert fs.transcribe(object()) == "ok"
    # Permanent → single primary attempt (no retry storm)
    assert pri.transcribe.call_count == 1


def test_stt_both_fail_raises(ha_cfg):
    pri = MagicMock()
    pri.transcribe.side_effect = _Boom("network error")
    local = MagicMock()
    local.transcribe.side_effect = _Boom("whisper boom")
    fs = FailoverTranscriber(pri, lambda: local, ha_cfg, log=lambda *_: None)
    with pytest.raises(RuntimeError, match="primary and fallback"):
        fs.transcribe(object())


def test_stt_no_fallback_reraises(ha_cfg):
    pri = MagicMock()
    pri.transcribe.side_effect = _Boom("network timeout")
    fs = FailoverTranscriber(pri, None, ha_cfg, log=lambda *_: None)
    with pytest.raises(_Boom, match="timeout"):
        fs.transcribe(object())


def test_stt_language_setter_propagates(ha_cfg):
    pri = MagicMock()
    pri.language = "en"
    local = MagicMock()
    local.language = "en"
    fs = FailoverTranscriber(pri, lambda: local, ha_cfg, log=lambda *_: None)
    # Force secondary load
    pri.transcribe.side_effect = _Boom("429")
    local.transcribe.return_value = "x"
    fs.transcribe(object())
    fs.language = "pt"
    assert pri.language == "pt"
    assert local.language == "pt"


def test_stt_circuit_skips_primary(ha_cfg):
    ha_cfg.CIRCUIT_FAIL_THRESHOLD = 1
    pri = MagicMock()
    pri.transcribe.side_effect = _Boom("timeout")
    local = MagicMock()
    local.transcribe.return_value = "L"
    fs = FailoverTranscriber(pri, lambda: local, ha_cfg, log=lambda *_: None)
    assert fs.transcribe(object()) == "L"
    assert pri.transcribe.call_count == 1
    assert fs.transcribe(object()) == "L"
    assert pri.transcribe.call_count == 1  # open circuit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_translator_uses_llm_helpers(ha_cfg):
    from livelingo.llm import LLMTranslator

    plain = MagicMock()  # not LLM
    assert translator_uses_llm(plain) is False
    assert translator_uses_llm(None) is False

    with pytest.MonkeyPatch.context() as mp:
        # Avoid real Session setup side effects if any — LLMTranslator needs Session
        from unittest.mock import patch

        with patch("livelingo.llm.requests.Session"):
            llm = LLMTranslator(ha_cfg)
            assert translator_uses_llm(llm) is True
            wrap = FailoverTranslator(llm, MagicMock(), ha_cfg, log=lambda *_: None)
            assert translator_uses_llm(wrap) is True
            wrap_only_google = FailoverTranslator(
                None, MagicMock(), ha_cfg, log=lambda *_: None
            )
            assert translator_uses_llm(wrap_only_google) is False


def test_transcriber_uses_groq_helpers(ha_cfg):
    from unittest.mock import patch

    from livelingo.groq_transcribe import GroqTranscriber

    with patch.object(GroqTranscriber, "__init__", lambda self, *a, **k: None):
        g = GroqTranscriber.__new__(GroqTranscriber)
        assert transcriber_uses_groq(g) is True
        wrap = FailoverTranscriber(g, None, ha_cfg, log=lambda *_: None)
        assert transcriber_uses_groq(wrap) is True
        local_only = FailoverTranscriber(None, None, ha_cfg, log=lambda *_: None)
        assert transcriber_uses_groq(local_only) is False
        assert transcriber_uses_groq(None) is False
