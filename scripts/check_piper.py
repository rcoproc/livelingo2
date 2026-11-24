#!/usr/bin/env python3
"""
Diagnose Piper TTS on the current Python interpreter.

Run with the SAME interpreter as LiveLingo:

  Windows:  py -3 scripts/check_piper.py
  Linux:    python3 scripts/check_piper.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

# Allow `py scripts/check_piper.py` from project root
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    print(f"Python: {sys.executable}")
    print(f"Version: {sys.version}")
    print(f"Project: {_ROOT}")
    print()

    ok = True
    try:
        import onnxruntime as ort

        print(f"onnxruntime: {ort.__version__}")
        print(f"  providers: {ort.get_available_providers()}")
    except Exception as exc:
        ok = False
        print(f"onnxruntime: FAIL ({type(exc).__name__}: {exc})")
        print("  Fix: py -m pip install onnxruntime")

    try:
        import piper
        from piper import PiperVoice

        print(f"piper: OK ({getattr(piper, '__file__', '?')})")
        print(f"  PiperVoice: {PiperVoice}")
    except Exception as exc:
        ok = False
        print(f"piper: FAIL ({type(exc).__name__}: {exc})")
        print("  Fix: py -m pip install piper-tts")

    print()
    try:
        import config as cfg
        from livelingo.piper_tts import PiperSynthesizer

        print(
            f"cfg: TTS_ENGINE={cfg.TTS_ENGINE!r} TTS_HYBRID={cfg.TTS_HYBRID!r} "
            f"PIPER_VOICE={cfg.PIPER_VOICE!r}"
        )
        synth = PiperSynthesizer(cfg, log=print)
        audio, sr = synth.synthesize("Hello from Piper.")
        n = 0 if audio is None else len(audio)
        print(f"synthesize OK: samples={n} sr={sr} voice={synth.voice_id}")
    except Exception:
        ok = False
        print("PiperSynthesizer FAILED:")
        traceback.print_exc()
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
