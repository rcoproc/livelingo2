"""
playback.py
===========
Plays synthesized audio to an output device — normally the VB-Cable playback
side ("CABLE Input"), which other apps then read as a microphone.

Uses a dedicated, persistent OutputStream per target device so we don't pay the
open/close cost on every chunk. PortAudio (MME on Windows) handles sample-rate
conversion if the device runs at a different rate than the TTS audio.
"""

import sounddevice as sd


class Player:
    def __init__(self, device_index, samplerate, monitor_device=None):
        """
        device_index   : main output (VB-Cable) device index, or None=default.
        samplerate     : sample rate of the audio that will be played.
        monitor_device : optional second device (e.g. your speakers) to also
                         play through; None=disabled, sd.default if you want it.
        """
        self.device = device_index
        self.samplerate = samplerate
        self.monitor_device = monitor_device

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

    def play(self, audio, samplerate):
        """Write one audio array (blocking until fully written)."""
        if audio is None or len(audio) == 0:
            return
        # If the chunk's rate differs from the stream's, fall back to a one-shot
        # play so PortAudio resamples correctly for this chunk.
        if samplerate != self.samplerate:
            sd.play(audio, samplerate=samplerate, device=self.device)
            sd.wait()
            if self.monitor_device is not None:
                sd.play(audio, samplerate=samplerate, device=self.monitor_device)
                sd.wait()
            return

        self._stream.write(audio)
        if self._monitor_stream is not None:
            self._monitor_stream.write(audio)

    def close(self):
        for stream in (self._stream, self._monitor_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
