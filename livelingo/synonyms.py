"""
synonyms.py
===========
Offline English synonym lookup for the [o] terminal command.

Uses Princeton WordNet (downloaded once to .cache/nltk) with a Moby Thesaurus
fallback for words missing from WordNet. Portuguese glosses use Google Translate
via deep-translator (same stack as the main pipeline); definitions and examples
still work in English when translation is unavailable.

Set SYNONYMS_ENGINE=llm in .env to keep the richer Groq-only explanations.
"""

from __future__ import annotations

import os
import urllib.request

from deep_translator import GoogleTranslator

MOBY_URL = "https://www.gutenberg.org/files/3202/files/mthesaur.txt"
MOBY_CACHE = os.path.join(".cache", "models", "moby_thesaurus.txt")

_POS_ORDER = ("n", "v", "a", "r", "s")


class SynonymError(Exception):
    """Raised when a synonym lookup cannot be completed."""


class SynonymLookup:
    def __init__(self, config, llm_translator=None, log=print):
        self.cfg = config
        self._llm = llm_translator
        self._log = log
        self._engine = (getattr(config, "SYNONYMS_ENGINE", "wordnet") or "wordnet").lower()
        self._pt_translate = getattr(config, "SYNONYMS_PT_TRANSLATE", True)
        self._wordnet = None
        self._moby_index = None
        self._pt_translator = None

    # ------------------------------------------------------------------ #
    def explain(self, word):
        word = (word or "").strip()
        if not word:
            raise SynonymError("Empty word.")

        if self._engine == "llm":
            return self._explain_llm(word)

        try:
            return self._explain_wordnet(word)
        except SynonymError:
            if self._engine == "auto" and self._llm is not None:
                return self._explain_llm(word)
            raise

    # ------------------------------------------------------------------ #
    def _explain_llm(self, word):
        if self._llm is None or not hasattr(self._llm, "explain_synonyms"):
            raise SynonymError(
                "SYNONYMS_ENGINE=llm requires TRANSLATION_ENGINE=llm with a valid GROQ_API_KEY."
            )
        return self._llm.explain_synonyms(word)

    def _explain_wordnet(self, word):
        entry = self._lookup_wordnet(word)
        if entry is None:
            entry = self._lookup_moby(word)
        if entry is None:
            raise SynonymError(
                f"No entry found for '{word}'. Try another spelling or part of speech."
            )

        definition, synonyms, examples = entry
        pt_definition = self._to_portuguese(definition) if self._pt_translate else None
        pt_synonyms = self._translate_synonym_glosses(synonyms[:8]) if self._pt_translate else {}

        lines = ["1. **Significado e Uso**:"]
        if pt_definition:
            lines.append(
                f"A palavra '{word}' pode ser entendida como: {pt_definition}"
            )
            lines.append(f"_(Definição em inglês: {definition})_")
        else:
            lines.append(definition)

        lines.append("")
        lines.append("2. **Sinônimos Comuns em Inglês**:")
        if synonyms:
            for syn in synonyms[:8]:
                gloss = pt_synonyms.get(syn.lower())
                if gloss:
                    lines.append(f"- {syn} ({gloss})")
                else:
                    lines.append(f"- {syn}")
        else:
            lines.append("- _(nenhum sinônimo próximo encontrado)_")

        lines.append("")
        lines.append("3. **Exemplos Práticos com Sinônimos**:")
        if examples:
            for ex in examples[:3]:
                highlighted = self._highlight_synonym(ex, synonyms, word)
                pt_ex = self._to_portuguese(ex) if self._pt_translate else None
                lines.append(f"- **Frase em Inglês**: {highlighted}")
                if pt_ex:
                    lines.append(f"  **Tradução**: {pt_ex}")
                lines.append("")
        else:
            lines.append(
                "- _(WordNet não tem frases de exemplo para esta palavra — "
                "use os sinônimos acima em suas próprias frases.)_"
            )

        if not self._pt_translate:
            lines.append("")
            lines.append(
                "_(Traduções em português desativadas — defina SYNONYMS_PT_TRANSLATE=true "
                "e verifique a conexão com a internet.)_"
            )

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    def _lookup_wordnet(self, word):
        wn = self._ensure_wordnet()
        key = word.lower().replace(" ", "_")
        synsets = []
        for pos in _POS_ORDER:
            synsets.extend(wn.synsets(key, pos=pos))
        if not synsets:
            synsets = wn.synsets(key)
        if not synsets:
            return None

        primary = self._best_synset(key, synsets)
        definition = primary.definition().strip()
        primary_pos = primary.pos()
        ranked = [primary] + [
            s for s in synsets if s is not primary and s.pos() == primary_pos
        ] + [s for s in synsets if s.pos() != primary_pos]

        synonyms = []
        seen = {key}

        def collect_from(group):
            for syn in group:
                names = [lemma.name().replace("_", " ").lower() for lemma in syn.lemmas()]
                if key not in names:
                    continue
                idx = names.index(key)
                # Skip synsets where the word is only a peripheral tag (e.g. fast ~ debauched).
                if idx == len(names) - 1 and len(names) >= 4:
                    continue
                for name in names:
                    if name not in seen:
                        seen.add(name)
                        synonyms.append(name)

        same_pos = [s for s in synsets if s.pos() == primary_pos]
        other_pos = [s for s in synsets if s.pos() != primary_pos]
        collect_from(same_pos)
        # Related adjective satellites (pos=s) often hold the best synonym lists.
        if primary_pos == "a":
            collect_from([s for s in other_pos if s.pos() == "s"])
            other_pos = [s for s in other_pos if s.pos() not in ("s", "n")]
        collect_from(other_pos)
        synonyms = synonyms[:12]

        examples = []
        for syn in ranked[:5]:
            for ex in syn.examples():
                ex = ex.strip()
                if ex and ex not in examples:
                    examples.append(ex)
                if len(examples) >= 3:
                    break
            if len(examples) >= 3:
                break

        return definition, synonyms, examples

    def _lookup_moby(self, word):
        index = self._ensure_moby()
        key = word.lower().strip()
        related = index.get(key)
        if not related:
            return None
        synonyms = [w for w in sorted(related) if w != key][:12]
        definition = (
            f"Moby Thesaurus groups '{word}' with related English words "
            "(no formal dictionary definition for this entry)."
        )
        return definition, synonyms, []

    @staticmethod
    def _best_synset(key, synsets):
        """Pick the most likely sense for vocabulary lookup (e.g. fast=quick, not fasting)."""
        # Prefer core adjective/noun senses over satellite adjectives (pos=s) and verbs.
        pos_rank = {"a": 5, "n": 4, "v": 3, "r": 2, "s": 1}

        def score(syn):
            lemmas = syn.lemmas()
            names = [lemma.name().lower() for lemma in lemmas]
            head_match = bool(names) and names[0] == key
            alt_synonyms = len({name for name in names if name != key})
            return (head_match, pos_rank.get(syn.pos(), 0), alt_synonyms)

        return max(synsets, key=score)

    # ------------------------------------------------------------------ #
    def _ensure_wordnet(self):
        if self._wordnet is not None:
            return self._wordnet
        try:
            import nltk
            from nltk.corpus import wordnet as wn
        except ImportError as exc:
            raise SynonymError(
                "WordNet requires nltk. Install with: pip install nltk"
            ) from exc

        cache_dir = os.path.join(".cache", "nltk")
        os.makedirs(cache_dir, exist_ok=True)
        if cache_dir not in nltk.data.path:
            nltk.data.path.insert(0, cache_dir)

        missing = []
        for pkg in ("wordnet", "omw-1.4"):
            try:
                nltk.data.find(
                    "corpora/wordnet" if pkg == "wordnet" else "corpora/omw-1.4"
                )
            except LookupError:
                missing.append(pkg)
        if missing:
            self._log("Downloading WordNet dictionary (one-time, ~10 MB)...")
            for pkg in missing:
                nltk.download(pkg, download_dir=cache_dir, quiet=True)

        self._wordnet = wn
        return wn

    def _ensure_moby(self):
        if self._moby_index is not None:
            return self._moby_index

        os.makedirs(os.path.dirname(MOBY_CACHE), exist_ok=True)
        if not os.path.exists(MOBY_CACHE):
            self._log("Downloading Moby Thesaurus fallback (one-time)...")
            urllib.request.urlretrieve(MOBY_URL, MOBY_CACHE)

        index = {}
        with open(MOBY_CACHE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                words = [
                    w.strip().lower()
                    for w in line.split(",")
                    if w.strip() and w.strip().isascii()
                ]
                if len(words) < 2:
                    continue
                bucket = set(words)
                for w in words:
                    index.setdefault(w, set()).update(bucket)

        self._moby_index = index
        return index

    # ------------------------------------------------------------------ #
    def _pt_google(self):
        if self._pt_translator is None:
            self._pt_translator = GoogleTranslator(source="en", target="pt")
        return self._pt_translator

    def _to_portuguese(self, text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return (self._pt_google().translate(text) or "").strip() or None
        except Exception:
            return None

    def _translate_synonym_glosses(self, synonyms):
        if not synonyms:
            return {}
        try:
            joined = ", ".join(synonyms)
            pt = self._to_portuguese(joined)
            if not pt:
                return {}
            parts = [p.strip() for p in pt.split(",")]
            out = {}
            for syn, gloss in zip(synonyms, parts):
                if gloss:
                    out[syn.lower()] = gloss
            return out
        except Exception:
            return {}

    @staticmethod
    def _highlight_synonym(sentence, synonyms, word):
        lowered = sentence.lower()
        for syn in synonyms:
            if syn.lower() in lowered:
                idx = lowered.find(syn.lower())
                original = sentence[idx : idx + len(syn)]
                return sentence.replace(original, f"*{original}*", 1)
        if word.lower() in lowered:
            idx = lowered.find(word.lower())
            original = sentence[idx : idx + len(word)]
            return sentence.replace(original, f"*{original}*", 1)
        return sentence


def build_synonym_lookup(config, translator=None, log=print):
    """Factory used by main.py; passes LLM translator only for llm/auto modes."""
    llm = translator if hasattr(translator, "explain_synonyms") else None
    return SynonymLookup(config, llm_translator=llm, log=log)