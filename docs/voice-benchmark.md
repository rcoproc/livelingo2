# Voice / translation latency benchmarks

**Date:** 2026-09-02  
**Machine / DB:** local `livelingo.db` (Windows LiveLingo sessions)  
**Tool:** `python scripts/analyze_latency.py`  
**Source of truth:** `chunks.timing_json` (`stt`, `translate`, `tts`, `tts_first`, `time_to_audio`, `translate_engine`, `tts_engine`)

This note captures A/B measurements for **translation engines** (Groq LLM vs Google) and **TTS engines** (Piper vs Edge), plus the config that followed from those results.

---

## How to re-run

```bat
cd C:\Users\rcopr\LiveLingo\LiveLingo
python scripts/analyze_latency.py
python scripts/analyze_latency.py --since 2026-09-02
python scripts/analyze_latency.py --session 20260902_014344
```

Labels appear only on **new** chunks after `translate_engine` / `tts_engine` were added to timing. Boot must show the intended runtime:

```text
TTS runtime: PiperSynthesizer · engine_name=piper · cfg TTS_ENGINE=piper TTS_HYBRID=False
LLM translation ready (Groq / …)
```

For TTS A/B, **Sound must be ON** (`[s]`); otherwise chunks get `tts_skipped` and no TTS timings.

---

## 1. Translation: Groq LLM vs Google

### Method

1. `pc off` (disable phrase cache) so misses are measured.
2. Session A: `TRANSLATION_ENGINE=google` · ~20 short phrases.
3. Session B: `TRANSLATION_ENGINE=llm` (Groq) · ~20 similar phrases.
4. Compare `engine=google` vs `engine=llm` in the script output.

### Results (voice path, labeled samples)

| Engine | n | p50 | p90 | p95 | mean | max |
|--------|---|-----|-----|-----|------|-----|
| **cache HIT** | 10 | **15 ms** | 42 ms | 42 ms | 19 ms | 42 ms |
| **llm (Groq)** | 65* | **~404–441 ms** | ~628–685 ms | ~641–899 ms | ~448–502 ms | ~1089–1471 ms |
| **google** | 26 | **1433 ms** | 2480 ms | 2956 ms | 1636 ms | 3400 ms |

\*LLM *n* grew across the day as more Groq sessions were recorded; the early A/B slice was **n=14** with p50 **388 ms** / p95 **799 ms** — same conclusion.

### Early A/B snapshot (user paste, first LLM batch)

| Engine | n | p50 | p95 |
|--------|---|-----|-----|
| google | 26 | 1433 ms | 2956 ms |
| llm | 14 | **388 ms** | **799 ms** |

**Groq LLM ≈ 3.7× faster than Google** at p50 on that slice.

### Conclusion (translate)

```
cache (~15 ms)  ≪  Groq LLM (~400 ms)  ≪  Google (~1400 ms)
```

**Recommendation**

```env
TRANSLATION_ENGINE=llm
GROQ_MODEL=llama-3.3-70b-versatile   # or whatever passed self-test
```

Keep **phrase cache ON** in real use (`pc on`). Keep Google as HA fallback only.

### Pitfalls seen

- `llama-3.1-8b-instant` returned **404** for this key → self-test failed → silent fall back to Google; misleading “No Groq API key” help text (fixed to distinguish model vs key errors).
- Prefer checking boot: `LLM translation ready (Groq / …)`.

---

## 2. TTS: Piper vs Edge

### Method

1. `TTS_ENGINE=piper` + **`TTS_HYBRID=false`** (critical on Windows).
2. Boot must log `TTS runtime: PiperSynthesizer · engine_name=piper`.
3. Sound **ON** · ~15 phrases with audible playback.
4. Repeat with `TTS_ENGINE=edge`.
5. Compare `engine=piper` vs `engine=edge` (`tts`, `tts_first`, `time_to_audio`).

### Results (labeled samples, `--since 2026-09-02`)

User paste (first clean Piper batch, n=9):

| Metric | Piper (n=9) | Edge (n=39) | Speedup (p50) |
|--------|-------------|-------------|---------------|
| **TTS full** p50 | **128 ms** | **2171 ms** | **~17×** |
| **TTS full** p95 | **159 ms** | **3770 ms** | **~24×** |
| **First audio** p50 | **126 ms** | **2171 ms** | **~17×** |
| **time_to_audio** p50 | **1514 ms** | **3382 ms** | **~2.2×** |

Refreshed DB aggregate later the same day (more Piper chunks):

