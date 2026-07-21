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
        """Stop the current playback as soon as possible."""
        self._interrupt.set()

    def clear_interrupt(self):
        """Allow the next play() call to output audio again."""
        self._interrupt.clear()

    def _write_stream(self, stream, audio):
        if stream is None:
            return
        pos = 0
        while pos < len(audio):
            if self._interrupt.is_set():
                return
            end = min(pos + self.block_frames, len(audio))
            stream.write(audio[pos:end])
            pos = end

    def play(self, audio, samplerate, interruptible=True, blocking=True):
        """Write one audio array in small blocks (interruptible)."""
        if audio is None or len(audio) == 0:
            return

        audio = np.ascontiguousarray(audio, dtype=np.float32)
        self._interrupt.clear()

        if samplerate != self.samplerate:
            if blocking:
                sd.play(audio, samplerate=samplerate, device=self.device)
                sd.wait()
                if self.monitor_device is not None:
                    sd.play(audio, samplerate=samplerate, device=self.monitor_device)
                    sd.wait()
            else:
                sd.play(audio, samplerate=samplerate, device=self.device, blocking=False)
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
        for stream in (self._stream, self._monitor_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass