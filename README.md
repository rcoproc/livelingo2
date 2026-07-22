# 🎙️ LiveLingo2 — Real-Time Voice Translation for Windows

[![CI](https://github.com/rcoproc/livelingo2/actions/workflows/ci.yml/badge.svg)](https://github.com/rcoproc/livelingo2/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-pytest-brightgreen.svg)](tests/)
[![Security](https://img.shields.io/badge/security-pip--audit%20%7C%20OWASP%20A06-blue.svg)](scripts/check_deps_security.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Português:** documentação completa em [`README-ptbr.md`](README-ptbr.md).  
> **More screenshots:** [`screenshots.md`](screenshots.md) (Live Captions strip, compact UI, Changelog tab).

**LiveLingo2** is a **fork** (downstream copy) of the original project
[**roirude/LiveLingo**](https://github.com/roirude/LiveLingo) by [@roirude](https://github.com/roirude).
This repository builds on that foundation with extra features (TUI, LiveCaptions,
phrase cache, sessions, security tooling, etc.). Credit for the initial idea and
architecture goes to the upstream project.

<p align="center">
  <img src="docs/screenshots/live_lingo4.png" alt="LiveLingo2 TUI — Live Captions strip + Tradução log with LC caption/translated chunks" width="900" />
</p>

<p align="center"><em>TUI in action: Live Captions (EN→BR) above <strong>Tradução</strong> (split LC | VOZ panes), with caption/translated pairs and pipeline status in the footer. See also <a href="screenshots.md">screenshots.md</a>.</em></p>

**LiveLingo2** turns your speech into another language **live**, on a virtual
microphone — so Microsoft Teams (or Zoom, Discord, Google Meet, OBS…) hears the
translation as if it were your mic. Speak **French**, others hear **English**
(both languages configurable).

**Current release: [v1.2.1](CHANGELOG.md#121---2026-07-22)** — webcam/TUI polish on top of
v1.1.0: reliable **OBS Virtual Camera**, **full-face F10 freeze**, F2/F5 on the tab bar,
bypass tips on **Sistema**, local Whisper first-boot notes. Full notes:
[`CHANGELOG.md`](CHANGELOG.md).

```text
🎤 mic (French)
   └─► Whisper STT  (speech → French text: Groq cloud large-v3, or local)
        └─► translation (French → English: Google free, or Groq LLM for quality)
             └─► edge-tts  (English text → speech, free)
                  └─► VB-Cable  ──►  "CABLE Output" used as mic in Teams

Optional video (v1.1+):
📷 webcam → MediaPipe mouth + lip engine → pyvirtualcam
              └─► OBS Virtual Camera  ──►  Teams camera
```

Translation and text-to-speech use free public services (internet required).
Speech-to-text runs **either** on Groq's free cloud Whisper (most accurate,
recommended) **or** fully locally with faster-whisper (offline).

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Windows 10/11** | The tool uses Windows audio APIs (MME via PortAudio). |
| **Python 3.10+** | 3.10 – 3.12 recommended. Check with `python --version`. |
| **VB-CABLE** | Free virtual audio cable. Download: **<https://vb-audio.com/Cable/>** |
| **OBS Studio** *(optional, for Teams video / lip-sync)* | Provides the **OBS Virtual Camera** driver so LiveLingo can send a processed face to Teams. Download: **<https://obsproject.com/>** (full installer, not only the Microsoft Store build). |
| **Internet** | Needed for translation + TTS (and the first Whisper model download). |

### Install VB-CABLE

1. Download the VB-CABLE zip from <https://vb-audio.com/Cable/>.
2. Extract it, then **right-click `VBCABLE_Setup_x64.exe` → Run as administrator**.
3. Click **Install Driver**.
4. **Reboot Windows** (important — the device won't appear reliably until you do).

After reboot you will have two new devices:

- **CABLE Input (VB-Audio Virtual Cable)** — a *playback* device. **This tool sends the English speech here.**
- **CABLE Output (VB-Audio Virtual Cable)** — a *recording* device. **Teams selects this as its microphone.**

### Install OBS Studio (virtual camera for Teams video)

Required only if you want **camera + lip-sync** in the call (audio-only translation still works with VB-Cable alone).

1. Install **OBS Studio** from <https://obsproject.com/>.
2. Open OBS **once as Administrator** (right-click → Run as administrator).
3. Bottom-right **Start Virtual Camera** (or Tools → Start Virtual Camera).  
   The first start **registers** the Windows **OBS Virtual Camera** device.
4. **Stop Virtual Camera**, then fully quit OBS (so LiveLingo can open the same device).
5. Windows Settings → Privacy → Camera → allow **desktop apps**.

> **Important:** the virtual camera is **video only**. Translated speech still goes through
> **VB-Cable**. In Teams you set **two** devices (see [Teams with audio + video](#use-it-as-your-microphone-and-camera-in-microsoft-teams--google-meet)).

---

## 2. Installation

From this project folder:

```powershell
# (optional but recommended) create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# optional — webcam lip-sync → OBS Virtual Camera (v1.1+)
pip install -r requirements-webcam.txt
```

> When using the **local** STT engine, the first run downloads the Whisper model
> (`small` ≈ 0.5 GB, `medium` ≈ 1.5 GB) into `~/.cache/huggingface` — automatic,
> just wait once. With the **Groq** engine (recommended), no download is needed.

### Dependency security (production)

LiveLingo tracks **OWASP Top 10 A06** (*Vulnerable and Outdated Components*) for
Python deps declared in [`requirements.txt`](requirements.txt).

**Security floors** (do not downgrade):

| Package | Minimum | Why |
|---------|---------|-----|
| `python-dotenv` | **≥ 1.2.2** | CVE-2026-28684 (symlink follow in `set_key` / `unset_key`) |
| `requests` | **≥ 2.33.0** | CVE-2026-25645 (predictable temp path in zip extract helper) |
| `urllib3` | **≥ 2.7.0** | Transitive floor for known decompress / DoS advisories |

`deep-translator==1.11.4` is the latest legitimate release. Advisory
**PYSEC-2022-252** is a *historical* PyPI account-takeover report with **no
fixed version**; the malicious releases were removed. The audit script
allowlists it (see `KNOWN_FALSE_POSITIVES` in the checker).

**Audit + tests before deploy**

Install dev tooling once (`pytest`, `pip-audit`, `ruff`):

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

#### Continuous Integration (GitHub Actions)

Every push to `main` / `master` and every pull request runs
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) on **Python 3.10 and 3.12**:

1. **Security** — `python scripts/check_deps_security.py --project-only --fail-on vuln`
2. **Tests** — `python -m pytest tests/ -q`

The green **CI** badge at the top of this README reflects the latest run on the default branch:
[Actions → CI](https://github.com/rcoproc/livelingo2/actions/workflows/ci.yml).

Locally you can mirror the same gates with `bash scripts/run_checks.sh` (includes optional format).

#### One-shot checks (WSL / Linux) — recommended

[`scripts/run_checks.sh`](scripts/run_checks.sh) runs, in order:

1. **Format** — `ruff format` + safe import fixes (falls back to `black`/`isort` if present)
2. **Security** — `python3 scripts/check_deps_security.py --project-only`
3. **Tests** — `python3 -m pytest tests/`

```bash
# from the project root (WSL)
cd /mnt/c/Users/rcopr/LiveLingo/LiveLingo   # adjust path if needed
bash scripts/run_checks.sh
# or:  ./scripts/run_checks.sh
```

Useful flags for `run_checks.sh`:

| Flag | Meaning |
|------|---------|
| `--skip-format` | Skip ruff/black |
| `--fail-on vuln` | Default: fail only on **CVEs** (outdated packages = warning) |
| `--fail-on any` | Fail if there is any CVE **or** outdated package |
| `--fail-on outdated` | Fail on outdated (and vulns) |
| `--pytest -v` | Extra args forwarded to pytest |
| `--no-project-only` | Audit the whole environment, not only project deps |

#### Manual steps (Windows PowerShell / any shell)

```powershell
python -m pytest tests/ -v
python scripts/check_deps_security.py --project-only
```

Useful flags for `check_deps_security.py`:

| Flag | Meaning |
|------|---------|
| `--project-only` | Report only packages from `requirements.txt` (+ security-floor packages) |
| `--fail-on any` | Non-zero exit if **any** vuln **or** outdated package |
| `--json report.json` | Write a full machine-readable report |
| `--ignore-vuln ID` | Extra advisory ignore (CVE / PYSEC / GHSA) |
| `--no-default-ignores` | Disable the built-in historical allowlist |

**How to read the security checklist**

| Mark | Meaning |
|------|---------|
| `✓` | OK |
| `!` | **Warning only** (e.g. a newer version exists on PyPI — not a CVE) |
| `✗` | Action required (scan failed or actionable vulnerability) |

- **“Components monitored (PyPI scan)”** = the outdated check **ran** (✓), not “everything is latest”.
- **“Deps on latest PyPI version”** may show `!` for packages like `sounddevice` / `soundfile` when a newer release exists. That is **freshness**, not a security hole.
- Default `--fail-on vuln` → **EXIT 0** if there are no actionable CVEs, even when some packages are outdated.
- Status line: `WARN (freshness only)` = safe to deploy for security; upgrade when you can and re-test audio.
- To make CI fail on outdated packages: `--fail-on any` (or `--fail-on outdated`).

Exit codes (`check_deps_security.py` / checks script):

| Code | Meaning |
|------|---------|
| `0` | OK for the selected `--fail-on` criterion |
| `1` | Actionable finding (vuln and/or outdated, depending on flags) |
| `2` | Tool/scan error (e.g. pip-audit could not run) |

Audio modules that need PortAudio may be **skipped** in headless/WSL CI; that
does not block the security floor or mocked pipeline tests.

---

## 3. Find your device indices

```powershell
python list_devices.py
```

This prints every audio device with its **index**, marking inputs (green),
outputs (magenta), and the VB-Cable device. Example:

```
idx   in out  host API       name
  1    2   0  MME            Microphone (Realtek Audio)      <- default-in
  8    0   2  MME            CABLE Input (VB-Audio Virtual Cable)   <- VB-CABLE
 12    0   2  MME            Speakers (Realtek Audio)        <- default-out
```

Note the index of **your microphone** and of **CABLE Input**.

---

## 4. Configure

You can leave the defaults (mic = system default, output = `CABLE Input`) and it
will usually just work. To customise, either edit [`config.py`](config.py)
directly, or copy the example env file:

```powershell
Copy-Item .env.example .env
notepad .env
```

Common settings:

| Setting | Default | Meaning |
|---------|---------|---------|
| `SOURCE_LANG` | `fr` | Language you speak |
| `TARGET_LANG` | `en` | Language others hear |
| `STT_ENGINE` | `auto` | `auto`/`groq`/`local` — Groq cloud Whisper (best) vs local (see below) |
| `GROQ_STT_MODEL` | `whisper-large-v3` | Groq STT model (`whisper-large-v3-turbo` = faster) |
| `STT_INITIAL_PROMPT` | *(empty)* | Hint of names/vocabulary/accents to bias recognition |
| `WHISPER_MODEL` | `small` | Local model: `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` |
| `INPUT_DEVICE` | *(default mic)* | Mic index or name substring |
| `OUTPUT_DEVICE` | `CABLE Input` | VB-Cable playback device (index or name) |
| `TTS_VOICE` | `en-US-AriaNeural` | Edge voice for **target** — locale must match `TARGET_LANG` (see below) |
| `TTS_VOICE_ALT` | *(auto on swap)* | Voice for the other language in the pair; used by terminal `[g]` swap |
| `CHUNK_DURATION` | `4.0` | Target/fixed chunk length (seconds) |
| `VAD_ENABLED` | `true` | Split on pauses (true) vs fixed chunks (false) |
| `SILENCE_THRESHOLD` | `0.015` | Mic loudness threshold for speech detection |
| `MONITOR_PLAYBACK` | `false` | Also play the translation on your speakers (testing) |
| `MONITOR_DEVICE` | *(default out)* | Device for the monitor copy (index/name) |
| `MUTE_CAPTURE_DURING_PLAYBACK` | `true` | Pause STT capture while TTS plays (breaks speaker→mic loop) |
| `MUTE_CAPTURE_HANGOVER_MS` | `350` | Wait (ms) after TTS before reopening the mic |
| `TRANSLATION_ENGINE` | `auto` | `auto`/`llm`/`google` (see below) |
| `GROQ_API_KEY` | *(empty)* | Free Groq key → much better translation quality |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model (`llama-3.1-8b-instant` = faster) |
| `STT_FALLBACK` | `local` | On Groq STT failure mid-session: `local` Whisper or `none` |
| `TRANSLATION_FALLBACK` | `google` | On LLM failure mid-session: `google` or `none` |
| `CIRCUIT_FAIL_THRESHOLD` | `3` | Open circuit after N primary failures |
| `CIRCUIT_COOLDOWN_S` | `60` | Seconds before probing primary again |
| `STT_WARMUP_LOCAL` | `true` | Pre-load local Whisper in background when Groq is primary |
| `FAILOVER_LOG` | `true` | Rate-limited `[ha]` messages on the System panel |

### Elegant Edge voices (quick reference)

Set in `.env`: `TTS_VOICE=exact-name`. The voice **locale prefix must match `TARGET_LANG`**
(`en` → `en-*`, `es` → `es-*`, `fr` → `fr-*`). Wrong locale → correct text, wrong accent.

**List every available voice** (Microsoft Edge neural TTS):

```powershell
edge-tts --list-voices
```

Filter EN / ES / FR (PowerShell):

```powershell
edge-tts --list-voices | Select-String "en-US|en-GB|es-ES|es-MX|fr-FR"
```

Linux / WSL / macOS:

```bash
edge-tts --list-voices | grep -E "en-US|en-GB|es-ES|es-MX|fr-FR"
```

Curated **polite / professional** picks for meetings — 2 male + 2 female per language:

| Language | Gender | `TTS_VOICE` | Profile |
|----------|--------|-------------|---------|
| **English** (`en`) | Female | `en-US-AriaNeural` | Clear, professional (US) — classic default |
| **English** (`en`) | Female | `en-GB-SoniaNeural` | Polished British, formal tone |
| **English** (`en`) | Male | `en-GB-RyanNeural` | Sober British, executive meeting |
| **English** (`en`) | Male | `en-US-ChristopherNeural` | Calm, articulate American |
| **Spanish** (`es`) | Female | `es-ES-ElviraNeural` | Spain, clean formal diction |
| **Spanish** (`es`) | Female | `es-MX-DaliaNeural` | Mexico, natural and polite |
| **Spanish** (`es`) | Male | `es-ES-AlvaroNeural` | Spain, deep professional |
| **Spanish** (`es`) | Male | `es-MX-JorgeNeural` | Mexico, confident and sober |
| **French** (`fr`) | Female | `fr-FR-DeniseNeural` | France, elegant and neutral |
| **French** (`fr`) | Female | `fr-FR-EloiseNeural` | France, clear and cordial |
| **French** (`fr`) | Male | `fr-FR-HenriNeural` | France, formal and measured |
| **French** (`fr`) | Male | `fr-FR-AlainNeural` | France, mature and polite |

Example `.env` (EN → FR, elegant male voice):

```env
SOURCE_LANG=en
TARGET_LANG=fr
TTS_VOICE=fr-FR-HenriNeural
```

> `*MultilingualNeural` voices can read several languages but **keep the accent of their
> locale**. Prefer `fr-FR-*` for native French, `es-ES-*` / `es-MX-*` for Spanish, etc.

**Runtime language swap:** press **`g`** in the terminal menu to invert `SOURCE_LANG` ↔
`TARGET_LANG` (STT + translation + TTS voice). The menu shows a bright yellow line such as
`[g]  Swap idiomas   EN → PT`. Set `TTS_VOICE` for the current target and `TTS_VOICE_ALT`
for the other side (or leave alt empty for an automatic elegant default). Old history
chunks are **not** re-translated.

### Better transcription accuracy (recommended, free)

If you speak but the *wrong words* come out, the local `small` model is usually
the culprit. The biggest, free fix is to let **Groq transcribe in the cloud**
with `whisper-large-v3` — far more accurate (especially for non-English speech),
fast, and it offloads your CPU.

1. Set a free `GROQ_API_KEY` (same key as for translation — see below).
2. Leave `STT_ENGINE=auto` (the default). With a key present it automatically
   uses Groq; without one it stays fully local. You'll see
   `Speech-to-text ready (Groq cloud / whisper-large-v3)` on startup.

Other knobs:

- **Stay offline / always local?** In `.env`:
  ```env
  STT_ENGINE=local
  WHISPER_MODEL=small              # daily CPU default (fast first boot)
  WHISPER_DEVICE=cpu
  WHISPER_COMPUTE_TYPE=int8
  WHISPER_BEAM_SIZE=1
  WHISPER_CPU_THREADS=4
  ```
  Larger models (`medium`, `large-v3-turbo`) need more RAM and the **first run
  downloads + loads** from Hugging Face (can look like a hang for several minutes
  on CPU). Prefer `small`/`base` for interactive use; use GPU
  (`WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE_TYPE=float16`) for large models.
- **Wrong names/jargon?** Set `STT_INITIAL_PROMPT` to a sentence containing the
  expected vocabulary, names and accents — it biases both engines.

> If the Groq key/network fails at **startup**, LiveLingo falls back to the local
> Whisper model. With default HA settings it also fails over **mid-session**
> (timeout / 429 / network) to local Whisper without quitting — see
> [Provider failover](#provider-failover-ha) below.

### Better translation quality (optional, free LLM)

By default the tool uses Google Translate. For **far more natural** results
(the "Typeless" effect — an LLM cleans up the raw speech-to-text *and*
translates it in one step), plug in a **free Groq API key**:

1. Go to **<https://console.groq.com/keys>** → sign up (no credit card).
2. Create a key (starts with `gsk_…`) and copy it.
3. Put it in your `.env`:

   ```
   GROQ_API_KEY=gsk_your_key_here
   ```

4. Run `python main.py` — you'll see `LLM translation ready (Groq / …)` plus a
   quick self-test. With `TRANSLATION_ENGINE=auto`, the LLM is used whenever a
   key is present, otherwise it falls back to Google.

> Privacy note: with the Groq STT engine, your **audio** is sent to Groq for
> transcription; with the LLM engine, the recognized **text** is sent for
> translation. To keep audio fully local, set `STT_ENGINE=local`. Without a key,
> STT runs locally and only Google Translate is used.

### Provider failover (HA)

LiveLingo keeps the session alive when a cloud provider blips. Wrappers in
`livelingo/failover.py` sit in front of STT and translation:

| Layer | Primary | Automatic fallback (default) |
|-------|---------|------------------------------|
| **STT** | Groq Whisper | Local faster-whisper (`STT_FALLBACK=local`) |
| **Translation** | Groq LLM | Google Translate (`TRANSLATION_FALLBACK=google`) |

- Transient errors (timeout, 429, DNS/network): up to `FAILOVER_MAX_RETRIES` on
  the primary, then secondary. A **circuit breaker** stops hammering a dead
  primary for `CIRCUIT_COOLDOWN_S` seconds.
- Permanent errors (401 / bad model 404): open the circuit; use secondary only.
- Boot self-test failure no longer exits the app if a fallback exists.
- Local Whisper can warm in a background thread (`STT_WARMUP_LOCAL=true`) so the
  first mid-session fallback does not wait on a cold model load (processor
  thread only; TUI stays responsive).
- System panel: rate-limited `[ha] …` lines when a fallback or restore happens.

**Limits:** airplane mode / total offline still cannot translate (both LLM and
Google need network). STT can continue via local Whisper. For a true offline
translate path you need phrase-cache HITs or a future local LLM.

Disable HA (not recommended for live sessions):

```env
STT_FALLBACK=none
TRANSLATION_FALLBACK=none
```

---

## 5. Run it

```powershell
python main.py
```

### Dev auto-reload

LiveLingo is a long-running CLI: **Python does not hot-reload** source on save
(unlike Flask/FastAPI `--reload`).

```powershell
python dev_reload.py
python dev_reload.py -v
```

**Must** start via `dev_reload.py` (not bare `python main.py`). Uses content
hashes (WSL `/mnt/c` safe). Restarts `main.py` on `.py` save; session state is
lost each time. Stop with **Ctrl+C** on the watcher terminal.

You'll see a banner, the detected/selected devices, and then a live status line
per chunk:

```
[chunk 3] Heard: "bonjour tout le monde" -> Translated: "hello everyone"
          ⏱  STT 1.20s | translate 0.30s | TTS 0.55s | total 2.05s
```

Stop any time with **Ctrl+C**.

### TUI (default)

With `UI_MODE=tui` (default) you get a Textual UI: log tabs, command field, fixed listen header (robot + language pair + audio), and a full-width command menu. Footer labels follow `SOURCE_LANG`. Set `UI_MODE=classic` for the legacy print/readline UI.

| Tab | Content |
|-----|---------|
| **Tradução** | Vertical split: **LC** (left, stable LiveCaptions pairs) \| **VOZ** (right, mic chunks + command output). Drag the **║** sash to resize width (double-click → 50/50). **Expandir/Restaurar** on the **VOZ** header maximizes the right pane. Click a pane to focus it for search / scroll / copy. Independent scroll per side. |
| **Sistema** | Pipeline stages, VAD/listen lines, timings, debug, **F1** help |
| **Novidades** | Project `CHANGELOG.md` (Markdown) |
| **Lista de comandos** | Grouped command help in `SOURCE_LANG` |

Above the tabs: **Live Captions** strip (partials live). Drag the bottom edge (`═ ↕ captions ═`) to grow/shrink captions vs the middle log tabs. On the **same row as the log tabs** (right side): compact **F2** bypass chip and **F5** auto-scroll chip (both LC + VOZ).

| Shortcut / command | Action |
|--------------------|--------|
| `F1` | Startup help → **Sistema** tab (opens that tab) |
| `F3` | Cycle log tabs (Tradução → Sistema → Novidades → Lista de comandos) |
| `F4` / `u` | Compact UI: hide command menu; keep command line (optional window height shrink) |
| `F5` / click scroll chip | **Auto-scroll lock** for Tradução **LC + VOZ** — ON (green) follows new lines; OFF (amber) freezes the viewport while lines still append. Footer shows `Auto↓ ON` / `Auto↓ OFF`. `GG` still jumps once without re-enabling follow when OFF |
| `Ctrl+C` | Copy selected log text |
| `Ctrl+Shift+C` | Copy entire content of the **focused** log pane (on Tradução: LC or VOZ) |
| `F2` / click bypass chip / `b` | **Voice bypass** — stop Cable TTS (like `[x]`), then raw mic → CABLE; tips log on **Sistema**; press again to resume translate |
| `F10` / `cam closed` | Toggle **full-face freeze** from the closed-mouth photo (manual show/hide; does **not** recapture — use `cam snap closed` to update) |
| Palette → Screenshot | Save SVG+PNG under `.cache/screenshots/` and put the **image** on the clipboard |
| `↑` / `↓` | Command history |
| `/text` · `/n` · `/p` | Search in the focused log pane (aliases: `find text`, `s?text`) |
| `g` | Swap SOURCE ↔ TARGET |
| `t` / `t EN` | Change TARGET only (codes forced UPPERCASE) |
| `enew <text>` | New translation from typed text (no mic); TTS if sound ON |
| `e` / `eN` | Edit last / chunk N (TUI pre-fills the sentence in the command field) |
| `gg` / `GG` (or `gt` / `gf`) | Go top / go bottom of the **focused** log pane. `GG` is case-sensitive. |
| `cls` | Clear LC + VOZ + Sistema |
| `cls1` / `cls2` | Clear only left **LC** / right **VOZ** |
| `l` / `lo` / `lt` | List session (into matching panes) / source-only / target-only |
| `co` / `coN` / `codN` | Comment on a chunk / delete comment by id |
| `s` / `n` / `r` / `rN` | Sound, mic mute, replay |
| `cam` / `cam on` / `cam off` / `cam status` | Webcam lip-sync stream to **OBS Virtual Camera** (needs `requirements-webcam.txt` + OBS) |
| `cam snap closed` | **Capture / update** the closed-mouth **full-face photo** used by F10. Preview shows the freeze oval. Aliases: `cam snap`, `cam snapshot closed` |
| `a` / `aN` / `p` / `pN` | Copy audio path / open audio folder |
| `ld` | List audio devices (`python list_devices.py`) into the log |
| `lav` | List all edge-tts voices (`edge-tts --list-voices`) into the log |
| `lv` | List filtered voices (`en-US|en-GB|es-ES|es-MX|fr-FR`) into the log |
| `ctts <ShortName>` | Change `TTS_VOICE` (one-liner or prompt; no modal) |
| `q` | Quit |

### Faster per-phrase text (sentence split)

With **sound OFF** (default), a short pause after enough speech can emit a **sentence-sized chunk** immediately (STT + translate + UI) without waiting for the whole monologue. Configure in `.env`:

| Setting | Default | Meaning |
|---------|---------|---------|
| `SENTENCE_SPLIT` | `true` | Early emit on short pauses |
| `SENTENCE_SPLIT_SOUND_OFF_ONLY` | `true` | Only when audio is OFF (safer with live TTS) |
| `SENTENCE_SILENCE` | `0.55` | Pause (s) treated as end-of-sentence |
| `SENTENCE_MIN_SPEECH` | `1.0` | Min speech (s) before a split |
| `SENTENCE_SPLIT_OVERLAP` | `0.25` | Overlap kept after a split |

For lower end-of-turn wait, also lower `SILENCE_DURATION` / `SOUND_OFF_SILENCE_DURATION` (e.g. `0.7`–`0.8`).

Resume a session without the picker:

```powershell
python main.py <session_id>
# or
livelingo <session_id>
```

### Use it as your microphone (and camera) in Microsoft Teams / Google Meet

**LiveLingo `.env` (example):** `INPUT_DEVICE=<your mic>` · `OUTPUT_DEVICE=CABLE Input` (or its index, e.g. `19`).

#### Audio only (classic)

| Role | Device |
|------|--------|
| You speak | Real microphone (e.g. USB headset) |
| LiveLingo plays translation | **CABLE Input** (`OUTPUT_DEVICE`) |
| Teams / Meet mic | **CABLE Output** |
| You hear others | Your speakers/headphones (app speaker setting) |

1. Keep `main.py` running; press **`s`** if you want live translation audio on the cable.
2. **Teams:** Settings → Devices → Microphone = **CABLE Output**; Speakers = your headset.
3. **Google Meet (browser):** ⋯ → Settings → Audio → same mic/speaker choices; allow the site to use the mic.
4. Speak in `SOURCE_LANG` → participants hear `TARGET_LANG`.

> **You do not hear your own CABLE feed in Teams/Meet by default** (no mic sidetone). The CABLE Output **level meter** moving means audio is entering the call. To hear the translation yourself: `MONITOR_PLAYBACK=true` + `MONITOR_DEVICE=<speakers>`, or Windows 11 *Sound settings → More sound settings → Recording → CABLE Output → Properties → Listen → Listen to this device*.

**Speak English (or any language) without translation:** press **`b`** (bypass) — first press **cuts any TTS on Cable** (same idea as `[x]`), then raw mic goes to CABLE; press **`b`** again to leave bypass and resume translate.

#### Audio + video with OBS Virtual Camera (v1.1 lip-sync)

So participants see **your face** while the **mouth stays closed** when you speak the source language, and moves with the **translated** TTS on Cable:

| Role | Device / setting |
|------|------------------|
| You speak | Real microphone |
| LiveLingo TTS (translation) | **CABLE Input** |
| Teams / Meet **microphone** | **CABLE Output** |
| LiveLingo video | **OBS Virtual Camera** (`pyvirtualcam` + OBS driver) |
| Teams / Meet **camera** | **OBS Virtual Camera** (not the physical webcam) |
| You hear others / pre-TTS bip | Headset; set `MONITOR_DEVICE` to your headphones index |

1. Install OBS and register **Virtual Camera** once ([section 1](#install-obs-studio-virtual-camera-for-teams-video)).
2. `pip install -r requirements-webcam.txt` and enable in `.env`:
   ```env
   WEBCAM_ENABLED=true
   WEBCAM_START_ENABLED=false   # or true to start streaming on boot
   WEBCAM_DEVICE_INDEX=0
   MONITOR_DEVICE=18            # your headphones index (list_devices.py) — cue only
   TTS_MONITOR_CUE=true
   ```
3. Run LiveLingo with **Windows Python** (not WSL) so Teams can see OBS Virtual Camera.
4. Commands: **`cam on`** → stream face; **`cam snap closed`** to capture the closed-mouth
   background (see below); **`s`** so TTS reaches Cable; **`cam status`** should show `vcam=true`.
5. **Teams:** Camera = **OBS Virtual Camera**, Microphone = **CABLE Output**.

##### Generate / update the closed-mouth **full-face** photo (`cam snap closed`)

The virtual cam freezes a **photo of your whole face with mouth closed** (soft-blended
face oval on the live video) while you speak the source language, so Teams does not
see source-language lip motion. **F10** only shows/hides that plate — it does **not**
recapture. There is **no** procedural “virtual teeth” paint (removed).

1. Enable webcam (`WEBCAM_ENABLED=true`) and start the stream: **`cam on`**  
   (or `WEBCAM_START_ENABLED=true`).
2. Face the camera with a **natural closed mouth** (same lighting/angle as the call).
3. Type:
   ```text
   cam snap closed
   ```
   Aliases: `cam snap` · `cam snapshot closed` · `cam capture closed`
4. OpenCV preview (Windows): green **full-face** oval = freeze region.  
   **SPACE/ENTER** save · **ESC** cancel · or ~3s auto-save with face lock.
5. Files (defaults):
   ```text
   .cache/webcam/closed_mouth.png
   .cache/webcam/closed_mouth.json
   ```
6. **`cam status`** → `tpl=true`. Toggle: **F10** / `cam closed` · `cam closed auto` (VAD).

Plate size: `WEBCAM_TEMPLATE_REGION_SCALE` (default `1.15`) · feather
`WEBCAM_TEMPLATE_FEATHER_PX` (default `24`). Paths:
`WEBCAM_CLOSED_MOUTH_IMAGE` · `WEBCAM_CLOSED_MOUTH_LANDMARKS`.

**OBS:** while LiveLingo streams, **Stop Virtual Camera** in OBS (exclusive producer).
LiveLingo owns the device; Teams camera = **OBS Virtual Camera**.

Full guide: [`docs/webcam-lipsync.md`](docs/webcam-lipsync.md).

---

## 6. Troubleshooting

**"VB-Cable was not found" / it exits immediately.**
Install VB-CABLE (section 1) and **reboot**. Re-run `python list_devices.py` to
confirm "CABLE Input" appears. If you renamed it, set `OUTPUT_DEVICE` to its
index.

**Teams doesn't pick up any audio.**
Make sure Teams' microphone is **CABLE Output** (the *Output* one), not CABLE
Input. Also confirm `main.py` is actually producing chunks (status lines appear).
Press **`s`** if sound is still OFF (default).

**Teams camera black / no virtual cam / `vcam=false`.**
Install OBS Studio, **Start Virtual Camera** once as admin, then stop and quit OBS.
Privacy → Camera → allow desktop apps. Run LiveLingo on **Windows Python**, not WSL.
See [`docs/webcam-lipsync.md`](docs/webcam-lipsync.md).

**Pre-TTS beep also in Teams.**
Set `MONITOR_DEVICE` to your **headphones** index (not CABLE Input). Cue must never
use Cable. Check Sistema log: `TTS cue → [N] … (SOMENTE monitor; NUNCA Cable/Teams)`.

**Short words get cut off / it never sends a chunk.**
Adjust VAD: lower `SILENCE_THRESHOLD` (e.g. `0.008`) if your mic is quiet, or
shorten `SILENCE_DURATION`. If background noise constantly triggers it, raise
`SILENCE_THRESHOLD`. You can also set `VAD_ENABLED=false` for fixed 4-second
chunks.

**Whisper hallucinates phrases during silence** (e.g. random subtitles).
Keep `WHISPER_VAD_FILTER=true` (default) and raise `SILENCE_THRESHOLD` a bit so
silent chunks aren't sent.

**I say words but the wrong words come out** (poor accuracy).
The local `small` model is usually the cause. Best fix: set a free `GROQ_API_KEY`
and keep `STT_ENGINE=auto` to transcribe with Groq's `whisper-large-v3`. To stay
offline, raise the local model (`WHISPER_MODEL=large-v3-turbo`). See "Better
transcription accuracy" above.

**It's too slow / chunks pile up** (`processing is N chunks behind`).
Use `STT_ENGINE=groq` to offload transcription to the cloud, or a smaller local
model: `WHISPER_MODEL=base` or `tiny`. Set `WHISPER_BEAM_SIZE=1` for extra speed.

**`Could not decode TTS audio` / soundfile MP3 error.**
Ensure `soundfile>=0.12.1` is installed (`pip install -U soundfile`); older
versions can't decode the MP3 that edge-tts returns.

**TTS fails with `403, Invalid response status`.**
Microsoft's TTS endpoint requires a time-based token (`Sec-MS-GEC`) that older
edge-tts versions can't generate. Fix: `pip install --upgrade edge-tts` (use
7.x or newer). Also make sure your **system clock is correct** — the token is
time-based, so a clock off by more than a few minutes also causes 403.

**Translation/TTS errors about network.**
deep-translator and edge-tts need internet. Check connectivity / proxy /
firewall. Transient failures skip a single chunk and the tool keeps running.

**Wrong microphone is captured.**
Set `INPUT_DEVICE` to the correct index from `list_devices.py`.

**GPU acceleration (optional).**
With an NVIDIA GPU + CUDA/cuDNN installed, set `WHISPER_DEVICE=cuda` and
`WHISPER_COMPUTE_TYPE=float16` for much faster STT.

---

## Project layout

```text
.
├── main.py             # entry point — wires everything together
├── config.py           # all tunable settings (env / .env overridable)
├── list_devices.py     # prints audio devices + their indices
├── requirements.txt    # runtime deps (security floors documented above)
├── requirements-webcam.txt # optional: OpenCV + MediaPipe + pyvirtualcam (OBS VC)
├── requirements-dev.txt # pytest + pip-audit + ruff (CI / pre-deploy)
├── docs/
│   └── webcam-lipsync.md       # OBS Virtual Camera + lip-sync guide
├── .github/workflows/
│   └── ci.yml                  # GitHub Actions: security + pytest (badge)
├── scripts/
│   ├── run_checks.sh           # WSL: format → security → tests
│   └── check_deps_security.py  # OWASP A06 audit (CVE + outdated)
├── tests/              # unit tests (floors, mocks, import smoke)
├── .env.example        # copy to .env to override settings
├── README.md
└── livelingo/          # modular pipeline package
    ├── capture.py      # mic -> audio chunks (energy VAD or fixed chunks)
    ├── transcribe.py   # local faster-whisper STT
    ├── groq_transcribe.py # Groq cloud Whisper STT (higher accuracy, optional)
    ├── failover.py     # runtime HA: STT Groq→local, LLM→Google (circuit breaker)
    ├── translate.py    # deep-translator (Google) translation
    ├── llm.py          # Groq LLM translation (higher quality, optional)
    ├── synthesize.py   # edge-tts TTS -> numpy audio
    ├── playback.py     # audio -> VB-Cable output device
    ├── monitor_cue.py  # pre-TTS beep on headphones only (never Cable)
    ├── pipeline.py     # threads + queues orchestration
    ├── devices.py      # device discovery / resolution
    ├── tui_app.py      # Textual TUI (log, menu, screenshot, clipboard)
    ├── db.py           # SQLite sessions, chunks, comments, favorites
    ├── ui.py           # terminal banner, colors, status lines
    └── webcam/         # optional lip-sync → OBS Virtual Camera
```

## Notes & limitations

- This is **chunked** near-real-time translation, not simultaneous interpreting:
  there is inherent latency (record an utterance → transcribe → translate →
  speak). Expect ~1–4 s after you finish a sentence.
- Translation and TTS quality depend on the free Google/Edge services.
- Privacy depends on the engines: with `STT_ENGINE=local` your audio never
  leaves the machine (only the recognized text and TTS request do). With the
  Groq STT engine, audio chunks are sent to Groq for transcription.
