"""#cmd key character resolution (ABNT2 / WT: slash vs question mark)."""

from __future__ import annotations

from types import SimpleNamespace

from livelingo.tui_app import LiveLingoApp


def _fake_key(*, character=None, name="", key=""):
    return SimpleNamespace(character=character, name=name, key=key)


resolve = LiveLingoApp._resolve_key_character


def test_shift_slash_is_question_mark():
    assert resolve(_fake_key(name="shift+slash")) == "?"
    assert resolve(_fake_key(key="shift+solidus")) == "?"


def test_plain_slash_is_slash():
    assert resolve(_fake_key(name="slash")) == "/"
    assert resolve(_fake_key(character="/")) == "/"


def test_explicit_question_mark():
    assert resolve(_fake_key(character="?")) == "?"
    assert resolve(_fake_key(name="question_mark")) == "?"


def test_character_wins_over_name():
    assert resolve(_fake_key(character="?", name="shift+slash")) == "?"
    assert resolve(_fake_key(character="/", name="slash")) == "/"
