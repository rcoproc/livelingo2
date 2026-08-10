# LiveLingo — Fluxo de Informação e Funcionalidades

## Diagrama de Fluxo Geral

```
ENTRADAS                    PROCESSAMENTO                     SAÍDAS
────────                    ─────────────                     ──────
Mic físico                 STT (Groq/Local Whisper)          VB-Cable → Teams/Zoom ouvem tradução
   ↓                           ↓
LiveCaptions Win11          Tradução (Groq LLM/Google)       TUI Textual (4 abas)
   ↓                           ↓
Comandos texto [ex]         Cache de Frases (LRU+SQLite)      SQLite DB (sessions/chunks)
   ↓                           ↓
Webcam física               TTS (edge-tts/Piper)              Webcam virtual → OBS → Teams
   ↓                           ↓
.env / config.py            Audio Ring Buffer                 Áudio cache WAV + Export Markdown
                                ↓
                            pyvirtualcam → lip-sync
```

---

## 3 Pipelines Paralelos

### 1. Pipeline de Áudio (3 threads)

```
[recorder thread]  Mic → chunk_queue
[processor thread] chunk_queue → STT → Tradução → TTS → playback_queue
[playback thread]  playback_queue → VB-Cable "CABLE Input"

Teams/Zoom/Discord ouvem "CABLE Output" como microfone.
```

### 2. Pipeline de Webcam (3 threads)

```
[capture thread]   cv2.VideoCapture → frame mais recente → capture_queue (maxsize=1, drop-old)
[infer thread]     capture_queue → MediaPipe Face Mesh (468 landmarks) → FaceMouthROI
                   → LipSyncEngine.infer(mouth_bgr, audio, sr, roi) → infer_queue (maxsize=2)
[emit thread]      infer_queue → soft blend → pyvirtualcam.send() → OBS Virtual Camera → Teams
```

### 3. Pipeline de Legendas (1 thread)

```
Windows 11 LiveCaptions → UIAutomation scrape → sentence queue → tradução → TUI strip
```

---

## Integração TTS + Lip-Sync

O áudio TTS que sai pelo VB-Cable é injetado simultaneamente no `AudioRingBuffer` do webcam:

```python
# pipeline.py — após enfileirar playback:
if self._webcam_service:
    self._webcam_service.push_tts_audio(audio, sample_rate)

# webcam/audio_ring.py — timeline agendado:
# push() agenda clipe TTS no timeline wall-clock
# latest(seconds) retorna janela de áudio tocando AGORA
```

O lip engine lê o trecho de áudio que está tocando no momento e sincroniza o movimento labial com o áudio que chega ao Teams.

---

## Entradas do Sistema

| Entrada | Módulo | Descrição |
|---------|--------|-----------|
| Mic físico | `capture.py` | PortAudio InputStream 16kHz mono, chunks com VAD (energia/Silero) |
| LiveCaptions Win11 | `livecaptions.py` | UIAutomation scraping da janela de legendas do Windows |
| Comandos de texto | `main.py` / `tui_app.py` | `[e]` editar, `[enew]` texto novo, `[g]` swap, etc. |
| Config (.env) | `config.py` | 232+ variáveis com override por env var |
| Webcam | `webcam/service.py` | OpenCV VideoCapture → frames BGR |

---

## Saídas do Sistema

| Saída | Módulo | Descrição |
|-------|--------|-----------|
| Áudio traduzido | `playback.py` | OutputStream no VB-Cable "CABLE Input" |
| TUI (Textual) | `tui_app.py` | Abas: Tradução, Sistema, Novidades, Comandos |
| SQLite DB | `db.py` | Sessions, chunks, comments, favorites, translation_pairs |
| Áudio cache | `.cache/audio_sessions/` | WAVs salvos por sessão |
| Export markdown | `main.py` | Transcrição formatada da sessão |
| Webcam virtual | `webcam/service.py` | pyvirtualcam → OBS → Teams |

---

## Funcionalidades Principais

