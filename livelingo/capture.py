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
    def __init__(
        self,
        config,
        device_index,
        chunk_queue,
        stop_event,
        on_listening=None,
        paragraph_split_enabled=None,
        shorter_end_enabled=None,
    ):
        self.cfg = config
        self.device = device_index
        self.chunk_queue = chunk_queue
        self.stop_event = stop_event
        self.on_listening = on_listening  # optional callback(bool is_speaking)
        self.paragraph_split_enabled = paragraph_split_enabled
        self.shorter_end_enabled = shorter_end_enabled

        self.sample_rate = config.SAMPLE_RATE
        self.block_frames = max(1, int(config.SAMPLE_RATE * config.BLOCK_DURATION))
        self.preroll_blocks = max(1, int(config.PREROLL_DURATION / config.BLOCK_DURATION))
        self.silence_blocks_needed = max(
            1, int(config.SILENCE_DURATION / config.BLOCK_DURATION)
        )
        self.min_speech_frames = int(config.MIN_SPEECH_DURATION * config.SAMPLE_RATE)
        self.max_chunk_frames = int(config.MAX_CHUNK_DURATION * config.SAMPLE_RATE)
        self.fixed_chunk_frames = int(config.CHUNK_DURATION * config.SAMPLE_RATE)
        self.rolling_chunk_frames = int(
            getattr(config, "ROLLING_CHUNK_DURATION", 2.5) * config.SAMPLE_RATE
        )
        self.rolling_overlap_blocks = max(
            1, int(0.15 / config.BLOCK_DURATION)
        )
        self.split_overlap_blocks = max(
            1, int(getattr(config, "VAD_SPLIT_OVERLAP", 1.5) / config.BLOCK_DURATION)
        )
        self.paragraph_silence_blocks = max(
            1, int(getattr(config, "PARAGRAPH_SILENCE", 1.0) / config.BLOCK_DURATION)
        )
        self.paragraph_min_frames = int(
            getattr(config, "PARAGRAPH_MIN_SPEECH", 5.0) * config.SAMPLE_RATE
        )
        self.paragraph_overlap_blocks = max(
            1,
            int(getattr(config, "PARAGRAPH_SPLIT_OVERLAP", 0.3) / config.BLOCK_DURATION),
        )
        self._silero = None
        if getattr(config, "VAD_MODE", "energy") == "silero":
            try:
                from .vad_silero import SileroVAD

                self._silero = SileroVAD(
                    sample_rate=config.SAMPLE_RATE,
                    threshold=getattr(config, "SILERO_VAD_THRESHOLD", 0.45),
                )
            except Exception as exc:
                import warnings

                warnings.warn(
                    f"Silero VAD unavailable ({exc}); falling back to energy VAD."
                )

    def _block_is_speech(self, block, in_speech=False):
        if self._silero is not None:
            return self._silero.is_speech(block)
        threshold = self.cfg.SILENCE_THRESHOLD
        if in_speech:
            # Hysteresis: stay in "speech" through brief dips between words/sentences.
            hangover = getattr(self.cfg, "VAD_SPEECH_HANGOVER", 0.65)
            threshold *= hangover
        return _rms(block) > threshold

    def _silence_blocks_to_end(self, total_frames):
        """Require longer pauses before ending a chunk during long monologues."""
        blocks = self.silence_blocks_needed
        if getattr(self.cfg, "VAD_ADAPTIVE_SILENCE", True):
            speech_sec = total_frames / self.sample_rate
            if speech_sec >= 4.0:
                scale_max = getattr(self.cfg, "VAD_SILENCE_SCALE_MAX", 3.5)
                factor = min(scale_max, 1.0 + speech_sec / 10.0)
                blocks = max(blocks, int(self.silence_blocks_needed * factor))
        if self.shorter_end_enabled is not None and self.shorter_end_enabled():
            cap = max(
                1,
                int(
                    getattr(self.cfg, "SOUND_OFF_SILENCE_DURATION", 2.0)
                    / self.cfg.BLOCK_DURATION
                ),
            )
            blocks = min(blocks, cap)
        return blocks

    def _paragraph_split_active(self):
        if not getattr(self.cfg, "PARAGRAPH_SPLIT", True):
            return False
        if self.paragraph_split_enabled is not None:
            return bool(self.paragraph_split_enabled())
        return not getattr(self.cfg, "PARAGRAPH_SPLIT_SOUND_OFF_ONLY", True)

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
        if chunk.size < self.min_speech_frames:
            return
        # Drop near-silent tails left after paragraph splits (Whisper hallucinates on these).
        if getattr(self.cfg, "STT_HALLUCINATION_FILTER", True):
            tail_max_sec = getattr(self.cfg, "CAPTURE_TAIL_MAX_SEC", 2.0)
            tail_max_frames = int(tail_max_sec * self.sample_rate)
            min_rms = getattr(self.cfg, "STT_MIN_RMS", 0.010)
            if chunk.size <= tail_max_frames and _rms(chunk) < min_rms:
                return
        self.chunk_queue.put(chunk)

    # ------------------------------------------------------------------ #
    def _run_vad(self, stream):
        """VAD: group audio into utterances separated by silence."""
        preroll = deque(maxlen=self.preroll_blocks)
        speech = []
        in_speech = False
        trailing_silence = 0
        total_frames = 0
        rolling_enabled = getattr(self.cfg, "ROLLING_CHUNKS", False)

        while not self.stop_event.is_set():
            block = self._read_block(stream)
            loud = self._block_is_speech(block, in_speech=in_speech)

            if not in_speech:
                preroll.append(block)
                if loud:
                    in_speech = True
                    speech = list(preroll)
                    preroll.clear()
                    total_frames = sum(b.size for b in speech)
                    trailing_silence = 0
                    if self.on_listening:
                        self.on_listening(True)
                continue

            speech.append(block)
            total_frames += block.size
            trailing_silence = 0 if loud else trailing_silence + 1

            end_threshold = self._silence_blocks_to_end(total_frames)
            ended = trailing_silence >= end_threshold
            too_long = total_frames >= self.max_chunk_frames
            rolling = (
                rolling_enabled
                and total_frames >= self.rolling_chunk_frames
            )
            paragraph_split = (
                self._paragraph_split_active()
                and not ended
                and not too_long
                and total_frames >= self.paragraph_min_frames
                and trailing_silence >= self.paragraph_silence_blocks
            )

            if rolling or ended or too_long or paragraph_split:
                self._emit(speech)
                if ended:
                    speech = []
                    in_speech = False
                    trailing_silence = 0
                    total_frames = 0
                    if self._silero is not None:
                        self._silero.reset()
                    if self.on_listening:
                        self.on_listening(False)
                elif paragraph_split:
                    overlap_blocks = max(
                        self.preroll_blocks, self.paragraph_overlap_blocks
                    )
                    speech = speech[-overlap_blocks:]
                    total_frames = sum(b.size for b in speech)
                    trailing_silence = 0
                elif too_long:
                    # Max duration reached — split for STT but keep listening
                    # (do not require loudness; brief dips were ending monologues early).
                    overlap_blocks = max(self.preroll_blocks, self.split_overlap_blocks)
                    speech = speech[-overlap_blocks:]
                    total_frames = sum(b.size for b in speech)
                    trailing_silence = 0
                elif rolling:
                    overlap = speech[-self.rolling_overlap_blocks :]
                    speech = list(overlap)
                    total_frames = sum(b.size for b in speech)
                    trailing_silence = 0

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
