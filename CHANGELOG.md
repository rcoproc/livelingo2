# Changelog

All notable changes to LiveLingo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) where applicable.

## [Unreleased]

### Added

- **TUI mode (`UI_MODE=tui`, default)** — Textual full-screen UI: scrollable log, command input, **fixed listen status bar at the bottom** (pair + áudio ON/OFF + localized wait text). Classic CLI via `UI_MODE=classic`. Requires `pip install textual`.
- **Live TTS default OFF** — translation audio starts muted (text-only); enable with `[s]`. Robot status line highlights `🔇 ÁUDIO OFF → [s] para ouvir` (or `🔊 ÁUDIO ON`). Replay `[r]` still re-enables sound when needed.
- **Audio file references** — each chunk log and command `l` show `audio:` (absolute host path only); `a`/`aN` copy path to clipboard; `p`/`pN` open Explorer on the WAV (attach in WhatsApp/Teams). WSL `/mnt/c` → `C:\` conversion.
- **Set TARGET language (`t`)** — prompt for EN/PT/ES/FR/DE/IT/ZH/JA; updates `TARGET_LANG` + translator + Edge/Piper voice; SOURCE/STT unchanged. Next chunks use the new target.
- **Dev auto-reload** (`dev_reload.py`) — watch project `*.py` and restart `main.py` on save (CLI has no built-in hot reload). Optional `--verbose` / `--debounce`.
- **Language swap (`g`)** — invert `SOURCE_LANG` ↔ `TARGET_LANG` at runtime (STT + Google/LLM translator + Edge/Piper TTS rebind). Yellow menu line shows `EN → PT` / flipped pair. Optional `TTS_VOICE_ALT` for the other side’s voice; defaults to an elegant Edge voice per language if unset. Does not rewrite historical chunks.
- **Command priority over listen icons** — listening animation yields on keypress and stays paused for the full command; `[g]` aborts in-progress capture + drains the chunk queue and rebinds TTS in the background so swap is immediate mid-listen.
- **Mute capture during TTS** (`MUTE_CAPTURE_DURING_PLAYBACK`, default on) — app-level STT gate pauses while translation audio plays, plus `MUTE_CAPTURE_HANGOVER_MS` ring-out delay; breaks speaker→mic feedback loops. Coexists with `[n]` user mute; does not flip Windows tray mute.
- **Mic mute (`n`)** — Windows Core Audio mute (`pycaw` / `comtypes`) + app capture gate; startup warns if mic already muted or volume ~0%. Graceful app-only gate when COM is unavailable.
- **Stop playback (`x`)** — interrupt current TTS and drop remaining queued audio for the utterance.
- **Sound OFF mode (`s`)** — process STT + translation without live TTS; optional full TTS skip (`TTS_SKIP_WHEN_MUTED`) for speed.
- **Parallel sound-OFF workers** — paragraph-split chunks processed in parallel with ordered display (`SOUND_OFF_PARALLEL` / `SOUND_OFF_WORKERS`).
- **On-demand TTS for replay** — `[r]` / `[rN]` synthesize and cache WAV when a chunk has text but no audio (after muted sessions).
- **STT hallucination filter** (`livelingo/stt_filter.py`) — drop pure silence credits (`Legenda por…`, etc.) and strip trailing credit tails from long monologues.
- **Session chunk performance metrics** — persist `created_at` + timing JSON (STT / translate / TTS / first_audio / hear / total) in SQLite.
- **History list (`l`)** — show timing and registration timestamp below each source line (dim, separated by a blank line).
- **Export (`c`) word counter** — total multi-syllable content words on source text, excluding `e` / `a` / `ou` / `para` / `ao` / `à`.
- **Streaming UI** — live LLM translation tokens with single-line stream updates; print lock for parallel workers.
- **Piper / hybrid TTS, Silero VAD, synonyms (`o`)** — additional engines and helpers (config + modules; WordNet offline by default).
- **Interactive menu** — two-column fixed-width layout for terminal commands.
- **Startup language/voice checks** — warn when `STT_INITIAL_PROMPT` language conflicts with `SOURCE_LANG`, or when `TTS_VOICE` locale does not match `TARGET_LANG`.
- **Docs (pt-BR)** — README sections for anti-feedback, STT prompt / TTS locale alignment, updated command table, troubleshooting for portunhol and wrong TTS accent.

### Changed

- **Capture VAD** — adaptive silence, paragraph split (sound-OFF by default), drop near-silent tail chunks that trigger Whisper hallucinations.
- **Pipeline ordered release** — sync `_next_release` when toggling sound so muted chunks after a sound-ON session still publish Heard/Translated.
- **Markdown export (`c`)** — clean chunk layout: target, blank line, source, blank line between chunks; no timing/date in the file body.
- **DB schema** — `chunks.created_at`, `chunks.timing_json` with automatic migration on `init_db()`.
- **Config / `.env.example`** — document `MUTE_CAPTURE_*`, critical `STT_INITIAL_PROMPT` language rule, and `TTS_VOICE` locale must match `TARGET_LANG`.
- **edge-tts factory** — log active voice on startup; warn on locale mismatch with target language.

### Fixed

- **Session picker “back”** — resume/delete (and new-session title) accept `0` / `back` / `voltar` to return to the main session menu without restarting the app.
- **Mic mute freezes listen UI** — `[n]` pauses capture **and** stops the listening icons/status line so the transcript is readable; unmute resumes animation.
- **`[g]` mid-translation no longer drops the phrase** — swap is deferred until the in-flight chunk finishes (queue not drained); yellow “swap agendado” then “swap aplicado”. Press `g` again while pending to cancel.
- **Whisper farewell hallucinations** — filter whole-chunk (and narrow tails) for goodbye / good night / bye / see you / boa noite / tchau / buenas noches / au revoir, etc., which often appear after real speech + room noise.
- **Portunhol / wrong Heard language** — startup warns when `STT_INITIAL_PROMPT` language conflicts with `SOURCE_LANG` (e.g. Portuguese prompt + `SOURCE_LANG=en` made Whisper emit Portuguese despite `language=en`).
- **TTS accent mismatch** — startup warns when `TTS_VOICE` locale (e.g. `es-ES-*`) does not match `TARGET_LANG` (edge can read foreign text but keeps the voice accent).
- `[r]` / `[rN]` with sound OFF: auto re-enable sound and play (no longer asks to press `[s]`).
- Translation UI missing after Sound ON → OFF: ordered publisher cursor lagging behind sound-ON chunk numbers.
- Parallel workers interleaving filter messages with Heard/Translated lines (terminal print lock).
- Streaming overwrite corrupting long wrapped monologue lines.
- Menu columns misaligned after longer command labels.

## [0.1.0] — 2026-07-16

### Added

- Initial LiveLingo baseline (prior commits on this branch): SQLite sessions, interactive commands, Groq cloud STT, AI export summary.
