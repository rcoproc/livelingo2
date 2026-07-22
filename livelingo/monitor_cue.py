"""
Pre-TTS cue on headphones only — never VB-Cable / Teams mic path.

Separated from Player so the Cable OutputStream is never opened for the beep.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np


def make_double_beep(
    sample_rate: int = 24000,
    *,
    duration_s: float = 0.14,
    freq_hz: float = 880.0,
    amplitude: float = 0.22,
) -> np.ndarray:
    sr = max(8000, int(sample_rate or 24000))
    n = max(1, int(sr * float(duration_s)))
    t = np.arange(n, dtype=np.float32) / float(sr)
    a = max(1, int(0.01 * sr))
    env = np.ones(n, dtype=np.float32)
    env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    env[-a:] = np.linspace(1.0, 0.0, a, dtype=np.float32)
    amp = float(amplitude)
    f1 = float(freq_hz)
    tone = (amp * np.sin(2.0 * np.pi * f1 * t) * env).astype(np.float32)
    gap = np.zeros(max(1, int(0.05 * sr)), dtype=np.float32)
    tone2 = (amp * 0.85 * np.sin(2.0 * np.pi * (f1 * 1.25) * t) * env).astype(
        np.float32
    )
    return np.concatenate([tone, gap, tone2]).astype(np.float32)


def resolve_headphones(
    monitor_spec: str = "",
    *,
    cable_index=None,
) -> Tuple[Optional[int], str]:
    """
    Pick headphones/speakers for the cue.

    If ``monitor_spec`` is set (index or name), it is **mandatory** — we never
    fall back to another device (that bug sent the bip to device 1 instead of 13).

    Returns (index_or_None, human_name_or_error_reason).
    """
    from . import devices as dev

    spec = str(monitor_spec or "").strip()
    # Strip inline comments: MONITOR_DEVICE=13 # fone
    if "#" in spec:
        spec = spec.split("#", 1)[0].strip()

    # ---- Explicit MONITOR_DEVICE: honor exactly ----
    if spec:
        try:
            idx, name = dev.resolve_device(spec, "output")
        except Exception as exc:
            return None, f"MONITOR_DEVICE={spec!r} inválido: {exc}"
        if idx is None:
            return None, f"MONITOR_DEVICE={spec!r} resolveu para default vazio"
        if cable_index is not None and idx == cable_index:
            return None, (
                f"MONITOR_DEVICE=#{idx} '{name}' é o mesmo que OUTPUT/Cable — "
                "use o índice do fone (ex.: 13)"
            )
        if dev.is_cable_like_output(idx):
            return None, (
                f"MONITOR_DEVICE=#{idx} '{name}' parece Cable — "
                "bip iria pro Teams; escolha o fone"
            )
        return int(idx), name or dev.device_name(idx)

    # ---- Auto only when MONITOR_DEVICE empty ----
    prefer = (
        "headphone",
        "headset",
        "fone",
        "earphone",
        "earbuds",
        "realtek",
        "speaker",
        "high definition audio",
        "usb audio",
        "bluetooth",
    )
    avoid = (
        "cable input",
        "cable output",
        "vb-audio",
        "voicemeeter",
        "obs",
        "virtual",
        "mapper",
        "primary sound driver",
    )
    candidates = []
    try:
        for i, d in enumerate(dev.query_devices()):
            if int(d.get("max_output_channels") or 0) < 1:
                continue
            if cable_index is not None and i == cable_index:
                continue
            if dev.is_cable_like_output(i):
                continue
            name = (d.get("name") or "").lower()
            if any(a in name for a in avoid):
                continue
            score = 10
            for p in prefer:
                if p in name:
                    score += 20
            try:
                if i == dev.default_output_index():
                    score += 15
            except Exception:
                pass
            candidates.append((i, d.get("name") or f"#{i}", score))
    except Exception as exc:
        return None, f"list devices failed: {exc}"

    if not candidates:
        return None, "nenhum fone/speaker (não-Cable); defina MONITOR_DEVICE=13"

    candidates.sort(key=lambda x: -x[2])
    idx, name, _ = candidates[0]
    return int(idx), name


def play_cue_on_headphones(
    audio: np.ndarray,
    sample_rate: int,
    *,
    monitor_index=None,
    monitor_spec: str = "",
    cable_index=None,
    log=None,
) -> bool:
    """
    Play mono float32 on **one** output only (MONITOR_DEVICE / headphones).

    Never opens or writes to Cable / OUTPUT_DEVICE / system default.
    Prefer ``monitor_index`` (already resolved at startup) over re-parsing spec.
    """
    import sounddevice as sd

    from . import devices as dev

    if audio is None or getattr(audio, "size", 0) == 0:
        return False

    name = ""
    mon = None
    if monitor_index is not None:
        try:
            mon = int(monitor_index)
            name = dev.device_name(mon)
        except Exception as exc:
            if log:
                log(f"TTS cue SKIP: monitor_index={monitor_index!r} inválido: {exc}")
            return False
    else:
        mon, name = resolve_headphones(monitor_spec, cable_index=cable_index)
        if mon is None:
            if log:
                log(f"TTS cue SKIP: {name}")
            return False

    mon = int(mon)
    # Absolute hard stops — cue must never touch Cable / Teams mic path
    try:
        cab = int(cable_index) if cable_index is not None else None
    except (TypeError, ValueError):
        cab = cable_index
    if cab is not None and mon == cab:
        if log:
            log(f"TTS cue SKIP: monitor #{mon} == Cable #{cab}")
        return False
    if dev.is_cable_like_output(mon):
        if log:
            log(f"TTS cue SKIP: #{mon} '{name}' parece Cable — bip iria pro Teams")
        return False

    audio = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1, 1)
    sr = int(sample_rate or 24000)
    # Short bip only — callers should wall-clock sleep for lead, not pad silence
    # on a long stream (reduces dual-device / shared-mode glitches on Windows).
    try:
        if log:
            log(f"TTS cue → [{mon}] {name}  (SOMENTE monitor; NUNCA Cable/Teams)")
        # Explicit index only — never sd.default / None (would hit wrong device)
        with sd.OutputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            device=mon,
            blocksize=max(256, sr // 25),
        ) as stream:
            pos = 0
            n = int(audio.shape[0])
            bf = max(256, sr // 25)
            while pos < n:
                end = min(pos + bf, n)
                stream.write(audio[pos:end])
                pos = end
        return True
    except Exception as exc:
        if log:
            log(f"TTS cue FALHOU no device [{mon}] {name}: {exc}")
        return False
