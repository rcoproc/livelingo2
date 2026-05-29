# 🎙️ LiveLingo — Real-Time Voice Translation for Windows

**LiveLingo** turns your speech into another language **live**, on a virtual
microphone — so Microsoft Teams (or Zoom, Discord, Google Meet, OBS…) hears the
translation as if it were your mic. Speak **French**, others hear **English**
(both languages configurable).

```text
🎤 mic (French)
   └─► Whisper STT  (speech → French text: Groq cloud large-v3, or local)
        └─► translation (French → English: Google free, or Groq LLM for quality)
             └─► edge-tts  (English text → speech, free)
                  └─► VB-Cable  ──►  "CABLE Output" used as mic in Teams
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
| **VB-CABLE** | Free virtual audio cable. Download: **https://vb-audio.com/Cable/** |
| **Internet** | Needed for translation + TTS (and the first Whisper model download). |

### Install VB-CABLE

1. Download the VB-CABLE zip from <https://vb-audio.com/Cable/>.
2. Extract it, then **right-click `VBCABLE_Setup_x64.exe` → Run as administrator**.
3. Click **Install Driver**.
4. **Reboot Windows** (important — the device won't appear reliably until you do).

After reboot you will have two new devices:

- **CABLE Input (VB-Audio Virtual Cable)** — a *playback* device. **This tool sends the English speech here.**
- **CABLE Output (VB-Audio Virtual Cable)** — a *recording* device. **Teams selects this as its microphone.**

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
```

> When using the **local** STT engine, the first run downloads the Whisper model
> (`small` ≈ 0.5 GB, `medium` ≈ 1.5 GB) into `~/.cache/huggingface` — automatic,
> just wait once. With the **Groq** engine (recommended), no download is needed.

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
| `TTS_VOICE` | `en-US-AriaNeural` | Any Edge voice (`edge-tts --list-voices`) |
| `CHUNK_DURATION` | `4.0` | Target/fixed chunk length (seconds) |
| `VAD_ENABLED` | `true` | Split on pauses (true) vs fixed chunks (false) |
| `SILENCE_THRESHOLD` | `0.015` | Mic loudness threshold for speech detection |
| `MONITOR_PLAYBACK` | `false` | Also play the translation on your speakers (testing) |
| `MONITOR_DEVICE` | *(default out)* | Device for the monitor copy (index/name) |
| `TRANSLATION_ENGINE` | `auto` | `auto`/`llm`/`google` (see below) |
| `GROQ_API_KEY` | *(empty)* | Free Groq key → much better translation quality |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model (`llama-3.1-8b-instant` = faster) |

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

- **Stay offline?** Set `STT_ENGINE=local` and raise the local model:
  `WHISPER_MODEL=large-v3-turbo` (much more accurate than `small`, still
  reasonable on CPU) or `medium`.
- **Wrong names/jargon?** Set `STT_INITIAL_PROMPT` to a sentence containing the
  expected vocabulary, names and accents — it biases both engines.

> If the Groq key/network fails at startup, LiveLingo automatically falls back
> to the local Whisper model so it always works.

### Better translation quality (optional, free LLM)

By default the tool uses Google Translate. For **far more natural** results
(the "Typeless" effect — an LLM cleans up the raw speech-to-text *and*
translates it in one step), plug in a **free Groq API key**:

1. Go to **https://console.groq.com/keys** → sign up (no credit card).
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

---

## 5. Run it

```powershell
python main.py
```

You'll see a banner, the detected/selected devices, and then a live status line
per chunk:

```
[chunk 3] Heard: "bonjour tout le monde" -> Translated: "hello everyone"
          ⏱  STT 1.20s | translate 0.30s | TTS 0.55s | total 2.05s
```

Stop any time with **Ctrl+C**.

### Use it as your microphone in Microsoft Teams

1. Keep `main.py` running.
2. In Teams: **Settings (⋯ / your avatar) → Settings → Devices**.
3. Under **Microphone**, choose **CABLE Output (VB-Audio Virtual Cable)**.
4. Speak French → participants hear the English translation.

> The same applies to Zoom, Discord, Google Meet (in the browser, pick "CABLE
> Output" as the mic), OBS, etc.

**Tip:** to also hear yourself, set `MONITOR_PLAYBACK=true`, or in Windows
*Sound Control Panel → Recording → CABLE Output → Properties → Listen* enable
"Listen to this device" and pick your headphones.

---

## 6. Troubleshooting

**"VB-Cable was not found" / it exits immediately.**
Install VB-CABLE (section 1) and **reboot**. Re-run `python list_devices.py` to
confirm "CABLE Input" appears. If you renamed it, set `OUTPUT_DEVICE` to its
index.

**Teams doesn't pick up any audio.**
Make sure Teams' microphone is **CABLE Output** (the *Output* one), not CABLE
Input. Also confirm `main.py` is actually producing chunks (status lines appear).

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
├── requirements.txt    # pinned dependencies
├── .env.example        # copy to .env to override settings
├── README.md
└── livelingo/          # modular pipeline package
    ├── capture.py      # mic -> audio chunks (energy VAD or fixed chunks)
    ├── transcribe.py   # local faster-whisper STT
    ├── groq_transcribe.py # Groq cloud Whisper STT (higher accuracy, optional)
    ├── translate.py    # deep-translator (Google) translation
    ├── llm.py          # Groq LLM translation (higher quality, optional)
    ├── synthesize.py   # edge-tts TTS -> numpy audio
    ├── playback.py     # audio -> VB-Cable output device
    ├── pipeline.py     # threads + queues orchestration
    ├── devices.py      # device discovery / resolution
    └── ui.py           # terminal banner, colors, status lines
```

## Notes & limitations

- This is **chunked** near-real-time translation, not simultaneous interpreting:
  there is inherent latency (record an utterance → transcribe → translate →
  speak). Expect ~1–4 s after you finish a sentence.
- Translation and TTS quality depend on the free Google/Edge services.
- Privacy depends on the engines: with `STT_ENGINE=local` your audio never
  leaves the machine (only the recognized text and TTS request do). With the
  Groq STT engine, audio chunks are sent to Groq for transcription.
