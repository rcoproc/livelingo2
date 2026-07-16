# 🎙️ LiveLingo — Tradução de Voz em Tempo Real para Windows

**LiveLingo** transforma sua fala em outro idioma **ao vivo**, num microfone virtual — para que Microsoft Teams (ou Zoom, Discord, Google Meet, OBS…) ouça a tradução como se fosse seu microfone. Você fala **francês**, os outros ouvem **inglês** (ambos os idiomas são configuráveis).

Além da tradução em tempo real, o projeto evoluiu para uma **ferramenta de reuniões multilíngues** com histórico persistente por sessão, comandos interativos no terminal, exportação de transcrições com resumo executivo por IA e auxiliar de vocabulário.

```text
🎤 Microfone real
   └─► STT (Whisper local ou Groq cloud)
        └─► Tradução (Google ou LLM Groq)
             └─► TTS (edge-tts / vozes Microsoft)
                  └─► VB-Cable (CABLE Input)
                       └─► Teams usa "CABLE Output" como mic
```

Tradução e text-to-speech usam serviços públicos gratuitos (internet necessária). O reconhecimento de fala roda **na nuvem Groq** (mais preciso, recomendado) **ou** totalmente local com faster-whisper (offline).

---

## Visão geral

### Propósito principal

O caso de uso central é: **você fala em um idioma (ex.: francês) e os participantes da reunião ouvem em outro (ex.: inglês)** — sem precisar trocar de idioma manualmente.

### Arquitetura técnica

O projeto é modular, com pipeline multi-thread:

| Módulo | Responsabilidade |
|--------|------------------|
| `main.py` | Ponto de entrada, sessões, menu de comandos |
| `config.py` | Configuração central via `.env` |
| `livelingo/capture.py` | Captura de áudio + VAD (detecção de voz) |
| `livelingo/transcribe.py` | Whisper local (faster-whisper) |
| `livelingo/groq_transcribe.py` | Whisper na nuvem Groq |
| `livelingo/translate.py` | Google Translate (grátis) |
| `livelingo/llm.py` | Tradução via LLM Groq (mais natural) |
| `livelingo/synthesize.py` | Text-to-speech (edge-tts) |
| `livelingo/playback.py` | Saída para VB-Cable / monitor |
| `livelingo/pipeline.py` | Orquestração com 3 threads |
| `livelingo/db.py` | Persistência SQLite |
| `livelingo/devices.py` | Descoberta e resolução de dispositivos |
| `livelingo/ui.py` | Interface terminal colorida |

### Pipeline (3 threads)

```text
[Recorder Thread]  mic → chunk_queue
[Processor Thread] chunk_queue → STT → tradução → TTS → playback_queue
[Playback Thread]  playback_queue → VB-Cable / monitor
```

1. **Recorder** — captura áudio do microfone em chunks (VAD ou fixo)
2. **Processor** — STT → tradução → TTS
3. **Playback** — envia áudio sintetizado para o dispositivo virtual

---

## Funcionalidades

### 1. Tradução de voz em tempo real

- Captura contínua do microfone
- VAD (Voice Activity Detection) para cortar em frases naturais
- Latência típica de ~1–4 s após você parar de falar
- Indicador visual animado no terminal (🎙️ ouvindo / 🤖 aguardando)
- Modo verbose com `--verbose` para logs detalhados de debug

### 2. Múltiplos motores de STT

| Motor | Quando usar |
|-------|-------------|
| **Groq cloud** (`whisper-large-v3`) | Melhor precisão, recomendado com API key |
| **Local** (`faster-whisper`) | Offline, usa CPU/GPU local |
| **Auto** | Groq se tiver key, senão local |

Se a key Groq ou a rede falharem na inicialização, o LiveLingo faz fallback automático para o Whisper local.

### 3. Múltiplos motores de tradução

| Motor | Qualidade |
|-------|-----------|
| **LLM Groq** (`llama-3.3-70b`) | Corrige erros de STT e traduz de forma natural |
| **Google Translate** | Grátis, sem key, mais literal |
| **Auto** | LLM se tiver `GROQ_API_KEY`, senão Google |

### 4. Sessões persistentes (SQLite)

Ao iniciar, o aplicativo oferece:

- **[1] Nova sessão** — com título personalizado ou automático
- **[2] Retomar sessão** — carrega histórico, favoritos e áudio anterior
- **[99] Deletar sessão** — remoção atômica (banco + cache de áudio)

Cada chunk é salvo em `.cache/audio_sessions/{session_id}/` e registrado em `livelingo.db`.

### 5. Comandos interativos no terminal

Durante a escuta, digite comandos no terminal:

