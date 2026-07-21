"""
import_phrase_csv.py
====================
Import phrase pairs from a CSV export into the global translation_pairs cache.

Expected columns (Google / Live Caption style exports):
  Timestamp, SourceText, TranslatedText, TargetLanguage, ApiUsed

Deduplication: UNIQUE(source_lang, target_lang, source_norm) via
db.upsert_translation_pair + normalize_phrase (same key as runtime HIT).

CLI:
  python -m livelingo.import_phrase_csv exported1.csv --dry-run
  python -m livelingo.import_phrase_csv exported1.csv
  python -m livelingo.import_phrase_csv exported1.csv --also-reverse
  python -m livelingo.import_phrase_csv exported1.csv --source-lang en
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any

from . import db
from .phrase_cache import normalize_phrase


def lang_code(value: str) -> str:
    """pt-BR → pt, en-US → en, zh-CN → zh."""
    s = (value or "").strip().lower()
    if not s:
        return ""
    if "-" in s:
        s = s.split("-", 1)[0]
    if "_" in s:
        s = s.split("_", 1)[0]
    if s in ("cn", "cmn", "zh-cn", "zh-tw"):
        return "zh"
    if s in ("jp",):
        return "ja"
    return s


def _row_get(row: dict, *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n])
        # case-insensitive / BOM
        for k, v in row.items():
            if k and k.lstrip("\ufeff").lower() == n.lower():
                return str(v) if v is not None else ""
    return ""


def import_phrase_csv(
    path: str,
    *,
    default_source_lang: str = "en",
    also_reverse: bool = False,
    dry_run: bool = False,
    phrase_cache=None,
) -> dict[str, Any]:
    """
    Import CSV into translation_pairs (and optional in-memory PhraseCache).

    Returns stats:
      read, inserted, updated, unchanged, skipped_empty, skipped_identical,
      reverse_inserted, reverse_updated, reverse_unchanged, errors, backup_path
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV not found: {path}")

    stats: dict[str, Any] = {
        "read": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_empty": 0,
        "skipped_identical": 0,
        "reverse_inserted": 0,
        "reverse_updated": 0,
        "reverse_unchanged": 0,
        "errors": 0,
        "backup_path": None,
        "path": path,
        "dry_run": dry_run,
        "also_reverse": also_reverse,
    }

    db.init_db()

    # Pre-import backup (unless dry-run)
    if not dry_run:
        try:
            from .phrase_cache import PhraseCache, get_phrase_cache

            cache = phrase_cache or get_phrase_cache()
            stamp_name = None
            # Prefer named pre_import backup
            import time as _time

            os.makedirs(
                os.path.join(".cache", "phrase_cache_backups"), exist_ok=True
            )
            stamp = _time.strftime("%Y%m%d_%H%M%S")
            pre_path = os.path.join(
                ".cache", "phrase_cache_backups", f"pre_import_{stamp}.json"
            )
            stats["backup_path"] = cache.backup(pre_path)
        except Exception:
            stats["backup_path"] = None

    default_src = lang_code(default_source_lang) or "en"

    # Preload existing pairs into a dict for O(1) dedupe (avoids 4k SQLite hits on WSL)
    existing_map: dict[tuple[str, str, str], str] = {}
    try:
        for p in db.list_translation_pairs(limit=100000):
            k = (
                (p.get("source_lang") or "").lower().strip(),
                (p.get("target_lang") or "").lower().strip(),
                (p.get("source_norm") or "").strip(),
            )
            if k[0] and k[1] and k[2]:
                existing_map[k] = (p.get("target_text") or "").strip()
    except Exception:
        existing_map = {}

    # Track seen norms within this CSV pass to avoid re-processing dups in file
    seen_fwd: set[tuple[str, str, str]] = set()
    seen_rev: set[tuple[str, str, str]] = set()

    def _apply_one(
        src_lang: str,
        tgt_lang: str,
        source_text: str,
        target_text: str,
        *,
        reverse: bool = False,
    ) -> None:
        nonlocal stats
        src_lang = lang_code(src_lang)
        tgt_lang = lang_code(tgt_lang)
        source_text = (source_text or "").strip()
        target_text = (target_text or "").strip()
        if not src_lang or not tgt_lang or not source_text or not target_text:
            stats["skipped_empty"] += 1
            return
        norm = normalize_phrase(source_text)
        if not norm:
            stats["skipped_empty"] += 1
            return
        if normalize_phrase(source_text) == normalize_phrase(target_text):
            stats["skipped_identical"] += 1
            return

        key = (src_lang, tgt_lang, norm)
        bucket_seen = seen_rev if reverse else seen_fwd
        if key in bucket_seen:
            stats["unchanged" if not reverse else "reverse_unchanged"] += 1
            return
        bucket_seen.add(key)

        old_tgt = existing_map.get(key)
        if old_tgt is not None:
            if old_tgt == target_text:
                stats["unchanged" if not reverse else "reverse_unchanged"] += 1
                if not dry_run and phrase_cache is not None:
                    try:
                        with phrase_cache._lock:
                            phrase_cache._mem_put(key, target_text)
                    except Exception:
                        pass
                return
            stats["updated" if not reverse else "reverse_updated"] += 1
        else:
            stats["inserted" if not reverse else "reverse_inserted"] += 1

        if dry_run:
            # Pretend store so later CSV dups / reverse see this key
            existing_map[key] = target_text
            return

        try:
            db.upsert_translation_pair(
                src_lang,
                tgt_lang,
                norm,
                source_text,
                target_text,
                bump_hit=False,
            )
            existing_map[key] = target_text
        except Exception:
            stats["errors"] += 1
            if reverse:
                if old_tgt is not None:
                    stats["reverse_updated"] = max(0, stats["reverse_updated"] - 1)
                else:
                    stats["reverse_inserted"] = max(0, stats["reverse_inserted"] - 1)
            else:
                if old_tgt is not None:
                    stats["updated"] = max(0, stats["updated"] - 1)
                else:
                    stats["inserted"] = max(0, stats["inserted"] - 1)
            return

        if phrase_cache is not None:
            try:
                with phrase_cache._lock:
                    phrase_cache._mem_put(key, target_text)
            except Exception:
                pass

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV sem cabeçalho")
        for row in reader:
            stats["read"] += 1
            source_text = _row_get(row, "SourceText", "source", "source_text")
            target_text = _row_get(
                row, "TranslatedText", "target", "translated", "translated_text"
            )
            target_lang_raw = _row_get(
                row, "TargetLanguage", "target_lang", "target_language", "lang"
            )
            tgt = lang_code(target_lang_raw)
            # Default source: en when target is not en; if target is en keep default_src
            src = default_src
            if not tgt:
                stats["skipped_empty"] += 1
                continue

            _apply_one(src, tgt, source_text, target_text, reverse=False)

            if also_reverse:
                # Reverse: TranslatedText is source, SourceText is target
                _apply_one(tgt, src, target_text, source_text, reverse=True)

    return stats


