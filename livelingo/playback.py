"""
playback.py
===========
Plays synthesized audio to an output device — normally the VB-Cable playback
side ("CABLE Input"), which other apps then read as a microphone.

Uses a dedicated, persistent OutputStream per target device so we don't pay the
open/close cost on every chunk. PortAudio (MME on Windows) handles sample-rate
conversion if the device runs at a different rate than the TTS audio.
"""

import threading

import numpy as np
import sounddevice as sd

INTERRUPT = object()


class Player:
    def __init__(self, device_index, samplerate, monitor_device=None, block_ms=80):
        """
        device_index   : main output (VB-Cable) device index, or None=default.
        samplerate     : sample rate of the audio that will be played.
        monitor_device : optional second device (e.g. your speakers) to also
                         play through; None=disabled, sd.default if you want it.
        block_ms       : write block size for responsive interrupt handling.
        """
        self.device = device_index
        self.samplerate = samplerate
        self.monitor_device = monitor_device
        self.block_frames = max(1, int(samplerate * block_ms / 1000))
        self._interrupt = threading.Event()
        # Serializes stream abort/restart vs write (not the full play duration).
        self._stream_lock = threading.Lock()

        self._stream = sd.OutputStream(
            samplerate=samplerate, channels=1, dtype="float32", device=device_index
        )
        self._stream.start()

        self._monitor_stream = None
        if monitor_device is not None:
            self._monitor_stream = sd.OutputStream(
                samplerate=samplerate,
                channels=1,
                dtype="float32",
                device=monitor_device,
            )
            self._monitor_stream.start()

    def interrupt(self):
        """Stop the current playback as soon as possible (thread-safe)."""
        self._interrupt.set()
        # Drop PortAudio buffer so silence is immediate, not ~block_ms delayed.
        with self._stream_lock:
            for stream in (self._stream, self._monitor_stream):
                if stream is None:
                    continue
                try:
                    stream.abort()
                except Exception:
                    pass
                try:
                    if not stream.active:
                        stream.start()
                except Exception:
                    pass
        # Also cancel any sd.play() fallback (sample-rate mismatch path).
        try:
            sd.stop()
        except Exception:
            pass

    def clear_interrupt(self):
        """Allow the next play() call to output audio again."""
        self._interrupt.clear()

    def is_interrupted(self):
        return self._interrupt.is_set()

    def _write_stream(self, stream, audio):
        if stream is None:
            return
        pos = 0
        while pos < len(audio):
            if self._interrupt.is_set():
                return
            end = min(pos + self.block_frames, len(audio))
            chunk = audio[pos:end]
            try:
                with self._stream_lock:
                    if self._interrupt.is_set():
                        return
                    if stream is None or not stream.active:
                        return
                    stream.write(chunk)
            except Exception:
                # abort() mid-write raises; treat as stop if interrupted.
                if self._interrupt.is_set():
                    return
                raise
            pos = end

    def play(self, audio, samplerate, interruptible=True, blocking=True, clear=True):
        """Write one audio array in small blocks (interruptible).

        clear=True (default) resets the interrupt flag at start. Callers that
        already cleared (and re-checked a stop flag) should pass clear=False
        so a concurrent stop is not accidentally undone.
        """
        if audio is None or len(audio) == 0:
            return

        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if clear:
            self._interrupt.clear()
        elif self._interrupt.is_set():
            return

        if samplerate != self.samplerate:
            # Fallback path: less granular, but interrupt() calls sd.stop().
            if blocking:
                sd.play(audio, samplerate=samplerate, device=self.device)
                sd.wait()
                if self._interrupt.is_set():
                    return
                if self.monitor_device is not None:
                    sd.play(audio, samplerate=samplerate, device=self.monitor_device)
                    sd.wait()
            else:
                sd.play(
                    audio, samplerate=samplerate, device=self.device, blocking=False
                )
                if self.monitor_device is not None:
                    sd.play(
                        audio,
                        samplerate=samplerate,
                        device=self.monitor_device,
                        blocking=False,
                    )
            return

        self._write_stream(self._stream, audio)
        if not self._interrupt.is_set():
            self._write_stream(self._monitor_stream, audio)

    def close(self):
        self._interrupt.set()
        with self._stream_lock:
            for stream in (self._stream, self._monitor_stream):
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
            self._stream = None
            self._monitor_stream = None