| Comando | Ação |
|---------|------|
| `r` / `rN` | Repetir áudio do último chunk ou do chunk N |
| `e` / `eN` | Editar e retraduzir frase |
| `d` / `dN` | Deletar chunk (com confirmação) |
| `f` / `fN` | Favoritar frase |
| `F` | Listar favoritos (modal) |
| `s` | Buscar sinônimos/significado de palavra em inglês |
| `c` | Exportar histórico para `.md` com resumo IA |
| `l` | Listar mensagens da sessão atual |
| `v` | Trocar ou reiniciar sessão |
| `m` | Mostrar menu de comandos |
| `q` | Sair da aplicação |

### 6. Exportação com resumo executivo IA

O comando `c` gera um arquivo Markdown (`AAAA-MM-DD_titulo.md`) contendo:

- **Resumo executivo** (assunto principal, resumo objetivo, tarefas/ações) via LLM Groq
- **Transcrição detalhada** chunk a chunk (idioma alvo + idioma de origem)
- **Vocabulário e sinônimos** consultados durante a sessão

Requer `GROQ_API_KEY` para o resumo automático.

### 7. Auxiliar de vocabulário

Comando `s`: explica uma palavra em inglês em português, com sinônimos e exemplos de frases. Requer o motor de tradução LLM (`TRANSLATION_ENGINE=llm` ou `auto` com key configurada).

### 8. Monitor de áudio

Com `MONITOR_PLAYBACK=true`, a tradução também é reproduzida nos seus fones/alto-falantes enquanto é enviada para o VB-Cable — útil para testes ou para ouvir a si mesmo durante a chamada.

---

## 1. Pré-requisitos

| Requisito | Observação |
|-----------|------------|
| **Windows 10/11** | O tool usa APIs de áudio do Windows (MME via PortAudio). WSL/Linux via `livelingo.sh`. |
| **Python 3.10+** | 3.10 – 3.12 recomendado. Verifique com `python --version`. |
| **VB-CABLE** | Cabo de áudio virtual gratuito. Download: **https://vb-audio.com/Cable/** |
| **Internet** | Necessária para tradução + TTS (e o primeiro download do modelo Whisper local). |

### Instalar VB-CABLE

1. Baixe o zip do VB-CABLE em <https://vb-audio.com/Cable/>.
2. Extraia e **clique com o botão direito em `VBCABLE_Setup_x64.exe` → Executar como administrador**.
3. Clique em **Install Driver**.
4. **Reinicie o Windows** (importante — o dispositivo pode não aparecer de forma confiável sem isso).

Após reiniciar, você terá dois novos dispositivos:

- **CABLE Input (VB-Audio Virtual Cable)** — dispositivo de *reprodução*. **O LiveLingo envia o áudio traduzido para cá.**
- **CABLE Output (VB-Audio Virtual Cable)** — dispositivo de *gravação*. **O Teams seleciona este como microfone.**

---

## 2. Instalação

Na pasta do projeto:

```powershell
# (opcional, mas recomendado) criar ambiente virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# instalar dependências
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Com o motor STT **local**, a primeira execução baixa o modelo Whisper (`small` ≈ 0,5 GB, `medium` ≈ 1,5 GB) em `~/.cache/huggingface` — automático, aguarde uma vez. Com o motor **Groq** (recomendado), nenhum download é necessário.

### Scripts de atalho

O projeto gera automaticamente:

- `livelingo.bat` — Windows
- `livelingo.sh` — Linux/WSL/macOS

---

## 3. Encontrar os índices dos dispositivos

```powershell
python list_devices.py
```

Lista todos os dispositivos de áudio com seu **índice**, marcando entradas (verde), saídas (magenta) e o VB-Cable. Exemplo:

```text
idx   in out  host API       name
  1    2   0  MME            Microphone (Realtek Audio)      <- default-in
  8    0   2  MME            CABLE Input (VB-Audio Virtual Cable)   <- VB-CABLE
 12    0   2  MME            Speakers (Realtek Audio)        <- default-out
