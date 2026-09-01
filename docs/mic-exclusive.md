# Microfone exclusivo e Teams / Meet (LiveLingo)

Como impedir que **Google Meet**, **Microsoft Teams** (ou Zoom) ouçam a sua **voz crua** enquanto o LiveLingo está em escuta — e como usar **WASAPI exclusive** no Windows.

```text
Correto (dois caminhos separados):

  Mic físico ──► LiveLingo (STT → Trad → TTS) ──► CABLE Input
                                                      │
  Teams / Meet ◄── microfone = CABLE Output ◄─────────┘
       (só ouve a tradução, não o headset)

Errado (mic compartilhado):

  Mic físico ──shared──► LiveLingo
           └─shared──► Teams / Meet   ← participantes ouvem você “ao vivo”
```

---

## 1. Camada A — roteamento VB-Cable (obrigatório)

O LiveLingo **não** substitui a escolha de microfone do Meet. Se o Meet estiver no mesmo mic físico, o Windows entrega o áudio aos dois apps (modo compartilhado).

| App | Microfone | Saída de áudio |
|-----|-----------|----------------|
| **LiveLingo** | Mic físico (headset / notebook) — `INPUT_DEVICE` | **`CABLE Input`** (`OUTPUT_DEVICE`) |
| **Teams / Meet / Zoom** | **`CABLE Output` (VB-Audio Virtual Cable)** | Fones (não alto-falante aberto) |
| Você ouve os outros | — | Fones |

### Checklist Meet / Teams

1. Instale o [VB-CABLE](https://vb-audio.com/Cable/) e reinicie o Windows se ainda não tiver.
2. Confira com `python list_devices.py`: deve existir **CABLE Input** (saída) e **CABLE Output** (entrada).
3. No LiveLingo (`.env`):

   ```env
   INPUT_DEVICE=          # vazio = mic padrão, ou substring / índice do headset
   OUTPUT_DEVICE=CABLE Input
   ```

4. No **Teams / Meet / Zoom** → Configurações → Áudio / Dispositivos:
   - **Microfone** = `CABLE Output` (nunca o “Microphone Array” / headset físico)
   - **Alto-falantes** = fones
5. Faça um teste: fale no LiveLingo; no Meet os outros devem ouvir **só a tradução** (TTS), não a fala crua.

Com este setup, exclusive é opcional — o Meet **já não deveria** abrir o mic físico.

---

## 2. Camada B — WASAPI exclusive (opcional, Windows)

Trava o mic físico no LiveLingo enquanto o stream de captura estiver aberto. Outros apps que tentem o mesmo endpoint tendem a **falhar** ou não capturar.

```env
# .env
CAPTURE_WASAPI_EXCLUSIVE=true
CAPTURE_EXCLUSIVE_FALLBACK=true
```

| Variável | Default | Efeito |
|----------|---------|--------|
| `CAPTURE_WASAPI_EXCLUSIVE` | `false` | `true` = abre captura com `WasapiSettings(exclusive=True)` e prefere o índice **WASAPI** do `INPUT_DEVICE` |
| `CAPTURE_EXCLUSIVE_FALLBACK` | `true` | Se o open exclusive falhar (USB, device ocupado), cai para mic **compartilhado** e avisa na aba Sistema |

### Comportamento

- Resolver de device (`devices.resolve_device`) com `prefer_hostapi=wasapi` quando a flag está ligada.
- `Recorder` (`capture.py`) e bypass `[b]` / F2 usam o mesmo `extra_settings` exclusive.
- Bypass continua a **soltar** o mic antes de reabrir (dois opens exclusivos no mesmo device congelam PortAudio no Windows).
- Fora do Windows / sem `WasapiSettings`: a flag é efetivamente no-op (sem crash).

### Quando usar

| Situação | Recomendação |
|----------|----------------|
| Meet já em CABLE Output | Exclusive **opcional** (rede de segurança) |
| Meet às vezes volta para o headset | `CAPTURE_WASAPI_EXCLUSIVE=true` |
| Headset USB instável / LiveCaptions no mesmo mic | Comece com `false`; se exclusive falhar, o fallback shared avisa no log |

Reinicie o LiveLingo após mudar o `.env`. No boot, com exclusive ON, aparece um aviso lembrando: Meet → CABLE Output.

---

## 3. Relacionado — o que **não** resolve “Meet ouve minha voz”

### `MUTE_CAPTURE_DURING_PLAYBACK` (gate de STT, não exclusive)

```env
MUTE_CAPTURE_DURING_PLAYBACK=true   # padrão: fecha escuta STT enquanto TTS toca
MUTE_CAPTURE_HANGOVER_MS=700        # ms após o TTS antes de reabrir (anti-eco)
```

- Serve para **não gravar o TTS** no mic (feedback speaker→mic).
- **Não** impede o Teams de usar o mic físico.
- Com fones + Cable, pode usar `MUTE_CAPTURE_DURING_PLAYBACK=false` para gravar a frase 2 enquanto a 1 toca (ver também `SOUND_ON_PARALLEL`).

### Tecla `[n]` / mute OS (`mic_control` / pycaw)

- Muta o **endpoint de captura inteiro** no Windows (bandeja).
- Silencia **LiveLingo e** qualquer app nesse mic — **não** use como “escuta privada” com STT ativo.
- Use para pausar a escuta de propósito.

Não existe API estável no produto para “mutar só o Teams e deixar o LiveLingo gravar”.

---

## 4. Bypass de voz (`[b]` / F2)

Com bypass ON, a voz crua vai **direto** para `OUTPUT_DEVICE` (CABLE Input) → quem está em CABLE Output no Meet **ouve a voz sem tradução**. Isso é intencional. Exclusive continua a aplicar no InputStream do bypass quando a flag está ligada.

---

## 5. Troubleshooting

| Sintoma | O que checar |
|---------|----------------|
| Meet ouve fala crua + tradução | Microfone do Meet ainda é o headset → mudar para **CABLE Output** |
| Exclusive ON mas Meet ainda ouve | Meet em outro device; ou fallback shared (veja log Sistema: “WASAPI exclusive falhou”) |
| Escuta não abre / reabre em loop | USB exclusive falhou; `CAPTURE_EXCLUSIVE_FALLBACK=true` ou desligar exclusive |
| Eco / STT “ouve” o TTS | Fones; `MUTE_CAPTURE_DURING_PLAYBACK=true`; subir `MUTE_CAPTURE_HANGOVER_MS` |
| Sem CABLE na lista | Instalar VB-Cable, reboot, `python list_devices.py` |

---

## 6. Referência rápida `.env`

```env
# --- Roteamento (sempre) ---
INPUT_DEVICE=
OUTPUT_DEVICE=CABLE Input

# --- Exclusive (Windows, opcional) ---
CAPTURE_WASAPI_EXCLUSIVE=false
CAPTURE_EXCLUSIVE_FALLBACK=true

# --- Anti-eco STT durante TTS (independente do Meet) ---
MUTE_CAPTURE_DURING_PLAYBACK=true
MUTE_CAPTURE_HANGOVER_MS=700
```

Código: `config.py`, `livelingo/devices.py` (`wasapi_exclusive_settings`, `resolve_device(..., prefer_hostapi=)`), `livelingo/capture.py`, bypass em `livelingo/pipeline.py`.

Ver também: README (seção Teams / mic), `.env.example` (bloco Audio devices).
