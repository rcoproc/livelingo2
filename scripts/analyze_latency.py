#!/usr/bin/env python3
"""
Analyze LiveLingo chunk latency from SQLite (timing_json).

Usage (from project root):

  python3 scripts/analyze_latency.py
  python3 scripts/analyze_latency.py --db livelingo.db
  python3 scripts/analyze_latency.py --session 20260901_155837_teste-coach-leitura
  python3 scripts/analyze_latency.py --since 2026-09-01

Reports p50/p90/p95 for STT / translate / TTS, split by:
  - translate_cache hit vs miss
  - translate_engine (llm | google | cache | …) when present
  - tts_engine (piper | edge | hybrid | …) when present
  - tts_first (time to first audio chunk) when present
  - source (voice vs livecaptions)

Note: older rows may lack ``translate_engine`` / ``tts_engine``. Run short A/B
sessions after upgrading so new chunks are labeled.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] * (c - k) + xs[c] * (k - f)


def _summary(ms: list[float], label: str) -> None:
    if not ms:
        print(f"  {label}: n=0")
        return
    print(
        f"  {label}: n={len(ms)}  "
        f"p50={_pct(ms, 50):.0f}ms  "
        f"p90={_pct(ms, 90):.0f}ms  "
        f"p95={_pct(ms, 95):.0f}ms  "
        f"mean={statistics.mean(ms):.0f}ms  "
        f"max={max(ms):.0f}ms"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        default="livelingo.db",
        help="Path to SQLite DB (default: ./livelingo.db)",
    )
    ap.add_argument("--session", default="", help="Filter by session_id (exact or prefix)")
    ap.add_argument(
        "--since",
        default="",
        help="Only sessions with created_at >= YYYY-MM-DD (optional)",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"DB not found: {db_path.resolve()}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    sql = """
    SELECT c.session_id, c.chunk_num, c.timing_json, s.created_at, s.title
    FROM chunks c
    LEFT JOIN sessions s ON s.id = c.session_id
    WHERE c.timing_json IS NOT NULL AND length(c.timing_json) > 2
    """
    params: list = []
    if args.session:
        sql += " AND c.session_id LIKE ?"
        params.append(args.session + "%")
    if args.since:
        sql += " AND (s.created_at IS NULL OR s.created_at >= ?)"
        params.append(args.since)

    cur.execute(sql, params)
    rows = cur.fetchall()

    by_stage: dict[str, list[float]] = defaultdict(list)
    by_translate: dict[str, list[float]] = defaultdict(list)
    by_tts: dict[str, list[float]] = defaultdict(list)
    by_tts_first: dict[str, list[float]] = defaultdict(list)
    by_tta: dict[str, list[float]] = defaultdict(list)
    labeled = 0
    unlabeled = 0
    tts_labeled = 0
    tts_unlabeled = 0

    for _sid, _n, tj, _created, _title in rows:
        try:
            d = json.loads(tj)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue

        src = str(d.get("source") or d.get("origin") or "voice").lower()
        bucket = "lc" if src == "livecaptions" else "voice"
        cache = bool(d.get("translate_cache"))
        engine = str(d.get("translate_engine") or "").strip().lower() or None
        tts_eng = str(d.get("tts_engine") or "").strip().lower() or None

        for key in ("stt", "translate", "tts", "total", "time_to_audio"):
            if key not in d or d[key] is None:
                continue
            try:
                ms = float(d[key]) * 1000.0
            except (TypeError, ValueError):
                continue
            if ms < 0:
                continue
            by_stage[key].append(ms)
            by_stage[f"{bucket}|{key}"].append(ms)

        if "translate" in d and d["translate"] is not None:
            try:
                t_ms = float(d["translate"]) * 1000.0
            except (TypeError, ValueError):
                continue
            by_translate["all"].append(t_ms)
            by_translate[f"{bucket}|{'cache' if cache else 'miss'}"].append(t_ms)
            if engine:
                labeled += 1
                by_translate[f"engine={engine}"].append(t_ms)
                by_translate[f"{bucket}|engine={engine}"].append(t_ms)
                if cache or engine == "cache":
                    by_translate["engine=cache"].append(t_ms)
            else:
                unlabeled += 1
                by_translate["engine=(unlabeled-legacy)"].append(t_ms)

        if "tts" in d and d["tts"] is not None:
            try:
                tts_ms = float(d["tts"]) * 1000.0
            except (TypeError, ValueError):
                continue
            by_tts["all"].append(tts_ms)
            if tts_eng:
                tts_labeled += 1
                by_tts[f"engine={tts_eng}"].append(tts_ms)
            else:
                tts_unlabeled += 1
                by_tts["engine=(unlabeled-legacy)"].append(tts_ms)
            if "tts_first" in d and d["tts_first"] is not None:
                try:
                    first_ms = float(d["tts_first"]) * 1000.0
                    key = f"engine={tts_eng}" if tts_eng else "engine=(unlabeled-legacy)"
                    by_tts_first[key].append(first_ms)
                    by_tts_first["all"].append(first_ms)
                except (TypeError, ValueError):
                    pass
            if "time_to_audio" in d and d["time_to_audio"] is not None:
                try:
                    tta = float(d["time_to_audio"]) * 1000.0
                    key = f"engine={tts_eng}" if tts_eng else "engine=(unlabeled-legacy)"
                    by_tta[key].append(tta)
                    by_tta["all"].append(tta)
                except (TypeError, ValueError):
                    pass

    print(f"DB: {db_path.resolve()}")
    print(f"Rows scanned: {len(rows)}")
    print(
        f"Translate samples with engine label: {labeled}  "
        f"legacy unlabeled: {unlabeled}"
    )
    print(
        f"TTS samples with engine label: {tts_labeled}  "
        f"legacy unlabeled: {tts_unlabeled}"
    )

    # Recent sessions — makes it obvious if Piper never wrote TTS rows yet
    print()
    print("=== Latest sessions (TTS engine counts) ===")
    cur.execute(
        """
        SELECT s.id, s.title, s.created_at
        FROM sessions s
        ORDER BY s.created_at DESC
        LIMIT 8
        """
    )
    for sid, title, created in cur.fetchall():
        cur.execute(
            "SELECT timing_json FROM chunks WHERE session_id = ?", (sid,)
        )
        from collections import Counter as _Counter

        eng = _Counter()
        tts_n = 0
        skip_n = 0
        chunk_n = 0
        for (tj,) in cur.fetchall():
            chunk_n += 1
            try:
                d = json.loads(tj or "{}")
            except Exception:
                continue
            if d.get("tts_skipped"):
                skip_n += 1
            if "tts" in d and d.get("tts") is not None:
                tts_n += 1
                eng[str(d.get("tts_engine") or "?")] += 1
        eng_s = ", ".join(f"{k}={v}" for k, v in sorted(eng.items())) or "—"
        print(
            f"  {created}  chunks={chunk_n} tts={tts_n} skipped={skip_n}  "
            f"[{eng_s}]  {sid}  ({title or ''})"
        )

    print()
    print("=== Pipeline stages (ms) ===")
    for k in ("stt", "translate", "tts", "time_to_audio", "total"):
        _summary(by_stage.get(k, []), k)

    print()
    print("=== Translate breakdown (ms) ===")
    for k in sorted(by_translate.keys()):
        _summary(by_translate[k], k)

    print()
    print("=== TTS breakdown — full synth (ms) ===")
    for k in sorted(by_tts.keys()):
        _summary(by_tts[k], k)
    print()
    print("=== TTS breakdown — first audio chunk (ms) ===")
    for k in sorted(by_tts_first.keys()):
        _summary(by_tts_first[k], k)
    print()
    print("=== time_to_audio by TTS engine (ms) ===")
    for k in sorted(by_tta.keys()):
        _summary(by_tta[k], k)

    print()
    print("=== How to A/B Groq vs Google ===")
    print("1) .env → TRANSLATION_ENGINE=llm   (Groq)  · pc off  · fale ~20 frases")
    print("2) .env → TRANSLATION_ENGINE=google         · pc off  · fale ~20 frases")
    print("3) python scripts/analyze_latency.py --since YYYY-MM-DD")
    print("   Compare lines: engine=llm  vs  engine=google  (p50/p95).")
    print("4) Cache HIT is always fastest — keep phrase cache ON in real use.")
    print()
    print("=== How to A/B Piper vs Edge TTS ===")
    print("1) .env → TTS_ENGINE=piper  · s ON · fale ~15 frases com áudio")
    print("2) .env → TTS_ENGINE=edge   · s ON · fale ~15 frases parecidas")
    print("3) python scripts/analyze_latency.py --since YYYY-MM-DD")
    print("   Compare: TTS engine=piper vs engine=edge (tts + tts_first).")
    print("   Boot must log 'Text-to-speech: Piper local' / 'edge-tts'.")
    if unlabeled and not labeled:
        print()
        print(
            "NOTE: This DB has no translate_engine yet. New sessions after this"
            " upgrade will be labeled; re-run the script after the A/B above."
        )
    if tts_unlabeled and not tts_labeled:
        print()
        print(
            "NOTE: No tts_engine labels yet — run Piper and Edge sessions after"
            " upgrade so chunks record tts_engine=piper|edge."
        )
    elif tts_labeled and "engine=piper" not in by_tts and "engine=edge" in by_tts:
        print()
        print(
            "NOTE: DB has Edge-labeled TTS but no Piper yet. "
            "Run a Piper session (TTS_ENGINE=piper, sound ON) then re-analyze."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