def format_import_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"CSV: {stats.get('path')}",
        f"dry_run={stats.get('dry_run')} · also_reverse={stats.get('also_reverse')}",
        f"linhas lidas: {stats.get('read', 0)}",
        f"inseridos: {stats.get('inserted', 0)} · "
        f"atualizados: {stats.get('updated', 0)} · "
        f"inalterados: {stats.get('unchanged', 0)}",
        f"skip vazio: {stats.get('skipped_empty', 0)} · "
        f"skip idêntico src≈tgt: {stats.get('skipped_identical', 0)} · "
        f"erros: {stats.get('errors', 0)}",
    ]
    if stats.get("also_reverse"):
        lines.append(
            f"reverso: +{stats.get('reverse_inserted', 0)} ins · "
            f"{stats.get('reverse_updated', 0)} upd · "
            f"{stats.get('reverse_unchanged', 0)} same"
        )
    if stats.get("backup_path"):
        lines.append(f"backup pré-import: {stats['backup_path']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Import phrase pairs from CSV into LiveLingo translation_pairs cache"
    )
    p.add_argument("csv_path", help="Path to exported CSV (SourceText, TranslatedText, …)")
    p.add_argument(
        "--source-lang",
        default="en",
        help="Source language code when CSV has no SourceLanguage (default: en)",
    )
    p.add_argument(
        "--also-reverse",
        action="store_true",
        help="Also store reverse pairs (target→source) for the other direction",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report stats without writing to the database",
    )
    args = p.parse_args(argv)

    try:
        stats = import_phrase_csv(
            args.csv_path,
            default_source_lang=args.source_lang,
            also_reverse=args.also_reverse,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"[x] Import failed: {exc}", file=sys.stderr)
        return 1
    print(format_import_stats(stats))
    if args.dry_run:
        print("(dry-run — nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
