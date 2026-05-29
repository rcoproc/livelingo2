"""
pipeline.py
===========
Orchestrates the full pipeline with three threads connected by queues:

    [recorder thread]  mic -> chunk_queue
    [processor thread] chunk_queue -> STT -> translate -> TTS -> playback_queue
    [playback thread]  playback_queue -> VB-Cable output device

All threads are daemons and watch a shared `stop_event` for clean shutdown.
"""

import queue
import threading
import time

from . import ui
from .capture import Recorder, is_capture_error
from .playback import Player


class Pipeline:
    def __init__(
        self,
        config,
        input_device,
        output_device,
        transcriber,
        translator,
        synthesizer,
        monitor_device=None,
    ):
        self.cfg = config
        self.input_device = input_device
        self.output_device = output_device
        self.monitor_device = monitor_device

        self.transcriber = transcriber
        self.translator = translator
        self.synthesizer = synthesizer

        self.chunk_queue = queue.Queue()
        self.playback_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.recorder = Recorder(
            config, input_device, self.chunk_queue, self.stop_event
        )

        self._chunk_count = 0
        self._threads = []

    # ------------------------------------------------------------------ #
    def start(self):
        self._threads = [
            threading.Thread(target=self.recorder.run, name="recorder", daemon=True),
            threading.Thread(target=self._process_loop, name="processor", daemon=True),
            threading.Thread(target=self._playback_loop, name="playback", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self.stop_event.set()

    def join(self, timeout=5.0):
        deadline = time.time() + timeout
        for thread in self._threads:
            remaining = max(0.0, deadline - time.time())
            thread.join(timeout=remaining)

    # ------------------------------------------------------------------ #
    def _process_loop(self):
        """STT -> translate -> TTS for each captured chunk."""
        while not self.stop_event.is_set():
            try:
                item = self.chunk_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            # The recorder forwards device errors through the queue.
            if is_capture_error(item):
                ui.error(f"Audio capture failed: {item.exc}")
                self.stop_event.set()
                break

            self._handle_chunk(item)

    def _handle_chunk(self, audio):
        self._chunk_count += 1
        n = self._chunk_count

        backlog = self.chunk_queue.qsize()
        if backlog >= 3:
            ui.warn(
                f"processing is {backlog} chunks behind — "
                f"a smaller WHISPER_MODEL would keep up better."
            )

        # --- Speech-to-text ---
        t0 = time.perf_counter()
        try:
            heard = self.transcriber.transcribe(audio)
        except Exception as exc:
            ui.error(f"[chunk {n}] STT failed: {exc}")
            return
        t1 = time.perf_counter()

        if not heard:
            ui.dim(f"[chunk {n}] (no speech detected — skipped)")
            return

        # --- Translation ---
        try:
            translated = self.translator.translate(heard)
        except Exception as exc:
            ui.error(f'[chunk {n}] translation failed for "{heard}": {exc}')
            return
        t2 = time.perf_counter()

        if not translated:
            ui.dim(f"[chunk {n}] (empty translation — skipped)")
            return

        # --- Text-to-speech ---
        try:
            tts_audio, sample_rate = self.synthesizer.synthesize(translated)
        except Exception as exc:
            ui.error(f"[chunk {n}] TTS failed: {exc}")
            return
        t3 = time.perf_counter()

        ui.chunk_status(
            n,
            heard,
            translated,
            {
                "stt": t1 - t0,
                "translate": t2 - t1,
                "tts": t3 - t2,
                "total": t3 - t0,
            },
        )

        if tts_audio is not None:
            self.playback_queue.put((tts_audio, sample_rate))

    # ------------------------------------------------------------------ #
    def _playback_loop(self):
        """Send synthesized audio to the VB-Cable output device."""
        player = None
        try:
            while not self.stop_event.is_set():
                try:
                    audio, sample_rate = self.playback_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                # Create the output stream lazily, once we know the TTS rate.
                if player is None:
                    try:
                        player = Player(
                            self.output_device, sample_rate, self.monitor_device
                        )
                    except Exception as exc:
                        ui.error(
                            f"Could not open output device "
                            f"#{self.output_device}: {exc}"
                        )
                        self.stop_event.set()
                        break
                try:
                    player.play(audio, sample_rate)
                except Exception as exc:
                    ui.error(f"playback failed: {exc}")
        finally:
            if player is not None:
                player.close()
