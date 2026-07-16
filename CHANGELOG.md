# Changelog

All notable changes to LiveLingo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) where applicable.

## [Unreleased]

### Added

- **Sound OFF mode (`s`)** — process STT + translation without live TTS; optional full TTS skip (`TTS_SKIP_WHEN_MUTED`) for speed.
- **Parallel sound-OFF workers** — paragraph-split chunks processed in parallel with ordered display (`SOUND_OFF_PARALLEL`).
- **On-demand TTS for replay** — `[r]` / `[rN]` synthesize and cache WAV when a chunk has text but no audio (after muted sessions).
- **STT hallucination filter** (`livelingo/stt_filter.py`) — drop pure silence credits (`Legenda por…`, etc.) and strip trailing credit tails from long monologues.
- **Session chunk performance metrics** — persist `created_at` + timing JSON (STT / translate / TTS / first_audio / hear / total) in SQLite.
- **History list (`l`)** — show timing and registration timestamp below each source line (dim, separated by a blank line).
- **Export (`c`) word counter** — total multi-syllable content words on source text, excluding `e` / `a` / `ou` / `para` / `ao` / `à`.
- **Streaming UI** — live LLM translation tokens with single-line stream updates; print lock for parallel workers.
- **Piper / hybrid TTS, Silero VAD, synonyms** — additional engines and helpers (config + modules).
- **Interactive menu** — two-column fixed-width layout for terminal commands.

### Changed

- **Capture VAD** — adaptive silence, paragraph split (sound-OFF by default), drop near-silent tail chunks that trigger Whisper hallucinations.
- **Pipeline ordered release** — sync `_next_release` when toggling sound so muted chunks after a sound-ON session still publish Heard/Translated.
- **Markdown export (`c`)** — clean chunk layout: target, blank line, source, blank line between chunks; no timing/date in the file body.
- **DB schema** — `chunks.created_at`, `chunks.timing_json` with automatic migration on `init_db()`.

### Fixed

- Translation UI missing after Sound ON → OFF: ordered publisher cursor lagging behind sound-ON chunk numbers.
- Parallel workers interleaving filter messages with Heard/Translated lines (terminal print lock).
- Streaming overwrite corrupting long wrapped monologue lines.
- Menu columns misaligned after longer command labels.

## [0.1.0] — 2026-07-16

### Added

- Initial LiveLingo baseline (prior commits on this branch): SQLite sessions, interactive commands, Groq cloud STT, AI export summary.
