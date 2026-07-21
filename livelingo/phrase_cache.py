"""
phrase_cache.py
===============
In-memory + SQLite exact phrase translation memory (TM).

Preserves context by caching **full sentences**, not word-for-word glue.

Lookup key: (source_lang, target_lang, normalize(heard_text))
HIT → skip Google/LLM. MISS → live translate → store.

Runtime control (see main commands [pc …]):
  pc on / pc off     enable/disable without restart
  pc force           next chunk ignores cache and overwrites store
  pc last            show last HIT for quality review
  pc good / pc bad   mark last HIT quality
  pc undo            restore previous target from history
  pc backup          JSON snapshot under .cache/
  pc restore [file]  restore pairs from a backup JSON
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from typing import Any

from . import db

# Default backup directory (project-local)
_BACKUP_DIR = os.path.join(".cache", "phrase_cache_backups")

# Collapse whitespace / strip most punctuation for matching (keep letters/digits)
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_phrase(text: str) -> str:
    """
    Normalize source phrase for exact TM lookup.

    - Unicode NFKC + casefold
    - strip most punctuation (keeps word characters / spaces)
    - collapse whitespace
    """
    s = unicodedata.normalize("NFKC", (text or "").strip())
    if not s:
        return ""
    s = s.casefold()
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


class PhraseCache:
    """
    Thread-safe phrase cache with optional SQLite persistence and JSON backups.
    """

    def __init__(self, config=None):
        self.cfg = config
        self._lock = threading.RLock()
        # OrderedDict as LRU: key -> target_text
        self._mem: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._max = int(getattr(config, "PHRASE_CACHE_SIZE", 10000) or 10000)
        self._enabled = bool(getattr(config, "PHRASE_CACHE", False))
        self._log = bool(getattr(config, "PHRASE_CACHE_LOG", True))
        # Next translate ignores HIT and refreshes store
        self._force_next = False
        # Stats
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.forced = 0
        # Last lookup result for [pc last] / good / bad / undo
        self.last_event: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # Enable / force
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, on: bool) -> bool:
        with self._lock:
            self._enabled = bool(on)
            return self._enabled

    def request_force_next(self) -> None:
        """Next successful translate path: skip HIT, re-translate, overwrite."""
        with self._lock:
            self._force_next = True

    def clear_force_next(self) -> None:
        with self._lock:
            self._force_next = False

    def consume_force_next(self) -> bool:
        with self._lock:
            if not self._force_next:
                return False
            self._force_next = False
            self.forced += 1
            return True

    def force_pending(self) -> bool:
        with self._lock:
            return self._force_next

    # ------------------------------------------------------------------ #
    # Core lookup / store
    # ------------------------------------------------------------------ #
    def lookup(
        self,
        source_lang: str,
        target_lang: str,
        heard_text: str,
        *,
        allow: bool = True,
    ) -> str | None:
        """
        Return cached target text or None.

        Skips when disabled, empty text, or force_next is set (caller should
        call consume_force_next separately before translate).
        """
        if not allow or not self.enabled:
            return None
        heard = (heard_text or "").strip()
        if not heard:
            return None
        src = (source_lang or "").lower().strip()
        tgt = (target_lang or "").lower().strip()
        norm = normalize_phrase(heard)
        if not norm or not src or not tgt:
            return None

        key = (src, tgt, norm)
        with self._lock:
            if self._force_next:
                return None
            # Memory first
            if key in self._mem:
                self._mem.move_to_end(key)
                target = self._mem[key]
                self.hits += 1
                self.last_event = {
                    "kind": "hit",
                    "source_lang": src,
                    "target_lang": tgt,
                    "source_norm": norm,
                    "source_text": heard,
                    "target_text": target,
                    "layer": "memory",
                    "t": time.time(),
                }
                try:
                    db.touch_translation_pair_hit(src, tgt, norm)
                except Exception:
                    pass
                return target

        # SQLite
        try:
            row = db.get_translation_pair(src, tgt, norm)
        except Exception:
            row = None
        if not row or not (row.get("target_text") or "").strip():
            with self._lock:
                self.misses += 1
                self.last_event = {
                    "kind": "miss",
                    "source_lang": src,
                    "target_lang": tgt,
                    "source_norm": norm,
                    "source_text": heard,
                    "target_text": "",
                    "layer": None,
                    "t": time.time(),
                }
            return None

        target = (row["target_text"] or "").strip()
        with self._lock:
            self._mem_put(key, target)
            self.hits += 1
            self.last_event = {
                "kind": "hit",
                "source_lang": src,
                "target_lang": tgt,
                "source_norm": norm,
                "source_text": heard,
                "target_text": target,
                "layer": "sqlite",
                "quality": row.get("quality"),
                "hit_count": row.get("hit_count"),
                "t": time.time(),
            }
        try:
            db.touch_translation_pair_hit(src, tgt, norm)
        except Exception:
            pass
        return target

    def store(
        self,
        source_lang: str,
        target_lang: str,
        heard_text: str,
        translated_text: str,
        *,
        from_force: bool = False,
    ) -> dict[str, Any] | None:
        """
        Persist phrase pair to memory + SQLite.
        Returns info dict including previous_target if overwritten.
        """
        if not self.enabled and not from_force:
            # Still allow store when force-refreshing even if later disabled? No —
            # only store when cache is on OR this store is the result of force.
            pass
        if not self.enabled:
            return None

        heard = (heard_text or "").strip()
        translated = (translated_text or "").strip()
        if not heard or not translated:
            return None
        src = (source_lang or "").lower().strip()
        tgt = (target_lang or "").lower().strip()
        norm = normalize_phrase(heard)
        if not norm:
            return None

        key = (src, tgt, norm)
        prev = None
        pair_id = None
        try:
            pair_id, prev = db.upsert_translation_pair(
                src, tgt, norm, heard, translated, bump_hit=False
            )
        except Exception:
            pair_id, prev = None, None

        with self._lock:
            self._mem_put(key, translated)
            self.stores += 1
            self.last_event = {
                "kind": "store",
                "source_lang": src,
                "target_lang": tgt,
                "source_norm": norm,
                "source_text": heard,
                "target_text": translated,
                "previous_target": prev,
                "from_force": from_force,
                "pair_id": pair_id,
                "t": time.time(),
            }
        return self.last_event

    def _mem_put(self, key: tuple[str, str, str], target: str) -> None:
        self._mem[key] = target
        self._mem.move_to_end(key)
        while len(self._mem) > max(1, self._max):
            self._mem.popitem(last=False)

    # ------------------------------------------------------------------ #
    # Quality / undo (last HIT)
    # ------------------------------------------------------------------ #
    def mark_last_quality(self, quality: str) -> tuple[bool, str]:
        """
        Mark last HIT (or store) as good/bad.
        Returns (ok, message).
        """
        q = (quality or "").strip().lower()
        if q not in ("good", "bad"):
            return False, "Use: pc good | pc bad"
        with self._lock:
            ev = self.last_event
        if not ev or ev.get("kind") not in ("hit", "store"):
            return (
                False,
                "Nenhum HIT/store recente para marcar. Rode uma frase com cache.",
            )
        src, tgt, norm = ev["source_lang"], ev["target_lang"], ev["source_norm"]
        try:
            ok = db.set_translation_pair_quality(src, tgt, norm, q)
        except Exception as exc:
            return False, f"Erro ao marcar qualidade: {exc}"
        if not ok:
            return False, "Par não encontrado no SQLite."
        with self._lock:
            if self.last_event:
                self.last_event["quality"] = q
        return (
            True,
            f"Qualidade marcada: {q} para «{(ev.get('source_text') or '')[:60]}»",
        )

    def undo_last(self) -> tuple[bool, str]:
        """Restore previous target for last HIT/store pair from history."""
        with self._lock:
            ev = self.last_event
        if not ev:
            return False, "Nada para desfazer (sem evento recente)."
        src, tgt, norm = ev["source_lang"], ev["target_lang"], ev["source_norm"]
        try:
            restored = db.undo_translation_pair(src, tgt, norm)
        except Exception as exc:
            return False, f"Erro no undo: {exc}"
        if not restored:
            return False, "Sem histórico anterior para este par (nada a restaurar)."
        key = (src, tgt, norm)
        with self._lock:
            self._mem_put(key, restored)
            if self.last_event:
                self.last_event["target_text"] = restored
                self.last_event["kind"] = "undo"
        return True, f"Restaurado target anterior:\n  «{restored}»"

    def format_last(self) -> str:
        with self._lock:
            ev = dict(self.last_event) if self.last_event else None
            en = self._enabled
            force = self._force_next
            stats = (self.hits, self.misses, self.stores, self.forced, len(self._mem))
        if not ev:
            return (
                f"Phrase cache: {'ON' if en else 'OFF'} · force_next={force}\n"
                f"Stats hits={stats[0]} misses={stats[1]} stores={stats[2]} "
                f"forced={stats[3]} mem={stats[4]}\n"
                f"Sem evento recente."
            )
        lines = [
            f"Phrase cache: {'ON' if en else 'OFF'} · force_next={force}",
            f"Stats hits={stats[0]} misses={stats[1]} stores={stats[2]} "
            f"forced={stats[3]} mem={stats[4]}",
            f"Last: {ev.get('kind')} · {ev.get('source_lang')}→{ev.get('target_lang')} "
            f"· layer={ev.get('layer')}",
            f"  quality={ev.get('quality')!r}",
            f"  SRC: {(ev.get('source_text') or '')[:200]}",
            f"  TGT: {(ev.get('target_text') or '')[:200]}",
        ]
        if ev.get("previous_target"):
            lines.append(f"  PREV: {str(ev['previous_target'])[:200]}")
        lines.append(
            "Avaliar: [pc good] / [pc bad] · Desfazer overwrite: [pc undo] · "
            "Forçar live: [pc force]"
        )
        return "\n".join(lines)

    def stats_line(self) -> str:
        with self._lock:
            total = self.hits + self.misses
            rate = (100.0 * self.hits / total) if total else 0.0
            return (
                f"cache={'ON' if self._enabled else 'OFF'} "
                f"hits={self.hits} misses={self.misses} "
                f"hit_rate={rate:.1f}% stores={self.stores} "
                f"forced={self.forced} mem={len(self._mem)} "
                f"force_next={self._force_next}"
            )

    # ------------------------------------------------------------------ #
    # Warm-up
    # ------------------------------------------------------------------ #
    def warmup(
        self,
        source_lang: str,
        target_lang: str,
        *,
        chunk_origin: str | None = "voice",
    ) -> int:
        """
        Load pairs into RAM only (fast).

        - Reads `translation_pairs` for this lang pair
        - Optionally seeds RAM from recent `chunks` matching *chunk_origin*
          (``voice`` | ``livecaptions`` | None=all). Avoids mixing LC EN→PT
          rows into the voice PT→EN warm-up (and vice-versa).

        Returns number of entries loaded into memory.
        """
        if not getattr(self.cfg, "PHRASE_CACHE_WARMUP", True):
            return 0
        src = (source_lang or "").lower().strip()
        tgt = (target_lang or "").lower().strip()
        loaded = 0
        max_n = max(1, min(int(self._max), 5000))

        # 1) Existing translation_pairs (already curated / stored) — fast SELECT
        try:
            pairs = db.list_translation_pairs(src, tgt, limit=max_n)
        except Exception:
            pairs = []
        with self._lock:
            for p in pairs:
                norm = p.get("source_norm") or normalize_phrase(
                    p.get("source_text") or ""
                )
                target = (p.get("target_text") or "").strip()
                if not norm or not target:
                    continue
                self._mem_put((src, tgt, norm), target)
                loaded += 1

        # 2) Seed RAM from recent chunks only (no INSERT/UPDATE per row)
        try:
            chunk_limit = min(max_n, 1500)
            chunks = db.load_chunks_for_warmup(limit=chunk_limit, origin=chunk_origin)
        except Exception:
            chunks = []
        with self._lock:
            for heard, translated in chunks:
                heard = (heard or "").strip()
                translated = (translated or "").strip()
                if not heard or not translated:
                    continue
                norm = normalize_phrase(heard)
                if not norm:
                    continue
                key = (src, tgt, norm)
                if key not in self._mem:
                    self._mem_put(key, translated)
                    loaded += 1
        return loaded

    # ------------------------------------------------------------------ #
    # Backup / restore
    # ------------------------------------------------------------------ #
    def backup(self, path: str | None = None) -> str:
        """
        Write all translation_pairs to a JSON file.
        Returns the path written.
        """
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        if not path:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(_BACKUP_DIR, f"phrase_cache_{stamp}.json")
        pairs = db.list_translation_pairs(limit=100000)
        payload = {
            "version": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(pairs),
            "pairs": pairs,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        # Keep a rolling "latest" pointer
        latest = os.path.join(_BACKUP_DIR, "phrase_cache_latest.json")
        try:
            with open(latest, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return path

    def restore(
        self, path: str | None = None, *, replace: bool = False
    ) -> tuple[int, str]:
        """
        Load pairs from a backup JSON into SQLite + memory.

        replace=False: upsert only (default, safer).
        Returns (count_restored, path_used).
        """
        if not path:
            path = os.path.join(_BACKUP_DIR, "phrase_cache_latest.json")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Backup não encontrado: {path}")
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        pairs = payload.get("pairs") or []
        n = 0
        for p in pairs:
            src = (p.get("source_lang") or "").lower().strip()
            tgt = (p.get("target_lang") or "").lower().strip()
            heard = (p.get("source_text") or "").strip()
            translated = (p.get("target_text") or "").strip()
            norm = (p.get("source_norm") or normalize_phrase(heard)).strip()
            if not src or not tgt or not norm or not translated:
                continue
            try:
                db.upsert_translation_pair(
                    src,
                    tgt,
                    norm,
                    heard,
                    translated,
                    bump_hit=False,
                    quality=p.get("quality"),
                )
            except Exception:
                continue
            with self._lock:
                self._mem_put((src, tgt, norm), translated)
            n += 1
        return n, path


# Process-wide singleton (set from Pipeline)
_CACHE: PhraseCache | None = None
_CACHE_LOCK = threading.Lock()


def get_phrase_cache(config=None) -> PhraseCache:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = PhraseCache(config)
        elif config is not None and _CACHE.cfg is None:
            _CACHE.cfg = config
        return _CACHE


def init_phrase_cache(config) -> PhraseCache:
    """Create/replace singleton and optionally warm up voice + LC lang pairs."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = PhraseCache(config)
        cache = _CACHE
    if cache.enabled and getattr(config, "PHRASE_CACHE_WARMUP", True):
        try:
            n_voice = cache.warmup(
                getattr(config, "SOURCE_LANG", "en"),
                getattr(config, "TARGET_LANG", "en"),
                chunk_origin="voice",
            )
            n_lc = 0
            # Live Captions pair (default invert: TARGET→SOURCE) — separate keys
            try:
                from .livecaptions import caption_lang_pair

                lc_src, lc_tgt = caption_lang_pair(config)
                if (lc_src, lc_tgt) != (
                    (getattr(config, "SOURCE_LANG", "") or "").lower(),
                    (getattr(config, "TARGET_LANG", "") or "").lower(),
                ):
                    n_lc = cache.warmup(lc_src, lc_tgt, chunk_origin="livecaptions")
            except Exception:
                n_lc = 0
            n = int(n_voice or 0) + int(n_lc or 0)
            if getattr(config, "VERBOSE", False) or getattr(
                config, "PHRASE_CACHE_LOG", True
            ):
                cache._warmup_count = n  # type: ignore[attr-defined]
                cache._warmup_voice = int(n_voice or 0)  # type: ignore[attr-defined]
                cache._warmup_lc = int(n_lc or 0)  # type: ignore[attr-defined]
        except Exception:
            cache._warmup_count = 0  # type: ignore[attr-defined]
    return cache


