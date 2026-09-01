"""Interview Coach: question detector, JSON parse, UI emit."""

from __future__ import annotations

from types import SimpleNamespace

from livelingo.interview_coach import (
    SYSTEM_PROMPT,
    InterviewCoach,
    _build_user_prompt,
    is_interview_question,
)
from livelingo.interview_llm import parse_coach_response
from livelingo import ui


def test_system_prompt_requires_spoken_examples():
    """Spoken EN + pt-BR must instruct 1–2 simple lived-experience examples."""
    low = SYSTEM_PROMPT.lower()
    assert "example" in low
    assert "spoken_pt" in low
    assert "lived experience" in low or "past project" in low
    assert "1–2" in SYSTEM_PROMPT or "1-2" in SYSTEM_PROMPT


def test_user_prompt_reminds_examples_for_topic():
    prompt = _build_user_prompt(
        "How would you design a rate limiter?",
        question_pt="Como você projetaria um rate limiter?",
    )
    low = prompt.lower()
    assert "example" in low
    assert "spoken" in low
    assert "rate limiter" in low


def test_user_prompt_includes_coach_md_context():
    prompt = _build_user_prompt(
        "How do you map DTOs?",
        context="Java developer with Angular and Spring Boot.",
        context_path="COACH.md",
        profile="8y backend",
    )
    assert "COACH.md" in prompt
    assert "Spring Boot" in prompt
    assert "INTERVIEW_CANDIDATE_PROFILE" in prompt or "8y backend" in prompt
    assert "Job / interview context" in prompt


def test_load_coach_context_prefers_coach_md(tmp_path, monkeypatch):
    from livelingo.interview_coach import load_coach_context, resolve_coach_context_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENT.md").write_text("# agent\nagent only", encoding="utf-8")
    (tmp_path / "COACH.md").write_text(
        "# coach\nJava Angular Spring Boot interview", encoding="utf-8"
    )
    path = resolve_coach_context_path(None, cwd=str(tmp_path))
    assert path is not None and path.name == "COACH.md"
    text, p = load_coach_context(None, cwd=str(tmp_path))
    assert p is not None and p.name == "COACH.md"
    assert "Spring Boot" in text


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


def test_is_interview_question_preamble_without_question_mark():
    """LC often drops '?' and prefixes 'Specifically,' before 'what…'."""
    q = (
        "Specifically, what tool or strategy do you use so that your Typescript "
        "definitions stay In Sync with the back end and convert keys from snake "
        "case to camel case without manual edits"
    )
    assert is_interview_question(q, min_chars=40)
    assert is_interview_question(
        "Question five When an unexpected crash happens inside an async task, "
        "how do you handle it cleanly so Phoenix returns a proper error?",
        min_chars=40,
    )


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
      "architect": ["Added circuit breaker at edge"],
      "tradeoffs": "Caching cuts latency but risks stale reads under write-heavy load.",
      "spoken_pt": "Eu lidertei a interrupção do checkout.",
      "software_engineer_pt": ["Identifiquei queries N+1"],
      "architect_pt": ["Adicionei circuit breaker na borda"],
      "tradeoffs_pt": "Cache reduz latência, mas arrisca leitura velha sob muita escrita."
    }
    """
    out = parse_coach_response(raw)
    assert "checkout" in out["spoken"]
    assert out["software_engineer"][0].startswith("Root-caused")
    assert "circuit" in out["architect"][0].lower()
    assert "Caching" in out["tradeoffs"]
    assert "stale" in out["tradeoffs"].lower()
    assert "checkout" in out["spoken_pt"].lower() or "interrup" in out["spoken_pt"].lower()
    assert out["software_engineer_pt"]
    assert "Cache" in out["tradeoffs_pt"] or "latência" in out["tradeoffs_pt"]


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
            tradeoffs="Replicas help reads but add replication lag on writes.",
            spoken_pt="Particionamos por tenant e adicionamos réplicas de leitura.",
            software_engineer_pt=["Usei EXPLAIN nos caminhos quentes"],
            architect_pt=["Introduzi CQRS para relatórios"],
            tradeoffs_pt="Réplicas ajudam leitura, mas geram lag na escrita.",
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
    assert "Trade-offs" in body or "replication lag" in body
    assert "pt-BR" in body or "Particionamos" in body
    # Two blank separators between EN and PT (raw "")
    raws = [i for i, (k, t) in enumerate([(k, t) for k, t, _ in captured]) if k == "raw"]
    assert len([1 for k, t, _ in captured if k == "raw" and t == ""]) >= 2


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


def test_ask_simulate_lc_schedules_and_emits_lc(monkeypatch):
    """airespond path: fake LC block + force coach job."""
    cfg = SimpleNamespace(
        INTERVIEW_COACH_ENABLED=False,
        INTERVIEW_QUESTION_MODE="auto",
        INTERVIEW_MIN_CHARS=40,
        INTERVIEW_COACH_PROVIDER="grok",
        INTERVIEW_COACH_MODEL="grok-3-mini",
        INTERVIEW_COACH_TIMEOUT_S=5,
        XAI_API_KEY="xai-test",
        GROK_API_KEY="",
        INTERVIEW_CANDIDATE_PROFILE="",
    )

    class _FakeLLM:
        provider_name = "grok"
        api_key = "xai-test"
        model = "grok-3-mini"

        def complete(self, system, user):
            assert "SAGA" in user or "microsserv" in user.lower() or "simulado" in user.lower()
            return (
                '{"spoken":"I would use choreography for SAGA with outbox.",'
                '"software_engineer":["Idempotent consumers"],'
                '"architect":["Prefer choreography over orchestration for loose coupling"],'
                '"tradeoffs":"Choreography scales ownership but debugging spans many services; '
                'orchestration is clearer but becomes a bottleneck.",'
                '"spoken_pt":"Eu usaria coreografia de SAGA com outbox.",'
                '"software_engineer_pt":["Consumidores idempotentes"],'
                '"architect_pt":["Prefiro coreografia à orquestração para acoplamento frouxo"],'
                '"tradeoffs_pt":"Coreografia escala ownership, mas a depuração atravessa vários serviços."}'
            )

    captured: list[tuple[str, str, str]] = []

    def sink(kind, text, panel="main"):
        captured.append((kind, text, panel))

    coach = InterviewCoach(cfg, llm=_FakeLLM())
    prev = ui.get_log_sink()
    try:
        ui.set_log_sink(sink)
        ok, msg = coach.ask(
            "Me fale sobre microsserviços no padrão de integração SAGA",
            simulate_lc=True,
        )
        # Wait briefly for background thread
        import time

        for _ in range(50):
            if coach.last_result() is not None:
                break
            time.sleep(0.02)
    finally:
        ui.set_log_sink(prev)

    assert ok is True
    assert "agendado" in msg.lower() or "Coach" in msg
    # Simulated LC pair went to lc panel
    assert any(p == "lc" for _, _, p in captured), captured
    last = coach.last_result()
    assert last is not None
    assert last.error == ""
    assert "SAGA" in last.spoken or "choreography" in last.spoken.lower()
    assert last.software_engineer
    assert last.architect
    assert "Choreography" in last.tradeoffs or "bottleneck" in last.tradeoffs.lower()
    assert "SAGA" in last.spoken_pt or "coreografia" in last.spoken_pt.lower()
    assert last.software_engineer_pt
    assert last.tradeoffs_pt
