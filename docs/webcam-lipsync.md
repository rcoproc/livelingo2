# Webcam lip-sync → virtual camera (LiveLingo2)

Low-latency path that keeps **your face** on a virtual webcam while the **mouth**
tracks the **translated TTS** that LiveLingo already plays on **Cable Out**
(VB-Audio / PipeWire).

```text
Webcam ──► Face Mesh (MediaPipe) ──► mouth ROI ──► lip engine ──► blend + SYNC marker
TTS audio schedule (timeline from pipeline) ──────────▲
                                                              │
                                                      pyvirtualcam
                                                              │
                                                    Teams / Meet / Zoom
```

**Lip morph runs only while TTS is scheduled** (not while the mic is listening).

### Closed mouth = your photo (recommended)

Procedural “seal” always looks fake. Best quality:

1. `[cam on]` so the virtual cam is live  
2. Close your mouth naturally, face the camera  
3. Type: **`cam snap closed`**  
4. Idle (no TTS) will **warp + soft-blend** that photo onto your live face  

Files (defaults):

```env
WEBCAM_CLOSED_MOUTH_IMAGE=.cache/webcam/closed_mouth.png
WEBCAM_CLOSED_MOUTH_LANDMARKS=.cache/webcam/closed_mouth.json
WEBCAM_TEMPLATE_REGION_SCALE=1.15  # full-face freeze (1.0 tight … 1.4 larger)
WEBCAM_TEMPLATE_FEATHER_PX=24      # soft edge of face plate
```

The photo freezes the **whole face** (MediaPipe face oval: forehead → chin) on top of
the live video when F10 / closed-mouth mode is ON. Soft edges blend into the background;
caps leave a little room around the head (not a full-frame rectangle).

**When it applies (continuous loop):**

| Situation | Closed photo |
|-----------|----------------|
| VAD hears **speech** on mic | **ON** — freeze plate |
| Speech ends (hangover **1.5s**) | still ON (covers mic lag) |
| TTS / translated audio playing | **OFF** — live face |
| Silence after hangover | **OFF** — natural live face |

**F10 / `cam closed`:** toggle closed photo **manual** (ignores VAD until
`cam closed auto`). Use this to show/hide the plate whenever you want without
waiting for speech detection.

Driven by capture VAD (`on_listening`) when not in F10 manual mode.

If left/right looks wrong: `WEBCAM_TEMPLATE_FLIP_H=true` (default is `false`).
Hangover: `WEBCAM_SPEECH_HANGOVER_S=1.5`.
Plate is the **full face** (`WEBCAM_TEMPLATE_REGION_SCALE≈1.15`). Snap preview
shows the same oval/hull as F10. Larger face pad: `1.25`–`1.35`; tighter: `1.0`.

Re-snap if lighting or camera angle changes a lot.  
`cam status` shows `tpl=true` when the template is loaded.

### Pre-TTS cue (headphones only)

~1s before translation hits Cable/Teams, a soft double-beep plays on
`MONITOR_DEVICE` (never on Cable). Adjust:

```env
TTS_MONITOR_CUE=true
TTS_MONITOR_CUE_LEAD_S=1.0
MONITOR_DEVICE=          # empty = system default headphones
```

### Speaking (TTS)

Without a neural model, open is a **gentle warp from the closed base** (not a black oval).
For photoreal speech later: `WEBCAM_LIP_ENGINE=onnx` + a Wav2Lip-style export
(see engines section).

Optional debug overlay: `WEBCAM_SYNC_MARKER=true` (default). Set `false` for production.

## Why this design (vs loopback + full-frame AI every frame)

| Choice | Reason |
|--------|--------|
| Audio from **pipeline TTS**, not Cable loopback | No extra WASAPI graph; lower latency; same samples as Cable Out |
| **ROI-only** morph + soft mask | Avoid full-face re-render every frame; cheaper CPU/GPU |
| Queues `maxsize=2` **drop-old** | Cap latency; prefer current face over backlog |
| Engines: `amplitude` → `onnx` | Ship a working path without heavy weights; plug Wav2Lip later |
| Optional deps | Core LiveLingo stays light; webcam is opt-in |