| Metric | Piper (n=13) | Edge (n=48) |
|--------|--------------|-------------|
| TTS full p50 / p95 | **136 / 240 ms** | **2391 / 3548 ms** |
| First audio p50 / p95 | **133 / 166 ms** | **2391 / 3548 ms** |
| time_to_audio p50 | **1464 ms** | **3407 ms** |

Microbenchmark (Linux/WSL, Piper only — Edge package missing in that env):

| Text | first audio | synth total |
|------|-------------|-------------|
| ~28 chars | ~50–60 ms | ~50–60 ms |
| ~91 chars | ~70 ms | ~160 ms |

### Conclusion (TTS)

```
Piper first-audio (~130 ms)  ≪  Edge first-audio (~2200–2700 ms)
```

`time_to_audio` still includes STT + translate before TTS, so it does not drop to ~130 ms end-to-end.

**Recommendation**

```env
TTS_ENGINE=piper
TTS_HYBRID=false
```

Use Edge/hybrid only if Piper voice quality is unacceptable for the use case.

### Pitfalls seen

1. **Windows default `TTS_HYBRID=true`** — with `TTS_ENGINE=piper`, runtime becomes **hybrid (edge first + piper)**. Header showed `TTS: hybrid (…)`. Fix: `TTS_HYBRID=false`.
2. **Piper failed to load** on Windows Python → silent fall back to Edge while cfg still said piper. Boot now prints:
   `TTS runtime: … engine_name=…` and warns if cfg≠runtime.
3. **Session named `tts-piper` still wrote `edge=9`** — Piper was not the active synthesizer for those chunks.
4. **Sound OFF** → `tts_skipped`, no TTS timings (easy to misread as “Piper not working”).
5. **Female voice** — auto EN default was `en_US-lessac` (female). Switched to male `en_US-ryan-low` (see below).

---

## 3. Recommended production stack (from these benchmarks)

```env
TRANSLATION_ENGINE=llm
GROQ_MODEL=llama-3.3-70b-versatile
TTS_ENGINE=piper
TTS_HYBRID=false
PIPER_VOICE=en_US-ryan-low
PIPER_QUALITY=fast
```

Plus phrase cache **ON** in normal sessions.

### Latency stack (order of magnitude from this DB)

| Stage | Typical p50 |
|-------|-------------|
| Phrase cache HIT | ~15 ms |
| Piper TTS | ~130 ms |
| Groq LLM translate | ~400 ms |
| STT (this day) | ~400–430 ms |
| Google translate | ~1400 ms |
| Edge TTS | ~2200–2700 ms |

---

## 4. Piper voice (male EN)

| Voice ID | Notes |
|----------|--------|
| `en_US-lessac-*` | Female (previous auto default) |
| `en_US-amy-*` | Female |
| **`en_US-ryan-low`** | **Male US, fast — configured** |
| `en_US-ryan-medium` | Male US, higher quality |
| `en_US-joe-medium` | Male US alternative |
| `en_GB-northern_english_male-medium` | Male UK |

Registered in `livelingo/piper_voices.py`; `.env` sets `PIPER_VOICE=en_US-ryan-low`. Default EN auto-pick is now `en_US-ryan-medium` (maps to `-low` when `PIPER_QUALITY=fast`).

Samples: [piper-samples](https://rhasspy.github.io/piper-samples) · models: [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).

---

## 5. Related tooling / code

| Piece | Role |
|-------|------|
| `scripts/analyze_latency.py` | p50/p90/p95 by stage + translate/TTS engine; lists latest sessions |
| `scripts/check_piper.py` | Diagnose `piper-tts` / `onnxruntime` on the **same** Python as `main.py` |
| `timing_json.translate_engine` | `llm` \| `google` \| `cache` |
| `timing_json.tts_engine` | `piper` \| `edge` \| `hybrid` |
| Boot `TTS runtime:` line | Confirms synthesizer class vs `.env` |

---

## 6. Session breadcrumbs (2026-09-02)

| Session | Notes |
|---------|--------|
| `…_analise-latencia-traducao` | Early translate A/B (mostly Google; sound often off) |
| `…_teste-comparativo-edge` | Edge TTS labeled |
| `…_tts-piper` | Named piper but **recorded edge=9** (runtime was edge) |
| `…_014344_1` | Mixed edge=9 + piper=9 (first clean Piper TTS timings) |
| `…_voz-masculina-piper` | Piper after ryan male voice switch |

---

*Generated from LiveLingo latency A/B work on 2026-09-02. Re-run `scripts/analyze_latency.py` to refresh numbers.*
