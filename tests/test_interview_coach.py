"""Interview Coach: question detector, JSON parse, UI emit."""

from __future__ import annotations

from types import SimpleNamespace

from livelingo.interview_coach import InterviewCoach, is_interview_question
from livelingo.interview_llm import parse_coach_response
from livelingo import ui


def test_is_interview_question_detects_clear_qs():
    assert is_interview_question(
        "Tell me about a production incident you owned end to end?",
        min_chars=40,
    )
    assert is_interview_question(
        "How would you design a rate limiter for a multi-region API?",
        min_chars=40,
    )
    assert is_interview_question(
        "Could you walk me through your last system design interview answer in detail?",
        min_chars=40,
    )
    assert is_interview_question("What is CAP theorem?", min_chars=20)


def test_is_interview_question_rejects_smalltalk():
    assert not is_interview_question("Thanks", min_chars=40)
    assert not is_interview_question("ok", min_chars=40)
    assert not is_interview_question("Hello everyone", min_chars=40)
    assert not is_interview_question("We deployed Friday.", min_chars=40)


def test_parse_coach_response_json():
    raw = """
    {
      "spoken": "I owned the checkout outage.",
      "software_engineer": ["Root-caused N+1 queries"],
      "architect": ["Added circuit breaker at edge"]
    }
    """
    out = parse_coach_response(raw)
    assert "checkout" in out["spoken"]
    assert out["software_engineer"][0].startswith("Root-caused")
    assert "circuit" in out["architect"][0].lower()


def test_parse_coach_response_fenced_and_fallback():
    raw = """```json
    {"spoken": "Hello world answer.", "software_engineer": ["a"], "architect": ["b"]}
    ```"""
    out = parse_coach_response(raw)
    assert out["spoken"].startswith("Hello")
    out2 = parse_coach_response("Just a plain spoken paragraph without JSON.")
    assert "plain spoken" in out2["spoken"]
    assert out2["software_engineer"] == []


def test_coach_block_emits_to_coach_panel():
    captured: list[tuple[str, str, str]] = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        ui.coach_block(
            3,
            "Tell me about scaling Postgres?",
            "We partitioned by tenant and added read replicas.",
            ["Used EXPLAIN to find hot paths"],
            ["Introduced CQRS for reporting"],
            provider="grok",
        )
    finally:
        ui.set_log_sink(prev)

    assert captured
    assert all(p == "coach" for _, _, p in captured), captured
    body = "\n".join(t for _, t, _ in captured)
    assert "Tell me about scaling" in body
    assert "Spoken" in body or "partitioned" in body
    assert "SE" in body or "EXPLAIN" in body
    assert "Arch" in body or "CQRS" in body


def test_maybe_handle_skips_when_disabled():
    cfg = SimpleNamespace(
        INTERVIEW_COACH_ENABLED=False,
        INTERVIEW_QUESTION_MODE="auto",
        INTERVIEW_MIN_CHARS=40,
        INTERVIEW_COACH_PROVIDER="grok",
        INTERVIEW_COACH_MODEL="grok-3-mini",
        INTERVIEW_COACH_TIMEOUT_S=5,
        XAI_API_KEY="",
        GROK_API_KEY="",
        INTERVIEW_CANDIDATE_PROFILE="",
    )
    coach = InterviewCoach(cfg, llm=None)
    assert (
        coach.maybe_handle(
            1,
            "Tell me about a hard production incident you led last year?",
            "Conte sobre um incidente…",
        )
        is False
    )


def test_normalize_panel_coach():
    assert ui._normalize_panel("coach") == "coach"
    assert ui._normalize_panel("entrevista") == "coach"
