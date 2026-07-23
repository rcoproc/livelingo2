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
    def __init__(
        self,
        device_index,
        samplerate,
        monitor_device=None,
        block_ms=80,
        monitor_full_playback=False,
    ):
        """
        device_index   : main output (VB-Cable) device index, or None=default.
        samplerate     : sample rate of the audio that will be played.
        monitor_device : optional second device (e.g. headphones).
        block_ms       : write block size for responsive interrupt handling.
        monitor_full_playback : if True, TTS also plays on monitor (MONITOR_PLAYBACK).
            If False (default), Cable only — pre-TTS cue uses monitor_cue module
            and must never open this class's Cable stream for the beep.
        """
        self.device = device_index
        self.samplerate = samplerate
        self.monitor_device = monitor_device
        self.monitor_full_playback = bool(monitor_full_playback)
        self.block_frames = max(1, int(samplerate * block_ms / 1000))
        self._interrupt = threading.Event()
        # Serializes stream abort/restart vs write (not the full play duration).
        self._stream_lock = threading.Lock()

        self._stream = sd.OutputStream(
            samplerate=samplerate, channels=1, dtype="float32", device=device_index
        )
        self._stream.start()

        self._monitor_stream = None
        # Open monitor only when full TTS mirror is requested (MONITOR_PLAYBACK).
        # Pre-TTS cue is a separate OutputStream on headphones — never via Cable.
        if monitor_device is not None and self.monitor_full_playback:
            if monitor_device != device_index:
                try:
                    from .devices import is_cable_like_output

                    if is_cable_like_output(monitor_device):
                        monitor_device = None
                except Exception:
                    pass
            if monitor_device is not None and monitor_device != device_index:
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
                if self.monitor_full_playback and self.monitor_device is not None:
                    if self.monitor_device != self.device:
                        sd.play(
                            audio, samplerate=samplerate, device=self.monitor_device
                        )
                        sd.wait()
            else:
                sd.play(
                    audio, samplerate=samplerate, device=self.device, blocking=False
                )
                if self.monitor_full_playback and self.monitor_device is not None:
                    if self.monitor_device != self.device:
                        sd.play(
                            audio,
                            samplerate=samplerate,
                            device=self.monitor_device,
                            blocking=False,
                        )
            return

        # Pre-TTS cue calls pause_main_output() → stream.abort(). Ensure live
        # before write; otherwise play() returns instantly and escuta re-arms
        # while the user still hears nothing / mid-glitch.
        try:
            self.resume_main_output()
        except Exception:
            pass
        self._write_stream(self._stream, audio)
        # Full TTS on headphones only when MONITOR_PLAYBACK (not cue-only)
        if (
            self.monitor_full_playback
            and not self._interrupt.is_set()
            and self._monitor_stream is not None
            and self.monitor_device is not None
            and self.monitor_device != self.device
        ):
            self._write_stream(self._monitor_stream, audio)

    def pause_main_output(self):
        """
        Silence Cable/Teams path (abort stream) without setting interrupt flag.

        Used while the pre-TTS cue plays on headphones so any shared-mode
        glitch cannot leak the bip into CABLE Input.
        """
        with self._stream_lock:
            stream = self._stream
            if stream is None:
                return
            try:
                stream.abort()
            except Exception:
                pass

    def resume_main_output(self):
        """Re-start Cable stream after pause_main_output() (pre-TTS cue done)."""
        with self._stream_lock:
            stream = self._stream
            if stream is None:
                return
            try:
                if not stream.active:
                    stream.start()
            except Exception:
                pass

    def play_monitor_only(self, audio, samplerate, clear=False):
        """
        Play audio on the monitor device only (headphones) — never Cable/Teams.

        HARD RULE: never writes to ``self._stream`` (Cable Input → Teams mic).
        Prefer ``monitor_cue.play_cue_on_headphones`` for the pre-TTS bip.
        """
        if audio is None or len(audio) == 0:
            return
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if clear:
            self._interrupt.clear()
        elif self._interrupt.is_set():
            return

        mon = self.monitor_device
        # Refuse Cable / same device as main TTS output
        if mon is None or mon == self.device:
            return
        try:
            from .devices import is_cable_like_output

            if is_cable_like_output(mon):
                return
        except Exception:
            pass

        # Dedicated one-shot stream — never touch Cable stream; no sd.play
        # fallback without device (would hit default / dual routing).
        try:
            sr = int(samplerate or self.samplerate)
            with sd.OutputStream(
                samplerate=sr,
                channels=1,
                dtype="float32",
                device=int(mon),
            ) as stream:
                pos = 0
                bf = max(1, int(sr * 0.04))
                while pos < len(audio):
                    if self._interrupt.is_set():
                        return
                    end = min(pos + bf, len(audio))
                    stream.write(audio[pos:end])
                    pos = end
        except Exception:
            pass

    def ensure_monitor_stream(self):
        """Open monitor stream if missing (full MONITOR_PLAYBACK mirror only)."""
        if not self.monitor_full_playback:
            return
        if self._monitor_stream is not None or self.monitor_device is None:
            return
        if self.monitor_device == self.device:
            return
        try:
            from .devices import is_cable_like_output

            if is_cable_like_output(self.monitor_device):
                return
        except Exception:
            pass
        try:
            self._monitor_stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=1,
                dtype="float32",
                device=self.monitor_device,
            )
            self._monitor_stream.start()
        except Exception:
            self._monitor_stream = None

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
