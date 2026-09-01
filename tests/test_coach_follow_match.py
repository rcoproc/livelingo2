"""Unit tests for Spoken EN follow-along matcher (no mic/STT)."""

from __future__ import annotations

from livelingo.coach_follow import (
    advance_cursor,
    normalize_word,
    render_current_word_banner,
    render_spoken_markup,
    tokenize_spoken,
    words_match,
)


def test_tokenize_and_normalize():
    words = tokenize_spoken("When designing APIs, I focus on clarity.")
    assert words[0] == "When"
    assert "APIs," in words or words[2].startswith("APIs")
    assert normalize_word("APIs,") == "apis"
    assert normalize_word("clarity.") == "clarity"


def test_words_match_fuzzy():
    assert words_match("designing", "designing")
    assert words_match("designing", "desining", threshold=0.7)
    assert words_match("clarity", "clar")  # prefix partial
    assert not words_match("apis", "banana")


def test_advance_cursor_basic():
    words = tokenize_spoken("When designing APIs I focus on clarity")
    cur = 0
    cur = advance_cursor(words, cur, "when designing")
    assert cur == 2
    cur = advance_cursor(words, cur, "APIs I focus")
    assert cur >= 5
    cur = advance_cursor(words, cur, "on clarity")
    assert cur == len(words)


def test_advance_never_goes_backwards():
    words = tokenize_spoken("one two three four")
    cur = advance_cursor(words, 0, "one two")
    assert cur == 2
    cur2 = advance_cursor(words, cur, "one")
    assert cur2 == 2


def test_advance_skips_article():
    words = tokenize_spoken("I focus on the clarity of design")
    # speaker skips "the"
    cur = advance_cursor(words, 0, "I focus on clarity")
    assert cur >= 4


def test_render_markup_progress():
    words = ["When", "designing", "APIs"]
    md = render_spoken_markup(words, 1)
    assert "[dim]When[/]" in md
    assert "designing" in md
    assert "APIs" in md
    done = render_spoken_markup(words, 3)
    assert done.count("[dim]") >= 3


def test_build_script_appends_tradeoffs_title():
    from livelingo.coach_follow import TRADEOFFS_TITLE, build_follow_script

    script, sp = build_follow_script("one two", "A vs B")
    assert sp == 2
    assert script[0] == "one" and script[1] == "two"
    assert script[2] == TRADEOFFS_TITLE
    assert "A" in script and "B" in script


def test_render_continues_into_tradeoffs():
    from livelingo.coach_follow import TRADEOFFS_TITLE, build_follow_script

    script, sp = build_follow_script("one two", "Latency versus consistency")
    # Still in Spoken — no Trade-offs block yet
    md0 = render_spoken_markup(script, 0, spoken_len=sp)
    assert "Trade-offs" not in md0
    # On title
    md_t = render_spoken_markup(script, sp, spoken_len=sp)
    assert "Trade-offs" in md_t
    assert "Latency" in md_t
    # Mid tradeoffs
    md_w = render_spoken_markup(script, sp + 2, spoken_len=sp)
    assert "Trade-offs" in md_w
    assert script[sp] == TRADEOFFS_TITLE


def test_engine_reads_through_tradeoffs_title():
    import time

    from livelingo.coach_follow import TRADEOFFS_TITLE, CoachFollowEngine

    eng = CoachFollowEngine(wpm=300)
    eng.set_spoken("one two")
    eng.set_tradeoffs("alpha beta")
    assert TRADEOFFS_TITLE in eng.state.words
    assert eng.state.spoken_len == 2
    ok, _ = eng.start()
    assert ok
    deadline = time.time() + 4.0
    saw_tradeoffs = False
    while time.time() < deadline:
        if eng.state.status == "tradeoffs":
            saw_tradeoffs = True
        if eng.state.status == "done":
            break
        time.sleep(0.05)
    eng.stop()
    assert saw_tradeoffs
    assert eng.state.cursor >= len(eng.state.words) or eng.state.status == "done"


def test_full_markup_keeps_all_spoken_words_stable():
    """Reading must not drop/shift Spoken words — only highlight moves."""
    words = [f"w{i}" for i in range(40)]
    md0 = render_spoken_markup(words, 0, spoken_len=40)
    md20 = render_spoken_markup(words, 20, spoken_len=40)
    for i in range(40):
        assert f"w{i}" in md0
        assert f"w{i}" in md20
    assert "…" not in md20


def test_line_index_for_cursor_advances_down():
    from livelingo.coach_follow import line_index_for_cursor, wrap_words_to_lines

    words = ["hello", "world", "this", "is", "a", "longer", "line", "here"]
    lines = wrap_words_to_lines(words, width=12)
    assert len(lines) >= 2
    y0 = line_index_for_cursor(words, 0, 12)
    y_last = line_index_for_cursor(words, len(words) - 1, 12)
    assert y0 == 0
    assert y_last >= y0


def test_parse_coach_spoken_payload_json():
    # Avoid importing tui_view (needs textual); mirror parser via engine payload shape
    import json

    from livelingo.coach_follow import build_follow_script

    raw = json.dumps(
        {
            "spoken": "One two.",
            "tradeoffs": "A vs B",
            "n": 3,
            "question": "Why caches?",
        }
    )
    data = json.loads(raw)
    script, sp = build_follow_script(data["spoken"], data["tradeoffs"])
    assert sp == 2
    assert "Trade-offs" in script
    assert data["n"] == 3


def test_render_current_word_banner():
    words = ["When", "designing", "APIs"]
    ban = render_current_word_banner(words, 1)
    assert "designing" in ban or "d e s i g n i n g" in ban
    assert "bold cyan" in ban or "cyan" in ban
    done = render_current_word_banner(words, 3)
    assert "done" in done.lower()


def test_wpm_clamp_and_ms_per_word():
    from livelingo.coach_follow import CoachFollowEngine, _clamp_wpm

    assert _clamp_wpm(10) == 40.0
    assert _clamp_wpm(999) == 300.0
    eng = CoachFollowEngine(wpm=120)
    assert abs(eng.ms_per_word() - 500.0) < 0.01


def test_timed_engine_advances_without_stt():
    """Teleprompter advances on the WPM clock — never waits for mic/STT."""
    import time

    from livelingo.coach_follow import CoachFollowEngine, FollowState

    updates: list[FollowState] = []
    eng = CoachFollowEngine(on_update=updates.append, wpm=300)  # fast for test
    eng.set_spoken("one two three four")
    ok, msg = eng.start()
    assert ok
    assert "WPM" in msg
    # Wait enough for ~4 words at 300 WPM (~200ms/word + first hold)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if eng.state.status == "done" or eng.state.cursor >= 4:
            break
        time.sleep(0.05)
    eng.stop()
    assert eng.state.cursor >= 3 or eng.state.status == "done"
    assert any(u.cursor > 0 for u in updates)
