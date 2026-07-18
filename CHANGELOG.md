# Changelog

All notable changes to LiveLingo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) where applicable.

## [Unreleased]

### Added

- **TUI polish (Textual)** — solid borders; fixed header with robot + `g(swap) SRC→TGT t(target)` + audio status; footer command menu with dynamic columns; labels/placeholder follow `SOURCE_LANG` (i18n). Classic CLI via `UI_MODE=classic`.
- **Log selection & copy** — click-drag character selection in the scrollable log; `Ctrl+C` copies selection; `Ctrl+Shift+C` / `F2` copies the full log (Windows/WSL clipboard).
- **Command history** — `↑` / `↓` in the command field walks previous commands (persisted under `.cache/cmd_history.txt`).
- **F1 help** — reprints startup banner, devices, engines, and tips into the log (aligned indent in TUI).
- **Screenshot (command palette)** — saves SVG under `.cache/screenshots/`, rasterizes to PNG (Chrome/Edge headless, ImageMagick, or optional cairosvg), and copies the **image** to the Windows clipboard (`SetDataObject` + STA PowerShell from WSL/host).
- **Chunk comments** — `co` / `coN` / `coN text` attach free-text notes to a chunk (SQLite `chunk_comments`, shown on `l` with `#id`); `codN` deletes by primary key.
- **Clear log (`cls`)** — clears the TUI log panel (classic: clears the terminal).
- **List source/target only** — `lo` lists heard (source) lines; `lt` lists translated (target) lines.
- **Log navigation** — `gt` (go top) jumps to the start of the log and turns auto-scroll off; `gf` (go footer) jumps to the end and re-enables auto-scroll.
- **Resume by session id** — `python main.py <session_id>` / `livelingo <session_id>` skips the session picker; session id is shown on exit.
- **TUI mode (`UI_MODE=tui`, default)** — Textual full-screen UI: scrollable log, command input, fixed listen bar. Requires `pip install textual`.
- **Live TTS default OFF** — translation audio starts muted (text-only); enable with `[s]`. Robot status highlights áudio OFF/ON. Replay `[r]` still re-enables sound when needed.
- **Audio file references** — each chunk log and command `l` show `audio:` (absolute host path); `a`/`aN` copy path; `p`/`pN` open Explorer on the WAV. WSL `/mnt/c` → `C:\` conversion. WAV is written **synchronously** before the path is shown (no false “missing” after playback).
- **Set TARGET language (`t`)** — prompt or one-liner `t EN` / `t en` for EN/PT/ES/FR/DE/IT/ZH/JA; **input forced to UPPERCASE** in this command only; updates `TARGET_LANG` + translator + TTS voice; SOURCE/STT unchanged.
- **Dev auto-reload** (`dev_reload.py`) — watch project `*.py` and restart `main.py` on save. Optional `--verbose` / `--debounce`.
- **Language swap (`g`)** — invert `SOURCE_LANG` ↔ `TARGET_LANG` at runtime (STT + translator + TTS). Yellow menu line shows the pair. Optional `TTS_VOICE_ALT`. Does not rewrite historical chunks.
- **Command priority over listen icons** — listening animation yields on keypress and stays paused for the full command.
- **Mute capture during TTS** (`MUTE_CAPTURE_DURING_PLAYBACK`, default on) — STT gate while TTS plays + `MUTE_CAPTURE_HANGOVER_MS`; breaks speaker→mic loops. Coexists with `[n]`.
- **Mic mute (`n`)** — Windows Core Audio mute (`pycaw` / `comtypes`) + app capture gate; graceful app-only gate when COM is unavailable.
- **Stop playback (`x`)** — interrupt current TTS and drop remaining queued audio.
- **Sound OFF mode (`s`)** — STT + translation without live TTS; optional full TTS skip (`TTS_SKIP_WHEN_MUTED`).
- **Parallel sound-OFF workers** — paragraph-split chunks in parallel with ordered display (`SOUND_OFF_PARALLEL` / `SOUND_OFF_WORKERS`).
- **On-demand TTS for replay** — `[r]` / `[rN]` synthesize and cache WAV when a chunk has text but no audio.
- **STT hallucination filter** (`livelingo/stt_filter.py`) — drop pure silence credits and strip trailing credit tails.
- **Session chunk performance metrics** — `created_at` + timing JSON in SQLite.
- **History list (`l`)** — timing/timestamp, optional comments with `#id`, blank line after language texts; monochromatic layout polish.
- **Export (`c`) word counter** — multi-syllable content words on source text (excludes common stopwords).
- **Streaming UI** — live LLM translation tokens with single-line stream updates; print lock for parallel workers.
- **Piper / hybrid TTS, Silero VAD, synonyms (`o`)** — engines/helpers; synonym output uses Rich/ANSI colors (markdown stripped for display).
- **Interactive menu** — dynamic multi-column footer (TUI) / two-column classic layout.
- **Startup language/voice checks** — warn on `STT_INITIAL_PROMPT` vs `SOURCE_LANG`, or `TTS_VOICE` locale vs `TARGET_LANG`.
- **Docs** — README (EN/pt-BR) and changelog for TUI commands, comments, navigation, screenshot clipboard.