```

Anote o índice do **seu microfone** e do **CABLE Input**.

---

## 4. Configuração

Os padrões (mic = padrão do sistema, saída = `CABLE Input`) costumam funcionar sem ajustes. Para personalizar, edite [`config.py`](config.py) ou copie o arquivo de exemplo:

```powershell
Copy-Item .env.example .env
notepad .env
```

### Configurações comuns

| Configuração | Padrão | Significado |
|--------------|--------|-------------|
| `SOURCE_LANG` | `fr` | Idioma que você fala |
| `TARGET_LANG` | `en` | Idioma que os outros ouvem |
| `STT_ENGINE` | `auto` | `auto`/`groq`/`local` — Groq cloud vs local |
| `GROQ_STT_MODEL` | `whisper-large-v3` | Modelo Groq STT (`whisper-large-v3-turbo` = mais rápido) |
| `STT_INITIAL_PROMPT` | *(vazio)* | Dica de nomes/vocabulário para melhorar reconhecimento |
| `WHISPER_MODEL` | `small` | Modelo local: `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` |
| `INPUT_DEVICE` | *(mic padrão)* | Índice ou substring do nome do microfone |
| `OUTPUT_DEVICE` | `CABLE Input` | Dispositivo VB-Cable de reprodução |
| `TTS_VOICE` | `en-US-AriaNeural` | Voz Edge (`edge-tts --list-voices`) |
| `CHUNK_DURATION` | `4.0` | Duração alvo/fixa do chunk (segundos) |
| `VAD_ENABLED` | `true` | Cortar nas pausas (true) vs chunks fixos (false) |
| `SILENCE_THRESHOLD` | `0.015` | Limiar de volume para detecção de fala |
| `MONITOR_PLAYBACK` | `false` | Também reproduzir a tradução nos seus alto-falantes |
| `MONITOR_DEVICE` | *(saída padrão)* | Dispositivo do monitor (índice/nome) |
| `TRANSLATION_ENGINE` | `auto` | `auto`/`llm`/`google` |
| `GROQ_API_KEY` | *(vazio)* | Key Groq gratuita → tradução muito melhor |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Modelo Groq (`llama-3.1-8b-instant` = mais rápido) |

### Melhor precisão na transcrição (recomendado, gratuito)

Se você fala mas saem *palavras erradas*, o modelo local `small` costuma ser o culpado. A melhor correção gratuita é usar o **Groq na nuvem** com `whisper-large-v3` — muito mais preciso (especialmente para fala não inglesa), rápido e descarrega a CPU.

1. Configure uma `GROQ_API_KEY` gratuita (mesma key da tradução).
2. Deixe `STT_ENGINE=auto` (padrão). Com a key presente, usa Groq automaticamente; sem ela, fica local. Na inicialização aparece `Speech-to-text ready (Groq cloud / whisper-large-v3)`.

Outros ajustes:

- **Ficar offline?** `STT_ENGINE=local` e suba o modelo: `WHISPER_MODEL=large-v3-turbo` ou `medium`.
- **Nomes/jargão errados?** `STT_INITIAL_PROMPT` com vocabulário esperado — influencia ambos os motores.

### Melhor qualidade de tradução (opcional, LLM gratuito)

Por padrão usa Google Translate. Para resultados **muito mais naturais** (o LLM corrige o STT imperfeito *e* traduz num passo), configure uma **key Groq gratuita**:

1. Acesse **https://console.groq.com/keys** → cadastre-se (sem cartão).
2. Crie uma key (começa com `gsk_…`) e copie.
3. Coloque no `.env`:
   ```
   GROQ_API_KEY=gsk_sua_key_aqui
   ```
4. Execute `python main.py` — verá `LLM translation ready (Groq / …)` e um self-test rápido.

> **Privacidade:** com STT Groq, o **áudio** é enviado para transcrição; com LLM, o **texto** reconhecido é enviado para tradução. Para manter áudio 100% local, `STT_ENGINE=local`. Sem key, STT roda local e só Google Translate é usado.

---

## 5. Executar

```powershell
python main.py
```

Ou use os atalhos: `livelingo.bat` (Windows) / `./livelingo.sh` (WSL/Linux).

### Fluxo de inicialização

1. Banner e seleção de sessão (nova, retomar ou deletar)
2. Detecção e confirmação dos dispositivos de áudio
3. Self-test dos motores STT e tradução
4. Menu de comandos e indicador de escuta

Exemplo de saída por chunk:

```text
[chunk 3] Heard: "bonjour tout le monde" -> Translated: "hello everyone"
          ⏱  STT 1.20s | translate 0.30s | TTS 0.55s | total 2.05s