def _count_words(text: str) -> int:
    """Rough word count (whitespace tokens after light cleanup)."""
    s = (text or "").strip()
    if not s:
        return 0
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return len(s.split()) if s else 0


def format_cache_inventory_summary(
    config=None, cache: PhraseCache | None = None
) -> list[str]:
    """
    Human-readable lines for TUI startup (Tradução tab).

    Summarizes SQLite translation_pairs: pairs per language direction,
    approximate word totals for source/target text, quality marks, RAM status.
    """
    lines: list[str] = []
    enabled = bool(getattr(config, "PHRASE_CACHE", False)) if config else False
    mem_n = 0
    if cache is not None:
        try:
            enabled = cache.enabled
            with cache._lock:
                mem_n = len(cache._mem)
        except Exception:
            pass

    try:
        inv = db.translation_pairs_inventory(limit=50000)
    except Exception:
        inv = []

    if not inv:
        lines.append(
            f"[dim]Cache de frases: {'ON' if enabled else 'OFF'} · "
            f"0 pares no SQLite · mem={mem_n} · [pc on] para usar TM[/]"
        )
        return lines

    # Aggregate by language pair
    by_pair: dict[tuple[str, str], dict[str, int]] = {}
    total_pairs = 0
    total_src_words = 0
    total_tgt_words = 0
    n_good = 0
    n_bad = 0
    n_unmarked = 0
    total_hits = 0

    for p in inv:
        src = p.get("source_lang") or "?"
        tgt = p.get("target_lang") or "?"
        key = (src, tgt)
        bucket = by_pair.setdefault(
            key,
            {
                "pairs": 0,
                "src_words": 0,
                "tgt_words": 0,
                "hits": 0,
                "good": 0,
                "bad": 0,
            },
        )
        sw = _count_words(p.get("source_text") or "")
        tw = _count_words(p.get("target_text") or "")
        bucket["pairs"] += 1
        bucket["src_words"] += sw
        bucket["tgt_words"] += tw
        bucket["hits"] += int(p.get("hit_count") or 0)
        q = (p.get("quality") or "").lower()
        if q == "good":
            bucket["good"] += 1
            n_good += 1
        elif q == "bad":
            bucket["bad"] += 1
            n_bad += 1
        else:
            n_unmarked += 1
        total_pairs += 1
        total_src_words += sw
        total_tgt_words += tw
        total_hits += int(p.get("hit_count") or 0)

    status = "ON" if enabled else "OFF"
    lines.append(
        f"[bold cyan]Cache de frases[/] [{status}] · "
        f"[bold]{total_pairs}[/] par(es) · "
        f"~[bold]{total_src_words}[/] palavras src · "
        f"~[bold]{total_tgt_words}[/] palavras tgt · "
        f"mem={mem_n}"
    )

    # Per-direction breakdown (sorted by pair count)
    ordered = sorted(by_pair.items(), key=lambda kv: -kv[1]["pairs"])
    parts = []
    for (src, tgt), b in ordered[:8]:
        parts.append(
            f"{src.upper()}→{tgt.upper()}: {b['pairs']} frases "
            f"(~{b['src_words']}→~{b['tgt_words']} pal.)"
        )
    if parts:
        lines.append("[dim]" + " · ".join(parts) + "[/]")

    # Quality summary if any marks exist
    if n_good or n_bad:
        lines.append(
            f"[dim]Qualidade: good={n_good} · bad={n_bad} · "
            f"sem marca={n_unmarked} · hits acumulados={total_hits} · "
            f"[pc last]/[pc good|bad][/]"
        )
    else:
        lines.append(
            f"[dim]hits acumulados={total_hits} · "
            f"sem marcas de qualidade · [pc on|off] · [pc last] · [pc force][/]"
        )
    return lines
