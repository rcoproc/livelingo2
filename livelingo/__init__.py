"""
livelingo
=========
Modular building blocks for the LiveLingo real-time voice translation pipeline:

    capture.py      microphone -> audio chunks (with optional VAD)
    transcribe.py   audio chunk -> source text (faster-whisper)
    translate.py    source text  -> target text (deep-translator / Google)
    llm.py          source text  -> target text (Groq LLM, higher quality)
    synthesize.py   target text  -> audio       (edge-tts)
    playback.py     audio        -> VB-Cable output device
    pipeline.py     wires the stages together with threads + queues
    devices.py      audio-device discovery / resolution helpers
    ui.py           terminal banner, colors and status lines
"""

__version__ = "1.0.0"