Target: **~30 FPS**, end-to-end video path **&lt; 100 ms** on a modest GPU when
using `amplitude` or a small ONNX mouth model (full Wav2Lip 96×96 is typically
heavier — measure on your machine).

## Install

```bash
pip install -r requirements-webcam.txt
# equivalent:
# pip install opencv-python mediapipe pyvirtualcam
# Optional ONNX (CUDA):
# pip install onnxruntime-gpu
```

> **Important:** virtual camera = **video only**. Translated speech still goes to
> **VB-Cable** (`OUTPUT_DEVICE=CABLE Input`). In Teams you must set **two** devices:
>
> | Teams setting | Device |
> |---------------|--------|
> | **Camera** | **OBS Virtual Camera** (not the physical webcam) |
> | **Microphone** | **CABLE Output** |
>
> LiveLingo: press **`[s]`** so TTS plays into Cable. Without sound ON, Teams
> hears silence even if the virtual camera works.

### Virtual camera drivers

| OS | Driver |
|----|--------|
| **Windows** | [OBS Studio](https://obsproject.com/) → start **Virtual Camera** once (or Unity Capture) |
| **Linux** | `v4l2loopback` (`sudo modprobe v4l2loopback devices=1`) |
| **macOS** | OBS Virtual Camera |

In Teams/Meet, pick **OBS Virtual Camera** (or the loopback device) as the camera.

### Windows: `virtual camera output could not be started`

This is a **driver / OBS** problem, not LiveLingo capture (if `cap_ok=true`).

1. Install **OBS Studio** from https://obsproject.com/ (not Microsoft Store if the Store build lacks virtual cam).
2. Open OBS **as Administrator** once (right-click → Run as administrator).
3. Controls bottom-right → **Start Virtual Camera** (or Tools → Start Virtual Camera).
   - First start **installs/registers** the Windows virtual camera device.
4. **Stop Virtual Camera**, then fully quit OBS (so LiveLingo can open the same device).
5. Windows Settings → Privacy → Camera → allow **desktop apps**.
6. Confirm device exists: Windows Settings → Bluetooth & devices → Cameras, or
   `python list_devices.py` will not list video, but Teams device list should show
   **OBS Virtual Camera**.
7. Restart LiveLingo → `cam status` → need `vcam=true`.
8. Teams camera = **OBS Virtual Camera** (not the laptop webcam).

Also fails if:

| Symptom | Fix |
|---------|-----|
| OBS still has Virtual Camera running | Stop it in OBS, quit OBS |
| Another app holds OBS VC | Close Zoom/Meet preview using it |
| Only Unity errors | Install OBS (Unity Capture is optional) |
| Store / portable OBS | Use full installer from obsproject.com |

Quick self-test (**must be the same Python that runs LiveLingo**):

```powershell
# Windows PowerShell (NOT WSL) — Teams path
.\.venv\Scripts\Activate.ps1
python -c "import pyvirtualcam; from pyvirtualcam.camera import BACKENDS; print(list(BACKENDS)); c=pyvirtualcam.Camera(width=640,height=480,fps=30,backend='obs'); print(c.device); c.close()"
```

```bash
# Linux / WSL only — Teams on Windows will NOT see this device
python3 -c "import pyvirtualcam; from pyvirtualcam.camera import BACKENDS; print(list(BACKENDS))"
# backends = ['v4l2loopback'] → need: sudo modprobe v4l2loopback devices=1 exclusive_caps=1
```

**WSL trap:** `python3` inside WSL is Linux. Backend `obs` does not exist → `KeyError: 'obs'`.
Microsoft Teams on Windows only sees cameras registered in **Windows**. Run LiveLingo with
**Windows Python** (`.venv\Scripts\python.exe`) for Teams video.

If the Windows self-test raises, LiveLingo cannot fix it until OBS virtual cam works.

## Enable in LiveLingo

`.env`:

```env
WEBCAM_ENABLED=true
WEBCAM_START_ENABLED=false
WEBCAM_DEVICE_INDEX=0
WEBCAM_FPS=30
WEBCAM_LIP_ENGINE=amplitude
# WEBCAM_LIP_ENGINE=onnx
# WEBCAM_ONNX_MODEL=.cache/models/lipsync.onnx
```

Runtime commands:

| Command | Action |
|---------|--------|
| `cam` | Toggle enable (starts threads on first ON) |
| `cam on` | Stream to virtual cam |
| `cam off` | Pause stream (hold last frame) |
| `cam status` | FPS, face, template, engine, backend, errors |
| `cam snap closed` | Save closed-mouth photo template (idle) |
| `cam closed` / **F10** | Toggle closed-mouth photo **manual** ON/OFF |
| `cam closed auto` | Back to VAD auto (exit F10 manual mode) |

Sound must be **ON** (`[s]`) for TTS to reach the lip audio ring (same as Cable Out).

## Threading model

```text
[cam-capture]  VideoCapture.read  → q_cap (drop-old)
[cam-infer]    FaceMesh + engine  → q_out (drop-old)
[cam-emit]     blend already done → pyvirtualcam.send + sleep_until_next_frame
[pipeline]     _enqueue_playback  → audio_ring.push  (non-blocking)
```

Rules:

1. Never do OpenCV / ONNX on the Textual UI thread.
2. `push_tts_audio` must not raise or block the playback thread.
3. If infer is slow, **drop frames** — do not grow queues.
4. On `cam off`, emit thread re-sends last frame so Meet still sees a picture.

## Lip engines

### `amplitude` (default demo)

CPU morph of the mouth band driven by RMS of recent TTS. Not photoreal; validates
A/V coupling and the whole capture→vcam path in &lt;5 ms.

### `passthrough`

No morph — ROI recompose only. Useful to debug camera/vcam without lip motion.

### `onnx`

Generic ONNX Runtime session. Provider order: **TensorRT → CUDA → DML → CPU**.
Default tensor contract (adapt if your export differs):

- `face`: float32 `[1,3,H,W]` RGB 0..1  
- `audio`: float32 `[1,T]` mono peak-normalized  
- out: float32 `[1,3,H,W]` RGB 0..1  

Many public **Wav2Lip** graphs expect **mel** features, not raw PCM. Either:

1. Export/adapt a model to raw audio, or  
2. Subclass `OnnxLipSyncEngine` and replace `_prep_audio` with your mel pipeline.

Place weights under `.cache/models/` and set `WEBCAM_ONNX_MODEL`.

## Config reference

See `config.py` / `.env.example` keys `WEBCAM_*`.

## Manual test checklist

1. `WEBCAM_ENABLED=true`, `pip install -r requirements-webcam.txt`.  
2. **OBS driver (once):** Install OBS → run as Admin → **Start Virtual Camera** (registers driver) → **Stop Virtual Camera**.  
   LiveLingo is the *producer*; OBS Virtual Camera button must stay **OFF** while LiveLingo streams. OBS app may remain open.  
3. **Close Teams camera** (or leave Teams closed) so the **physical** cam is free for capture.  
4. Start LiveLingo → `[cam on]` → `cam status`: `enabled=true`, `vcam=true`, `cap_ok=true`, `fps_out` &gt; 0.  
5. Open Teams → camera = **OBS Virtual Camera** → preview shows face (or placeholder if capture failed).  
6. Teams mic = **CABLE Output**; LiveLingo `[s]` ON; speak → translation on Cable + mouth moves.  
7. If `err=` / `could not be started`: another app is outputting to OBS Virtual Cam (OBS button still On, or zombie process). Stop it, wait 2s, `[cam on]` again (auto-retries).  
8. If deps missing: reinstall webcam packages **in the same venv** that runs LiveLingo.  
9. If `capture_ok=false`: free physical cam / try `WEBCAM_DEVICE_INDEX=1`.  
10. `[cam off]` releases virtual cam; `[cam]` toggles; `[q]` clean shutdown.

## Limits (honest)

- Photoreal lip-sync needs a tuned ONNX/TensorRT model + GPU.  
- Full offline airplane mode: video works; no new TTS without cache.  
- Multi-face / profile view: Face Mesh may lose lock → passthrough full frame.  
- Do not point Meet at the **physical** camera if you need the processed stream —
  select the **virtual** device.