### Core Pipeline
- **STT**: `auto` (Groq se key, senão local), `groq`, `local` (faster-whisper: tiny→large-v3)
- **Tradução**: `auto` (LLM se key, senão Google), `llm` (Groq), `google` (deep-translator)
- **TTS**: `edge` (Edge neural), `piper` (local ONNX), `hybrid` (edge 1º chunk + piper tail)
- **VAD**: Energia (RMS) ou Silero neural; thresholds adaptativos

### Controle de Áudio
- `[s]` — toggle TTS ao vivo
- `[n]` — mute do mic via Windows Core Audio (pycaw)
- `[b]` — bypass: mic direto → VB-Cable sem tradução
- `[r]`/`[rN]` — replay de chunks
- `[x]` — interromper TTS atual

### Idiomas
- `[g]` — swap SOURCE↔TARGET
- `[t <code>]` — mudar target
- Suporte: en, pt, es, fr, de, it, zh, ja

### Sessão
- `[v]` — switch de sessão
- `[c]` — export markdown
- `python main.py <session_id>` — resume

### Phrase Cache
- `pc on/off`, `pc force`, `pc last`, `pc good/bad`, `pc undo`, `pc backup/restore`

### Webcam
- `[cam on|off|status]` — controle da webcam virtual
- `[cam snap closed]` — foto de boca fechada (template)
- `F10` — freeze full-face plate

### Failover (HA)
- STT: Groq → local Whisper (circuit breaker: 3 falhas / 60s cooldown)
- Tradução: Groq LLM → Google Translate

---

## Estrutura de Módulos

```
main.py                    ← Entry point, CLI, session picker, loop de comandos
config.py                  ← 590 linhas: todas configs + env override
list_devices.py            ← Lista dispositivos de áudio com índices

livelingo/
├── pipeline.py            ← Orchestrator: 3 threads, queues, session resume
├── capture.py             ← Mic → chunks (VAD, split sentences)
├── transcribe.py          ← faster-whisper local (CTranslate2)
├── groq_transcribe.py     ← Groq cloud Whisper STT
├── failover.py            ← Circuit breaker + HA wrappers
├── translate.py           ← Google Translate (deep-translator)
├── llm.py                 ← Groq LLM translation com streaming
├── synthesize.py          ← edge-tts TTS, voice catalog
├── playback.py            ← OutputStream persistente no VB-Cable
├── db.py                  ← SQLite schema + CRUD
├── devices.py             ← PortAudio device discovery
├── tui_app.py             ← Textual full-screen TUI (6647 linhas)
├── ui.py                  ← Terminal colors, log sinks, stage bar
├── phrase_cache.py        ← Translation memory LRU + SQLite
├── livecaptions.py        ← Windows 11 LiveCaptions scrape
├── mic_control.py         ← Windows Core Audio mute
├── synonyms.py            ← WordNet / Groq synonym lookup
├── tts_segments.py        ← Sentence splitting para TTS streaming
├── stt_filter.py          ← Filtro de alucinação / baixa energia
├── hybrid_tts.py          ← Edge + Piper TTS híbrido
├── piper_tts.py           ← Piper TTS local (ONNX)
├── piper_voices.py        ← Catálogo de vozes Piper
├── vad_silero.py          ← VAD neural Silero
└── webcam/
    ├── service.py         ← 3-thread pipeline, audio ring push
    ├── engines.py         ← Lip engines: passthrough, amplitude, ONNX
    ├── face_roi.py        ← MediaPipe Face Mesh → mouth ROI
    ├── mouth_template.py  ← Closed-mouth template alignment, F10 freeze
    └── audio_ring.py      ← Scheduled TTS audio timeline
```

---

## Skill de Desenvolvimento

Skill para devs Python experientes salva em:

`/home/rcoproc/.agents/skills/livelingo-dev/SKILL.md`

Cobre arquitetura completa, padrões de código, como adicionar features/engines/comandos, e referências exatas por arquivo:linha para cada ponto crítico do sistema.

---

## Comandos Rápidos

```bash
python main.py                      # Iniciar com session picker
python main.py <session_id>         # Resume sessão específica
python main.py --list-sessions      # Listar todas sessões
python main.py --verbose            # Debug detalhado

python -m pytest tests/ -v          # Testes
ruff check .                        # Lint
pip-audit                           # Segurança
```