```

Pare a qualquer momento com **Ctrl+C** ou o comando `q`.

### Usar como microfone no Microsoft Teams

1. Mantenha `main.py` rodando.
2. No Teams: **Configurações (⋯ / seu avatar) → Configurações → Dispositivos**.
3. Em **Microfone**, escolha **CABLE Output (VB-Audio Virtual Cable)**.
4. Fale francês → os participantes ouvem a tradução em inglês.

> O mesmo vale para Zoom, Discord, Google Meet (no navegador, escolha "CABLE Output" como mic), OBS, etc.

**Dica:** para também se ouvir, `MONITOR_PLAYBACK=true`, ou no Windows *Painel de Controle de Som → Gravação → CABLE Output → Propriedades → Escutar* ative "Escutar este dispositivo" e escolha seus fones.

---

## 6. Solução de problemas

**"VB-Cable was not found" / encerra imediatamente.**
Instale o VB-CABLE (seção 1) e **reinicie**. Rode `python list_devices.py` para confirmar "CABLE Input". Se renomeou, defina `OUTPUT_DEVICE` com o índice.

**Teams não capta áudio.**
Confirme que o microfone do Teams é **CABLE Output** (o *Output*), não CABLE Input. Verifique se `main.py` está gerando chunks (linhas de status aparecem).

**Palavras curtas cortadas / nunca envia chunk.**
Ajuste o VAD: diminua `SILENCE_THRESHOLD` (ex.: `0.008`) se o mic for fraco, ou encurte `SILENCE_DURATION`. Se ruído de fundo dispara chunks, aumente `SILENCE_THRESHOLD`. Ou `VAD_ENABLED=false` para chunks fixos de 4 s.

**Whisper alucina frases no silêncio** (legendas aleatórias).
Mantenha `WHISPER_VAD_FILTER=true` (padrão) e aumente um pouco `SILENCE_THRESHOLD`.

**Falo mas saem palavras erradas** (baixa precisão).
O modelo local `small` costuma ser a causa. Melhor correção: `GROQ_API_KEY` + `STT_ENGINE=auto`. Para offline: `WHISPER_MODEL=large-v3-turbo`.

**Muito lento / chunks acumulando** (`processing is N chunks behind`).
`STT_ENGINE=groq` para descarregar na nuvem, ou modelo menor: `WHISPER_MODEL=base` ou `tiny`. `WHISPER_BEAM_SIZE=1` para mais velocidade.

**`Could not decode TTS audio` / erro soundfile MP3.**
Instale `soundfile>=0.12.1` (`pip install -U soundfile`).

**TTS falha com `403, Invalid response status`.**
O endpoint Microsoft exige token `Sec-MS-GEC`. Corrija: `pip install --upgrade edge-tts` (7.x+). Verifique também o **relógio do sistema**.

**Erros de rede na tradução/TTS.**
deep-translator e edge-tts precisam de internet. Falhas transitórias pulam um chunk e o tool continua.

**Microfone errado capturado.**
`INPUT_DEVICE` com o índice correto de `list_devices.py`.

**Aceleração GPU (opcional).**
Com GPU NVIDIA + CUDA/cuDNN: `WHISPER_DEVICE=cuda` e `WHISPER_COMPUTE_TYPE=float16`.

---

## Estrutura do projeto

```text
.
├── main.py                # ponto de entrada — sessões, comandos, pipeline
├── config.py              # configurações (sobrescrevíveis via .env)
├── list_devices.py        # lista dispositivos de áudio + índices
├── requirements.txt       # dependências fixadas
├── .env.example           # copie para .env
├── livelingo.db           # banco SQLite (sessões, chunks, favoritos)
├── livelingo.bat          # atalho Windows
├── livelingo.sh           # atalho Linux/WSL/macOS
├── README.md              # documentação em inglês
├── README-ptbr.md         # esta documentação
└── livelingo/             # pacote modular do pipeline
    ├── capture.py         # mic → chunks de áudio (VAD ou fixo)
    ├── transcribe.py      # STT local faster-whisper
    ├── groq_transcribe.py # STT Groq cloud Whisper
    ├── translate.py       # tradução Google (deep-translator)
    ├── llm.py             # tradução LLM Groq + resumo + sinônimos
    ├── synthesize.py      # edge-tts TTS → áudio numpy
    ├── playback.py        # áudio → VB-Cable / monitor
    ├── pipeline.py        # orquestração threads + filas
    ├── devices.py         # descoberta de dispositivos
    ├── db.py              # persistência SQLite
    └── ui.py              # banner, cores, status no terminal
```

### Dependências

```text
faster-whisper, deep-translator, edge-tts, sounddevice, soundfile,
numpy, python-dotenv, colorama, requests
```

---

## Notas e limitações

- **Não é interpretação simultânea** — é tradução por chunks, com latência inerente (~1–4 s após terminar a frase).
- Qualidade de tradução e TTS depende dos serviços gratuitos Google/Edge/Groq.
- **Privacidade** depende dos motores: com `STT_ENGINE=local`, o áudio nunca sai da máquina (só o texto reconhecido e o pedido TTS). Com STT Groq, chunks de áudio vão para a nuvem.
- O histórico fica em `livelingo.db` e `.cache/audio_sessions/` — faça backup se precisar preservar reuniões.

---

## Resumo

O LiveLingo é adequado para **reuniões internacionais**, entrevistas, aulas ou qualquer cenário em que você precise falar num idioma e os outros ouvirem em outro — com registro persistente, edição pós-fala, favoritos e exportação com resumo executivo automático.

Para a documentação em inglês, consulte [`README.md`](README.md).