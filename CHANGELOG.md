# Changelog

All notable changes to LiveLingo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) where applicable.

## [Unreleased]

## [1.0.0] - 2026-07-21

First stable release of **LiveLingo2** (fork of [roirude/LiveLingo](https://github.com/roirude/LiveLingo)).
Tag: [`v1.0.0`](https://github.com/rcoproc/livelingo2/releases/tag/v1.0.0).

**Highlights:** TUI with Tradução split **LC | VOZ**, Windows LiveCaptions, phrase translation cache (TM), dual-channel export, voice bypass, sentence-split, OWASP A06 dependency floors + GitHub Actions CI (pytest + pip-audit), README badges.

### Changed

- **Tradução split vertical (LC | VOZ)** — real dual log panes under the Tradução tab (replaces space-padded dual-rail **inside one widget** for TUI):
  - **Left:** stable LiveCaptions pairs only (`panel=lc` / `#log-lc`)
  - **Right:** VOZ chunks + all command output (`panel=main` / `#log`)
  - **Independent scroll** per pane (one channel no longer pushes the other out of view)
  - **Mouse-draggable sash** (║) to resize LC|VOZ width; double-click restores 50/50
  - **Captions bottom edge** (`═ ↕ captions ═`) — drag up/down between **Live Captions strip (top)** and **middle log tabs**; double-click restores default height
  - **Expandir / Restaurar** on the **right VOZ header** only (top-right of the VOZ log window)
  - Click a pane to focus it for `/` search, `gg`/`GG`, and **Ctrl+Shift+C** (copies the focused pane, not both)
  - `[cls]` clears LC + VOZ + Sistema; `[cls1]` / `[cls2]` clear left LC / right VOZ only
  - History list `[l]` reprints **into the matching pane** (LC → left, VOZ → right)
  - Classic terminal keeps the old space-padded dual-rail layout (`ui._rail_geometry` no-op pad when a TUI sink is set)
- **Log sink panels** — `ui` now routes `main` | `lc` | `app` (`_normalize_panel`; aliases: `captions`/`livecaptions` → `lc`, `sistema`/`system` → `app`).
- **LiveCaptions final pairs** — `live_caption_block` / LC commit fallback emit to `panel="lc"` (no longer pollute VOZ).
- **Audio path lines** — empty / missing WAV no longer prints “not generated yet — use r / rN” or a second missing line; omit the line when path is empty (sound OFF / TTS skipped); known path still prints full `audio:` even if file not on disk yet.
- **Bypass badge (TUI)** — compact **F2** chip on its own row between Live Captions strip and log tabs (not beside the command box). Click / **F2** / `[b]` still toggle voice bypass. Full-log copy remains **Ctrl+Shift+C** only.
- **UI language codes** — `SOURCE_LANG` aliases `br` / `bra` map to `pt` for TUI chrome / audio i18n labels.

### Added

- **`cls1` / `cls2`** — clear one Tradução column: `cls1` = left LiveCaptions (LC), `cls2` = right VOZ; `cls` still clears LC + VOZ + Sistema. Documented in menu, command-help tab (EN/pt), and READMEs.
- **GitHub Actions CI** — [`.github/workflows/ci.yml`](.github/workflows/ci.yml): on push to `main`/`master` and on PRs, runs dependency security (`check_deps_security.py --project-only --fail-on vuln`) + `pytest` on Python **3.10** and **3.12**. README badges (EN/pt-BR): live CI status, Python, tests, security, MIT license.

### Security

- Bumped `python-dotenv` to **≥1.2.2** (CVE-2026-28684 symlink rewrite).
- Bumped `requests` to **≥2.33.0** (CVE-2026-25645 temp zip extract path).
- Pinned transitive `urllib3` floor **≥2.7.0** (decompress / DoS advisories).
- Added `scripts/check_deps_security.py` (OWASP A06 / pip-audit + outdated).
- Added unit tests under `tests/` (security floors, translate/LLM/STT mocks, import smoke).

### Added

#### Windows LiveCaptions (inbound)

- **LiveCaptions strip (TUI)** — scrapes Windows 11 LiveCaptions via UI Automation (`uiautomation`) into a fixed strip **above** the log tabs (original + translated). Independent of mic→Whisper→TTS. Module: `livelingo/livecaptions.py`.
- **LiveCaptions config** — `LIVE_CAPTIONS_ENABLED` (default on), `LIVE_CAPTIONS_HIDE_WINDOW`, `LIVE_CAPTIONS_KILL_ON_EXIT`, `LIVE_CAPTIONS_POLL_MS`, `LIVE_CAPTIONS_MAX_IDLE` / `MAX_SYNC`, `LIVE_CAPTIONS_PARTIAL_INTERVAL_S`, `LIVE_CAPTIONS_LOG` (final pairs → Tradução). Language: `LIVE_CAPTIONS_INVERT_LANGS` (default **true** so voice BR→EN ⇒ captions EN→BR) or explicit `LIVE_CAPTIONS_SOURCE_LANG` / `LIVE_CAPTIONS_TARGET_LANG`.
- **LiveCaptions commands (`lc …`)** — runtime control:
  - `lc` — toggle pause/resume of caption translation
  - `lc on` / `lc off` — resume / pause
  - `lc show` / `lc restore` — restore OS LiveCaptions window
  - `lc hide` — hide/minimize LiveCaptions window
  - `lc status` — service snapshot (status / paused / hidden / error)
- **LC → session history** — stable/final caption pairs stored as chunks with `timing.source=livecaptions` (partials stay strip-only when log is on).

#### Dual-rail log & list

- **Dual-rail Tradução layout** — **left rail** (magenta/cyan): LiveCaptions entrada `[LC N]`; **right rail** (yellow/blue): LiveLingo VOZ mic+TTS `[Chunk N]`. VOZ nudged ~15 cols left for readability. Shared geometry in `ui._rail_geometry` (list `l` matches live chunks).
- **History list (`l`) dual rail** — same LC ◄ / VOZ ► layout; counts `LC entrada` vs `VOZ mic`; meta (timing, audio, comments) stays inside each rail (no wrap to the left edge).
- **Lang labels** — short UI codes (`pt` → **BR**, else upper); LC uses caption pair, VOZ uses `SOURCE`/`TARGET`.

#### Phrase cache (TM) & import

- **Phrase translation cache (TM)** — optional exact **full-sentence** memory across **all sessions** (not session-scoped). SQLite `translation_pairs` + in-memory LRU. Lookup key: `(SOURCE_LANG, TARGET_LANG, normalized heard)`. **HIT** skips Google/LLM; **MISS** translates live then stores. Config: `PHRASE_CACHE`, `PHRASE_CACHE_SIZE`, `PHRASE_CACHE_WARMUP`, `PHRASE_CACHE_LOG` in `.env`. Timing field `translate_cache` for latency A/B.
- **LC dual-direction store** — when LiveCaptions commits a final pair (e.g. **EN→PT**), also stores the **inverted** pair (**PT→EN**, same texts swapped). Config: `PHRASE_CACHE_LC_ALSO_REVERSE` (default true). Warm-up loads both directions so voice in Portuguese can HIT phrases learned from English captions.
- **Phrase-cache commands (`pc …`)** — runtime control without restart:
  - `pc` / `pc status` — stats (hits/misses/hit rate/mem) + last event
  - `pc on` / `pc off` — enable/disable cache (A/B vs live LLM)
  - `pc force` — next chunk ignores HIT, live translate, overwrites pair (history kept for undo)
  - `pc last` — review last HIT/MISS/store (source + target)
  - `pc good` / `pc bad` — mark quality of last pair
  - `pc undo` — restore previous target from `translation_pairs_history`
  - `pc backup` — JSON snapshot under `.cache/phrase_cache_backups/` (+ `phrase_cache_latest.json`)
  - `pc restore` / `pc restore <path.json>` — restore pairs (auto backup before restore)
  - `pc import <file.csv> [reverse]` — import CSV into TM
- **CSV import into phrase cache** — `python -m livelingo.import_phrase_csv exported1.csv` (`--dry-run`, `--source-lang en`, `--also-reverse`). Columns `SourceText` / `TranslatedText` / `TargetLanguage`; **dedupe** via `UNIQUE(source_lang, target_lang, source_norm)`; skips empty and src≈tgt; pre-import JSON backup. Typical export direction **EN→PT**; use `reverse` for **PT→EN**.
- **CACHE / LIVE badges (Tradução tab)** — with cache enabled: `Translated [CACHE]:` (magenta) vs `Translated [LIVE]:` (cyan). With cache off: plain `Translated:`.
- **Cache inventory on TUI startup** — under the “Audio OFF…” tip on Tradução: pairs count, ~words src/tgt, per-direction breakdown (e.g. `EN→PT`), quality marks, mem size, ON/OFF.
- **Cache inventory on F1 help** — same summary appended at the end of F1 output on the **Sistema** tab.
- **Fast warm-up** — loads pairs into RAM only (no bulk SQLite upsert of every historical chunk on start).

#### Replay & audio commands

- **Replay Heard (`rs` / `rsN`)** — like `r`/`rN` but TTS from **Heard** (source) text with a default `SOURCE_LANG` voice; separate cache `chunk_N_heard.wav` (does not overwrite translated `r` WAV).
- **Replay auto sound ON** — `r` / `rN` / `rs` / `rsN` turn sound ON if it was OFF (log tip + UI badge).
- **Replay always prints WAV path** — after `r` / `rN` / `rs` / `rsN` (cache hit or on-demand TTS), shows full host path `audio: C:\…\chunk_N.wav` (or `chunk_N_heard.wav` for `rs`).
- **Blank lines after key status messages** — after bypass OFF (`[b]`), sound menu line (`[s]`), mic menu line (`[n]`), and after replay blocks (`r`/`rs`).

#### Export & AI summary

- **Export (`c`) dual-channel Markdown** — chronological body tags **`[LC N] LiveCaptions`** vs **`[Chunk N] LiveLingo VOZ`**; VOZ includes audio path when present; legend at top; totals split LC / VOZ.
- **Export annexes** — after the main body: **Anexo — só LiveCaptions** and **Anexo — só LiveLingo VOZ** (easy split for post-processing).
- **Chunked AI meeting summary** — `generate_meeting_summary` splits long transcripts to respect Groq TPM/size limits; config `SUMMARY_MAX_INPUT_TOKENS` (default 4000). Clearer errors on 413/429 “too large”.

#### TUI UX

- **Mic mute modal (`n`)** — when the mic is muted, a **centered red popup** (white text) overlays the TUI without closing it. Sole action: press **`n`** again (hint: `desmutar o microfone - Cmd n`). Header still shows MUTED. Classic CLI keeps log lines only.
- **Pipeline activity bar (command row)** — left of the command box: live stages **Mic → STT → Trad → TTS → Out** (Cable). Active step pulses (color + ●/◉); completed steps turn green; idle returns to soft Mic. **LC** chip appears when LiveCaptions is live/translating (magenta). Bypass mode shows `BYPASS→Out`. Driven by VAD + `chunk_progress` + playback loop.
- **Full WAV path on one line** — chunk `audio:` lines show the complete host path (no middle `…` truncation), **single line**, right-aligned in the log content width (list `l` + live VOZ).
- **TUI start compact (`TUI_MINIMAL`)** — open already compact (menu strip hidden; command line stays). Same as pressing F4 / `[u]` once at launch.
- **Vim-style log search** — `/text`, `/n`, `/p` (and aliases `find text`, `find:text`, `s?text`) on the active log tab; yellow/orange highlights; works on all four tabs.
- **Slash key fix (command bar)** — WT/WSL/ABNT2 `slash` without `character` now inserts `/`.
- **Word-delete on hold** — hold Backspace/Delete accelerates to whole-word erase; `Ctrl+Backspace` / `Ctrl+W` / `Ctrl+Delete` always word-level.
- **Bypass badge (left of command bar)** — white **(Translated audio)** / green **(Your voice)** (i18n); click = `[b]`. Rounded frame aligned with command chrome.
- **TTS badge style (right of command bar)** — black fill, blue `$accent` border (same as command box), white text.
- **Heard / Translated column align** — phrase text after `:` starts on the same column for both lines (pads `Heard:` to `Translated [CACHE]:` width).
- **Auto-select Tradução on mic speech** — VAD speech start focuses the Tradução tab if another tab was active.
- **Tradução auto-scroll** — new log lines re-enable follow-to-bottom (after `/` search or `gg`).
- **Novidades / What's New tab (TUI)** — third log tab loads the project root `CHANGELOG.md` (Markdown). Tab title follows `SOURCE_LANG`.
- **Lista de comandos / Command list tab (TUI)** — fourth log tab: all menu commands **grouped** with full descriptions in `SOURCE_LANG`, A–Z, Markdown (`livelingo/command_help.py`). Includes `lc …`, `pc …`, `rs`/`rsN`, `/` search, dual-rail `l`, export `c`, etc. Refreshes on `[g]`.
- **CLI `--list-sessions`** — list every saved session (same line format as menu option `[2] RESUME`) and exit. Example: `livelingo --list-sessions`.
- **CLI `--help` / `-h`** — English usage for all CLI flags with short explanations and sample outputs; then exit.
- **CLI `-v`** — alias for `--verbose` (detailed debug logs).
- **TUI log tabs** — **Tradução**, **Sistema**, **Novidades** (`CHANGELOG.md`), **Lista de comandos**. `F3` cycles all four.
- **Typed new translation (`enew`)** — `enew <text>` queues a chunk without mic/STT; TTS follows current sound mode (`[s]` ON → audio).
- **Voice bypass (`b` / `bypass` / `hot`)** — toggle: raw mic → `OUTPUT_DEVICE` (VB-Cable) without STT/translate; pauses listen-to-translate. Header shows `BYPASS [b]`. For speaking English (or any language) live into Teams.
- **Sentence-early emit (`SENTENCE_SPLIT`)** — short pause after min speech emits a phrase as its own chunk (STT+translate+UI) while still listening; does not wait for the full monologue. Config: `SENTENCE_SILENCE`, `SENTENCE_MIN_SPEECH`, `SENTENCE_SPLIT_OVERLAP`, `SENTENCE_SPLIT_SOUND_OFF_ONLY` (default: sound-OFF only).
- **Compact UI (`u` / `ui` / `compact` / `F4`)** — hides the command menu strip; command line stays. Safe host-window height resize (CSI + `MoveWindow` only; no console buffer thrash).
- **Edit prefill (TUI)** — `[e]` / `[eN]` pre-fills the command field with the current sentence text.
- **Utility list commands** — `ld` runs `python list_devices.py` into the log; `lav` runs `edge-tts --list-voices`; `lv` same with filter `en-US|en-GB|es-ES|es-MX|fr-FR` (Python filter, no shell pipe).
- **Change TTS voice (`ctts`)** — one-liner `ctts en-US-AndrewMultilingualNeural` or prompt; validates ShortName via edge-tts catalog; applies to upcoming synthesis only.
- **TUI polish (Textual)** — fixed header with robot + `g(swap) SRC→TGT t(target)` + audio status; full-width pack menu; command box with outer border (`#cmd-box`); labels/placeholder follow `SOURCE_LANG` (i18n). Classic CLI via `UI_MODE=classic`.
- **Log selection & copy** — click-drag character selection in the scrollable log; `Ctrl+C` copies selection; `Ctrl+Shift+C` copies the full log (Windows/WSL clipboard).
- **Bypass via F2** — white/green badge left of the command box toggles voice bypass; **F2** (Footer shortcut) same as click / `[b]`. Full-log copy is **Ctrl+Shift+C** only.
- **Command history** — `↑` / `↓` in the command field walks previous commands (persisted under `.cache/cmd_history.txt`).
- **F1 help** — reprints startup banner, devices, engines, and tips into the **Sistema** tab (opens that tab); Tradução stays for phrase logs only.
- **Screenshot (command palette)** — saves SVG under `.cache/screenshots/`, rasterizes to PNG (Chrome/Edge headless, ImageMagick, or optional cairosvg), and copies the **image** to the Windows clipboard (`SetDataObject` + STA PowerShell from WSL/host).
- **Chunk comments** — `co` / `coN` / `coN text` attach free-text notes to a chunk (SQLite `chunk_comments`, shown on `l` with `#id`); `codN` deletes by primary key.
- **Clear log (`cls`)** — clears both TUI log panels (classic: clears the terminal).
- **List source/target only** — `lo` lists heard (source) lines; `lt` lists translated (target) lines.
- **Log navigation** — `gg` / `gt` (go top) jumps to the start of the active log tab and turns auto-scroll off; `GG` / `gf` (go bottom) jumps to the end and re-enables auto-scroll. `GG` is case-sensitive (vim-style).
- **Resume by session id** — `python main.py <session_id>` / `livelingo <session_id>` skips the session picker; session id is shown on exit.
- **TUI mode (`UI_MODE=tui`, default)** — Textual full-screen UI: dual scrollable logs, command input, fixed listen bar. Requires `pip install textual`.
- **Live TTS default OFF** — translation audio starts muted (text-only); enable with `[s]`. Robot status highlights áudio OFF/ON. Replay `[r]` still re-enables sound when needed.
- **Audio file references** — each chunk log and command `l` show `audio:` (**full** absolute host path, one line); `a`/`aN` copy path; `p`/`pN` open Explorer on the WAV. WSL `/mnt/c` → `C:\` conversion. WAV is written **synchronously** before the path is shown (no false “missing” after playback).
- **Set TARGET language (`t`)** — prompt or one-liner `t EN` / `t en` for EN/PT/ES/FR/DE/IT/ZH/JA; **input forced to UPPERCASE** in this command only; updates `TARGET_LANG` + translator + TTS voice; SOURCE/STT unchanged.
- **Dev auto-reload** (`dev_reload.py`) — watch project `*.py` and restart `main.py` on save. Optional `--verbose` / `--debounce`.
- **Language swap (`g`)** — invert `SOURCE_LANG` ↔ `TARGET_LANG` at runtime (STT + translator + TTS). Yellow menu line shows the pair. Optional `TTS_VOICE_ALT`. Does not rewrite historical chunks.
- **Command priority over listen icons** — listening animation yields on keypress and stays paused for the full command.
- **Mute capture during TTS** (`MUTE_CAPTURE_DURING_PLAYBACK`, default on) — STT gate while TTS plays + `MUTE_CAPTURE_HANGOVER_MS`; breaks speaker→mic loops. Coexists with `[n]`.
- **Mic mute (`n`)** — Windows Core Audio mute (`pycaw` / `comtypes`) + app capture gate; graceful app-only gate when COM is unavailable. TUI: centered red unmute modal (see TUI UX).
- **Stop playback (`x`)** — interrupt current TTS and drop remaining queued audio.
- **Sound OFF mode (`s`)** — STT + translation without live TTS; optional full TTS skip (`TTS_SKIP_WHEN_MUTED`).
- **Parallel sound-OFF workers** — sentence/paragraph-split chunks in parallel with ordered display (`SOUND_OFF_PARALLEL` / `SOUND_OFF_WORKERS`).
- **On-demand TTS for replay** — `[r]` / `[rN]` synthesize and cache WAV when a chunk has text but no audio.
- **STT hallucination filter** (`livelingo/stt_filter.py`) — drop pure silence credits and strip trailing credit tails.
- **Session chunk performance metrics** — `created_at` + timing JSON in SQLite.
- **History list (`l`)** — timing/timestamp, optional comments with `#id`; header rules fit log panel width.
- **Export (`c`) word counter** — multi-syllable content words on source text (excludes common stopwords).
- **Streaming UI** — live LLM translation tokens with single-line stream updates; print lock for parallel workers.
- **Piper / hybrid TTS, Silero VAD, synonyms (`o`)** — engines/helpers; synonym output uses Rich/ANSI colors (markdown stripped for display).
- **Interactive menu** — full-width pack layout (TUI) / two-column classic layout.
- **Startup language/voice checks** — warn on `STT_INITIAL_PROMPT` vs `SOURCE_LANG`, or `TTS_VOICE` locale vs `TARGET_LANG`.
- **Docs** — README (EN/pt-BR) and changelog for TUI commands, dual logs, bypass, sentence split, Teams/Meet routing.

### Changed

- **Markdown export (`c`)** — channel-aware LC vs VOZ body + annexes; VOZ audio path; no timing/date in the file body; word counter still on source text.
- **History list (`l`)** — dual-rail layout (was single-column hang-indent); LC/VOZ counts in header.
- **Live VOZ chunks** — render on the **right rail** (aligned with list `l`); optional CACHE/LIVE badge on target line.
- **AI export summary** — multi-request chunking when transcript exceeds `SUMMARY_MAX_INPUT_TOKENS` (avoids Groq free-tier size/TPM failures).
- **Pipeline stage logs** — `chunk_progress` / listen VAD / timings / live audio paths go to the **Sistema** tab; Tradução keeps phrase lines + command output. Stage detail text is no longer hard-truncated at 45 chars in TUI.
- **Chunk spacing** — one blank line between consecutive Heard/Translated blocks (not two).
- **`ctts`** — modal/click popup removed (could freeze the TUI); command-line only (`ctts <ShortName>` or prompt).
- **TUI command chrome** — command field in `#cmd-box` with round border; `#bottom` not docked with Footer (avoids overlap clipping left/right borders).
- **Capture VAD** — longer preroll, onset gap tolerance, more sensitive onset threshold; soft-mute pads preroll with silence (no TTS echo in lead-in).
- **TUI screenshot path** — palette “Screenshot” no longer only prints a file path: also produces PNG and puts image data on the OS clipboard.
- **VERBOSE-gated logs** — STT/hallucination/processing chatter respects `--verbose` / `VERBOSE`.
- **Capture VAD** — adaptive silence, sentence/paragraph early emit, drop near-silent tail chunks.
- **Pipeline ordered release** — sync `_next_release` when toggling sound so muted chunks still publish Heard/Translated.
- **DB schema** — `chunks.created_at`, `chunks.timing_json`, `chunk_comments`, `translation_pairs` (+ history) with migration on `init_db()`.
- **Config / `.env.example`** — document `MUTE_CAPTURE_*`, `SENTENCE_*`, `PHRASE_CACHE_*` (incl. `PHRASE_CACHE_LC_ALSO_REVERSE`), `LIVE_CAPTIONS_*`, `TUI_MINIMAL`, STT prompt language rule, TTS locale must match `TARGET_LANG`.
- **edge-tts factory** — log active voice on startup; warn on locale mismatch.
- **Deps** — optional `uiautomation` on Windows for LiveCaptions scrape.
- **F2 shortcut** — **F2** is voice bypass (not full-log copy); full log remains **Ctrl+Shift+C**.

### Removed

- **Vendored `LiveCaptions-Translator/` + `.zip`** — reference C# project no longer needed; LC uses built-in `livelingo/livecaptions.py` + Windows Live Captions + `uiautomation` only.

### Fixed

- **Long phrases cut mid-thought** — VAD no longer treats short breaths as end-of-turn / end-of-sentence so eagerly:
  - `SOUND_OFF_SILENCE_DURATION` is a **base** for end-of-turn silence (still adaptive), not a hard cap that forced ~0.7s cuts.
  - Early sentence-split silence **scales up** with monologue length (`SENTENCE_SILENCE_SCALE_MAX`).
  - Safer defaults: `SENTENCE_SILENCE≈0.95`, `SENTENCE_MIN_SPEECH≈2.5`, `SOUND_OFF_SILENCE_DURATION≈1.8`.
- **Mic unmute in TUI (`n`)** — no longer calls missing `LiveLingoApp.start()` (classic indicator only).
- **Log wrap on inactive tab** — inactive TabPane no longer bakes lines at ~20 columns; safe render width for both logs.
- **List (`l`) header rules** — `===` lines fit the log panel (`panel_width` / `rule_line`).
- **Command box crushed / missing side borders** — Footer no longer overlaps `#bottom`; outer `#cmd-box` draws the full border.
- **Compact UI (`u`/`F4`)** — no console buffer resize thrash (ghost UI / dead tabs); safe window height via CSI + `MoveWindow` only.
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

[Unreleased]: https://github.com/rcoproc/livelingo2/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/rcoproc/livelingo2/releases/tag/v1.0.0
[0.1.0]: https://github.com/rcoproc/livelingo2/releases/tag/v0.1.0