### Changed

- **TUI screenshot path** — palette “Screenshot” no longer only prints a file path: also produces PNG and puts image data on the OS clipboard.
- **VERBOSE-gated logs** — STT/hallucination/processing chatter respects `--verbose` / `VERBOSE`.
- **Capture VAD** — adaptive silence, paragraph split (sound-OFF by default), drop near-silent tail chunks.
- **Pipeline ordered release** — sync `_next_release` when toggling sound so muted chunks still publish Heard/Translated.
- **Markdown export (`c`)** — clean chunk layout; no timing/date in the file body.
- **DB schema** — `chunks.created_at`, `chunks.timing_json`, `chunk_comments` with migration on `init_db()`.
- **Config / `.env.example`** — document `MUTE_CAPTURE_*`, STT prompt language rule, TTS locale must match `TARGET_LANG`.
- **edge-tts factory** — log active voice on startup; warn on locale mismatch.

### Fixed

- **False “audio missing”** — path was shown before the WAV hit disk; persist is now synchronous in the finalize path.
- **Screenshot clipboard empty on Windows/WSL** — use PowerShell full path + `-STA` + `Clipboard.SetDataObject(..., $true)`; fix Chrome headless `--default-background-color` hex so PNG rasterization succeeds.
- **F1 / log indent** — TUI log sink respects `indent` so help/banner lines align.
- **Session resume argv** — do not treat `main.py` as a session id when wrappers pass the script path.
- **Session picker “back”** — resume/delete accept `0` / `back` / `voltar`.
- **Mic mute freezes listen UI** — `[n]` pauses capture and listen icons; unmute resumes.
- **`[g]` mid-translation** — swap deferred until in-flight chunk finishes; press `g` again to cancel pending.
- **Whisper farewell hallucinations** — filter goodbye / boa noite / tchau / etc. after speech + room noise.
- **Portunhol / wrong Heard language** — startup warns when STT prompt language conflicts with `SOURCE_LANG`.
- **TTS accent mismatch** — warn when `TTS_VOICE` locale does not match `TARGET_LANG`.
- `[r]` / `[rN]` with sound OFF: auto re-enable sound and play.
- Translation UI missing after Sound ON → OFF (ordered publisher cursor).
- Parallel workers interleaving filter messages with Heard/Translated (print lock).
- Streaming overwrite corrupting long wrapped monologue lines.
- Menu columns misaligned after longer command labels.

## [0.1.0] — 2026-07-16

### Added

- Initial LiveLingo baseline (prior commits on this branch): SQLite sessions, interactive commands, Groq cloud STT, AI export summary.
