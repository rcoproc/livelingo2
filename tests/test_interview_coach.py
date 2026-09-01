"""Interview Coach: question detector, JSON parse, UI emit."""

from __future__ import annotations

from types import SimpleNamespace

from unittest.mock import MagicMock, patch

from livelingo.interview_coach import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_EN,
    SYSTEM_PROMPT_PT,
    InterviewCoach,
    _build_pt_user_prompt,
    _build_user_prompt,
    is_interview_question,
)
from livelingo.interview_llm import parse_coach_pt_response, parse_coach_response
from livelingo import ui


def test_system_prompt_en_only_requires_spoken_examples():
    """EN-first prompt: examples in Spoken, no *_pt fields (pt is async)."""
    low = SYSTEM_PROMPT_EN.lower()
    assert "example" in low
    assert "spoken_pt" not in SYSTEM_PROMPT_EN
    assert "lived experience" in low or "past project" in low
    assert "1–2" in SYSTEM_PROMPT_EN or "1-2" in SYSTEM_PROMPT_EN
    # Alias still points at EN-only prompt
    assert SYSTEM_PROMPT is SYSTEM_PROMPT_EN
    assert "spoken_pt" in SYSTEM_PROMPT_PT


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


def test_parse_coach_pt_response():
    raw = """
    {
      "spoken_pt": "Eu lidertei a interrupção do checkout.",
      "software_engineer_pt": ["Identifiquei queries N+1"],
      "architect_pt": ["Adicionei circuit breaker na borda"],
      "tradeoffs_pt": "Cache reduz latência."
    }
    """
    out = parse_coach_pt_response(raw)
    assert "checkout" in out["spoken_pt"]
    assert out["software_engineer_pt"][0].startswith("Identifiquei")
    assert "Cache" in out["tradeoffs_pt"]


def test_build_pt_user_prompt_embeds_en_json():
    prompt = _build_pt_user_prompt(
        {
            "spoken": "I use Redis.",
            "software_engineer": ["TTL on keys"],
            "architect": ["Cache aside"],
            "tradeoffs": "Stale vs fast",
        }
    )
    assert "Redis" in prompt
    assert "TTL" in prompt
    assert "Translate" in prompt or "translate" in prompt.lower()


def test_run_job_emits_en_before_pt_async():
    """EN coach_block runs after 1st complete; PT thread uses 2nd complete."""
    calls: list[str] = []

    class FakeLLM:
        provider_name = "fake"

        def complete(self, system, user):
            if "spoken_pt" in (system or "") and "English only" not in (system or ""):
                calls.append("pt")
                return (
                    '{"spoken_pt":"Oi","software_engineer_pt":["a"],'
                    '"architect_pt":["b"],"tradeoffs_pt":"c"}'
                )
            # EN system prompt mentions English only / no *_pt
            calls.append("en")
            return (
                '{"spoken":"Hello there friend today","software_engineer":["se1"],'
                '"architect":["ar1"],"tradeoffs":"trade1"}'
            )

    cfg = SimpleNamespace(
        INTERVIEW_COACH_ENABLED=True,
        INTERVIEW_COACH_PROVIDER="grok",
        INTERVIEW_CANDIDATE_PROFILE="",
        INTERVIEW_COACH_CONTEXT_FILE="",
        INTERVIEW_COACH_PT_ASYNC=True,
        INTERVIEW_MIN_CHARS=20,
        INTERVIEW_STABLE_S=0.1,
        INTERVIEW_COOLDOWN_S=0,
        INTERVIEW_QUESTION_MODE="always",
    )
    coach = InterviewCoach(cfg, session_id=None)
    coach._llm = FakeLLM()
    coach._context_text = ""
    coach._context_path = None

    order: list[str] = []

    def fake_block(*_a, **kwargs):
        order.append("en_ui")
        assert kwargs.get("pt_pending") is True

    def fake_pt(*_a, **_k):
        order.append("pt_ui")

    with (
        patch.object(ui, "coach_block", side_effect=fake_block),
        patch.object(ui, "coach_pt_section", side_effect=fake_pt),
        patch.object(ui, "dim"),
        patch.object(ui, "info"),
        patch.object(ui, "warn"),
        patch.object(ui, "error"),
    ):
        coach._busy = True
        coach._gen = 1
        coach._run_job(1, 7, "How do you design APIs for rate limiting at scale?", "")
        # Wait briefly for background PT thread
        import time

        deadline = time.time() + 2.0
        while time.time() < deadline and "pt_ui" not in order:
            time.sleep(0.05)

    assert order[0] == "en_ui"
    assert "pt_ui" in order
    assert calls[0] == "en"
    assert "pt" in calls
    assert coach._last is not None
    assert "Hello" in (coach._last.spoken or "")
    assert "Oi" in (coach._last.spoken_pt or "")


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


def test_persist_result_writes_sqlite(tmp_db):
    from livelingo import db
    from livelingo.interview_coach import CoachResult

    db.create_session("persist-coach", "T")
    cfg = SimpleNamespace(INTERVIEW_COACH_ENABLED=True)
    coach = InterviewCoach(cfg, llm=None, session_id="persist-coach")
    result = CoachResult(
        n=7,
        question="How do you test distributed systems?",
        spoken="I start with contract tests.",
        software_engineer=["pytest + mocks"],
        architect=["chaos in staging"],
        tradeoffs="Mocks are fast but miss integration bugs.",
        spoken_pt="",
        provider="grok",
        error="",
    )
    coach._persist_result(result)
    rows = db.load_session_coach_results("persist-coach")
    assert len(rows) == 1
    assert rows[0]["coach_num"] == 7
    assert "contract" in rows[0]["spoken_en"]
    assert (rows[0]["spoken_pt"] or "") == ""

    # Async pt-BR fills the same row
    coach._persist_result_pt(
        7,
        spoken_pt="Começo com testes de contrato.",
        software_engineer_pt=["pytest + mocks"],
        architect_pt=["chaos em staging"],
        tradeoffs_pt="Mocks são rápidos, mas perdem bugs de integração.",
    )
    rows2 = db.load_session_coach_results("persist-coach")
    assert len(rows2) == 1
    assert rows2[0]["id"] == rows[0]["id"]
    assert "contrato" in rows2[0]["spoken_pt"].lower()

    # Errors must not be persisted
    bad = CoachResult(n=8, question="Q", spoken="", error="timeout")
    coach._persist_result(bad)
    assert len(db.load_session_coach_results("persist-coach")) == 1


def test_normalize_panel_coach():
    assert ui._normalize_panel("coach") == "coach"
    assert ui._normalize_panel("entrevista") == "coach"


def test_coach_split_paragraphs_sentences():
    parts = ui._coach_split_paragraphs(
        "First sentence here. Second sentence here! Third one?"
    )
    assert len(parts) == 3
    assert parts[0].startswith("First")
    assert parts[1].startswith("Second")
    assert parts[2].startswith("Third")
    # Explicit blank lines win
    parts2 = ui._coach_split_paragraphs("Para A.\n\nPara B.")
    assert parts2 == ["Para A.", "Para B."]


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
