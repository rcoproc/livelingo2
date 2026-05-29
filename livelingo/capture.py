"""
capture.py
==========
Microphone capture. Reads 16 kHz mono float32 audio from the selected input
device and emits "chunks" (numpy arrays) onto a queue for the processing stage.

Two modes (config.VAD_ENABLED):
  * VAD mode (default): an energy-based voice-activity detector groups audio
    into natural utterances — a chunk is emitted once you pause talking, or
    when MAX_CHUNK_DURATION is reached.
  * Fixed mode: a chunk is emitted every CHUNK_DURATION seconds.

The recorder runs in its own thread and stops cleanly when `stop_event` is set.
"""

import math
import queue
from collections import deque

import numpy as np
import sounddevice as sd


def _rms(block):
    """Root-mean-square energy of a float32 block (proxy for loudness)."""
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


class Recorder:
    def __init__(self, config, device_index, chunk_queue, stop_event, on_listening=None):
        self.cfg = config
        self.device = device_index
        self.chunk_queue = chunk_queue
        self.stop_event = stop_event
        self.on_listening = on_listening  # optional callback(bool is_speaking)

        self.sample_rate = config.SAMPLE_RATE
        self.block_frames = max(1, int(config.SAMPLE_RATE * config.BLOCK_DURATION))
        self.preroll_blocks = max(1, int(config.PREROLL_DURATION / config.BLOCK_DURATION))
        self.silence_blocks_needed = max(
            1, int(config.SILENCE_DURATION / config.BLOCK_DURATION)
        )
        self.min_speech_frames = int(config.MIN_SPEECH_DURATION * config.SAMPLE_RATE)
        self.max_chunk_frames = int(config.MAX_CHUNK_DURATION * config.SAMPLE_RATE)
        self.fixed_chunk_frames = int(config.CHUNK_DURATION * config.SAMPLE_RATE)

    # ------------------------------------------------------------------ #
    def run(self):
        """Blocking capture loop; intended to run in a dedicated thread."""
        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.cfg.CHANNELS,
                dtype="float32",
                blocksize=self.block_frames,
                device=self.device,
            ) as stream:
                if self.cfg.VAD_ENABLED:
                    self._run_vad(stream)
                else:
                    self._run_fixed(stream)
        except Exception as exc:  # surface device errors to the main thread
            self.chunk_queue.put(_CaptureError(exc))

    # ------------------------------------------------------------------ #
    def _read_block(self, stream):
        """Read one analysis block as a 1-D float32 array (mono)."""
        data, _overflowed = stream.read(self.block_frames)
        # data shape: (frames, channels) -> flatten to mono.
        if data.ndim > 1:
            data = data[:, 0]
        return np.ascontiguousarray(data, dtype=np.float32)

    def _emit(self, blocks):
        """Concatenate blocks and push the chunk if it is long enough."""
        if not blocks:
            return
        chunk = np.concatenate(blocks)
        if chunk.size >= self.min_speech_frames:
            self.chunk_queue.put(chunk)

    # ------------------------------------------------------------------ #
    def _run_vad(self, stream):
        """Energy-based VAD: group audio into utterances separated by silence."""
        preroll = deque(maxlen=self.preroll_blocks)
        speech = []            # accumulated blocks of the current utterance
        in_speech = False
        trailing_silence = 0   # consecutive silent blocks while in speech
        total_frames = 0

        while not self.stop_event.is_set():
            block = self._read_block(stream)
            loud = _rms(block) > self.cfg.SILENCE_THRESHOLD

            if not in_speech:
                preroll.append(block)
                if loud:
                    # Speech just started: seed the utterance with the preroll
                    # so the first syllable isn't clipped.
                    in_speech = True
                    speech = list(preroll)
                    preroll.clear()
                    total_frames = sum(b.size for b in speech)
                    trailing_silence = 0
                    if self.on_listening:
                        self.on_listening(True)
                continue

            # Already inside an utterance.
            speech.append(block)
            total_frames += block.size
            trailing_silence = 0 if loud else trailing_silence + 1

            ended = trailing_silence >= self.silence_blocks_needed
            too_long = total_frames >= self.max_chunk_frames

            if ended or too_long:
                self._emit(speech)
                speech = []
                in_speech = False
                trailing_silence = 0
                total_frames = 0
                if self.on_listening:
                    self.on_listening(False)

        # Flush whatever is buffered when stopping.
        self._emit(speech)

    # ------------------------------------------------------------------ #
    def _run_fixed(self, stream):
        """Fixed-length chunking: emit every CHUNK_DURATION seconds."""
        buffer = []
        frames = 0
        while not self.stop_event.is_set():
            block = self._read_block(stream)
            buffer.append(block)
            frames += block.size
            if frames >= self.fixed_chunk_frames:
                self._emit(buffer)
                buffer = []
                frames = 0
        self._emit(buffer)


class _CaptureError:
    """Sentinel wrapper used to forward a capture-thread exception via the queue."""

    def __init__(self, exc):
        self.exc = exc


def is_capture_error(item):
    return isinstance(item, _CaptureError)
