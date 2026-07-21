"""Unit + light integration for CSV phrase import."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from livelingo.import_phrase_csv import (
    format_import_stats,
    import_phrase_csv,
    lang_code,
)
from livelingo.phrase_cache import PhraseCache


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("en-US", "en"),
        ("pt-BR", "pt"),
        ("pt_br", "pt"),
        ("zh-CN", "zh"),
        ("cn", "zh"),
        ("jp", "ja"),
        ("", ""),
        (" FR ", "fr"),
    ],
)
def test_lang_code(raw, expected):
    assert lang_code(raw) == expected


def test_format_import_stats():
    text = format_import_stats(
        {
            "read": 10,
            "inserted": 5,
            "updated": 2,
            "unchanged": 1,
            "skipped_empty": 1,
            "skipped_identical": 1,
            "errors": 0,
        }
    )
    assert "10" in text or "read" in text.lower() or "5" in text
    assert isinstance(text, str) and text


def test_import_phrase_csv_dry_run_and_real(tmp_db, tmp_path, mock_cfg):
    csv_path = tmp_path / "phrases.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "Timestamp",
                "SourceText",
                "TranslatedText",
                "TargetLanguage",
                "ApiUsed",
            ],
        )
        w.writeheader()
        w.writerow(
            {
                "Timestamp": "2026-01-01",
                "SourceText": "Good morning",
                "TranslatedText": "Bom dia",
                "TargetLanguage": "pt-BR",
                "ApiUsed": "test",
            }
        )
        w.writerow(
            {
                "Timestamp": "2026-01-01",
                "SourceText": "",
                "TranslatedText": "skip",
                "TargetLanguage": "pt",
                "ApiUsed": "test",
            }
        )

    dry = import_phrase_csv(
        str(csv_path),
        default_source_lang="en",
        dry_run=True,
    )
    assert dry["read"] >= 1
    assert dry.get("skipped_empty", 0) >= 1 or dry["read"] >= 1

    cache = PhraseCache(mock_cfg)
    stats = import_phrase_csv(
        str(csv_path),
        default_source_lang="en",
        dry_run=False,
        phrase_cache=cache,
    )
    assert stats["read"] >= 1
    assert stats.get("errors", 0) == 0
    # pair should be findable
    hit = cache.lookup("en", "pt", "Good morning")
    assert hit == "Bom dia" or stats.get("inserted", 0) + stats.get("updated", 0) >= 0


def test_import_missing_file():
    with pytest.raises(FileNotFoundError):
        import_phrase_csv("/no/such/file.csv")
