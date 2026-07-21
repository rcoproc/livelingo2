"""
piper_tts.py
============
Local text-to-speech via Piper (ONNX). No internet required after model download.

Install:  pip install piper-tts onnxruntime
"""

import json
import os
import platform
import subprocess
import sys
import threading
import urllib.request

import numpy as np


def _default_ort_threads():
    if platform.system() == "Windows":
        return 4
    return 0


def _configure_onnx_threads(num_threads):
    """Tune ONNX Runtime CPU threads before Piper loads the model."""
    if num_threads <= 0:
        return
    os.environ.setdefault("OMP_NUM_THREADS", str(num_threads))
    os.environ.setdefault("ORT_NUM_THREADS", str(num_threads))


def _resolve_onnx_providers(config):
    import onnxruntime as ort

    setting = getattr(config, "PIPER_ONNX_PROVIDER", "auto").lower()
    available = set(ort.get_available_providers())

    if setting == "cuda" and "CUDAExecutionProvider" in available:
        return [
            ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}),
            "CPUExecutionProvider",
        ]
    if setting == "dml" and "DmlExecutionProvider" in available:
        return ["DmlExecutionProvider", "CPUExecutionProvider"]
    if setting == "auto":
        if "DmlExecutionProvider" in available:
            return ["DmlExecutionProvider", "CPUExecutionProvider"]
        if "CUDAExecutionProvider" in available:
            return [
                ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}),
                "CPUExecutionProvider",
            ]
    return ["CPUExecutionProvider"]


def _load_piper_voice(model_path, config, log=print):
    """Load Piper with tuned ONNX Runtime session options."""
    import onnxruntime as ort
    from piper import PiperVoice
    from piper.config import PiperConfig

    json_path = f"{model_path}.json"
    with open(json_path, encoding="utf-8") as config_file:
        config_dict = json.load(config_file)

    ort_threads = getattr(config, "PIPER_ORT_THREADS", 0)
    if ort_threads <= 0:
        ort_threads = _default_ort_threads()
    if ort_threads > 0:
        _configure_onnx_threads(ort_threads)

    sess_options = ort.SessionOptions()
    if ort_threads > 0:
        sess_options.intra_op_num_threads = ort_threads
        sess_options.inter_op_num_threads = 1
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    providers = _resolve_onnx_providers(config)
    provider_names = [p[0] if isinstance(p, tuple) else p for p in providers]
    log(f"Piper ONNX providers: {', '.join(provider_names)}")

    session = ort.InferenceSession(
        str(model_path),
        sess_options=sess_options,
        providers=providers,
    )
    return PiperVoice(config=PiperConfig.from_dict(config_dict), session=session)


from .piper_voices import (
    HF_BASE,
    PIPER_VOICE_PATHS,
    default_voice_for_lang,
    fast_voice_for,
)
from .synthesis_error import SynthesisError
from .tts_segments import split_piper_segments


def _ensure_model(voice_id, model_dir, log=print):
    """Download .onnx + .onnx.json if missing."""
    os.makedirs(model_dir, exist_ok=True)
    onnx_path = os.path.join(model_dir, f"{voice_id}.onnx")
    json_path = os.path.join(model_dir, f"{voice_id}.onnx.json")

    if os.path.exists(onnx_path) and os.path.exists(json_path):
        return onnx_path

    hf_path = PIPER_VOICE_PATHS.get(voice_id)
    if hf_path:
        log(f"Downloading Piper voice '{voice_id}' (one-time)...")
        for ext in (".onnx", ".onnx.json"):
            dest = os.path.join(model_dir, f"{voice_id}{ext}")
            url = f"{HF_BASE}/{hf_path}{ext}"
            urllib.request.urlretrieve(url, dest)
        if os.path.exists(onnx_path):
            return onnx_path

    log(f"Trying piper.download_voices for '{voice_id}'...")
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "piper.download_voices",
                voice_id,
                "--download-dir",
                model_dir,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SynthesisError(
            f"Could not download Piper voice '{voice_id}'. "
            f"Set PIPER_VOICE to a known voice or download manually into {model_dir}. "
            f"Details: {exc.stderr or exc}"
        ) from exc

    if not os.path.exists(onnx_path):
        raise SynthesisError(
            f"Piper voice '{voice_id}' not found in {model_dir} after download."
        )
    return onnx_path


class PiperSynthesizer:
    """Local Piper TTS — streams ONNX chunks for low time-to-first-audio."""

    supports_live_streaming = True

    def __init__(self, config, log=print):
        self.cfg = config
        self.log = log
        self.voice_id = getattr(
            config, "PIPER_VOICE", ""
        ).strip() or default_voice_for_lang(config.TARGET_LANG)
        if getattr(config, "PIPER_QUALITY", "medium").lower() == "fast":
            self.voice_id = fast_voice_for(self.voice_id)
        self.model_dir = getattr(config, "PIPER_MODEL_DIR", ".cache/models/piper")
        self.length_scale = getattr(config, "PIPER_LENGTH_SCALE", 1.0)
        self.chunk_streaming = getattr(config, "PIPER_CHUNK_STREAMING", True)
        self.playback_buffer_ms = getattr(config, "PIPER_PLAYBACK_BUFFER_MS", 60)
        self.segment_min_chars = min(
            getattr(config, "PIPER_SEGMENT_MIN_CHARS", 70), 120
        )
        try:
            from piper.config import SynthesisConfig
        except ImportError as exc:
            raise SynthesisError(
                "Piper TTS requires: pip install piper-tts onnxruntime"
            ) from exc

        self._SynthesisConfig = SynthesisConfig
        self._synth_lock = threading.Lock()
        model_path = _ensure_model(self.voice_id, self.model_dir, log=log)
        self._voice = _load_piper_voice(model_path, config, log=log)
        self._syn_config = SynthesisConfig(length_scale=self.length_scale)
        self._sample_rate = None

        try:
            next(self._voice.synthesize(".", syn_config=self._syn_config), None)
        except Exception:
            pass

        log(
            f"Text-to-speech: Piper local ({self.voice_id}, "
            f"chunk_streaming={'on' if self.chunk_streaming else 'off'})."
        )

    def set_language_pair(self, source=None, target=None):
        """
        Reload Piper voice for the current TARGET_LANG after [g] swap.
        Uses PIPER_VOICE if still matching the new target locale; else default.
        """
        target = (target if target is not None else self.cfg.TARGET_LANG) or "en"
        target = str(target).lower().strip()
        explicit = getattr(self.cfg, "PIPER_VOICE", "").strip()
        if explicit and explicit.lower().startswith(target[:2]):
            voice_id = explicit
        else:
            voice_id = default_voice_for_lang(target)
        if getattr(self.cfg, "PIPER_QUALITY", "medium").lower() == "fast":
            voice_id = fast_voice_for(voice_id)
        if voice_id == self.voice_id:
            return
        model_path = _ensure_model(voice_id, self.model_dir, log=self.log)
        with self._synth_lock:
            self._voice = _load_piper_voice(model_path, self.cfg, log=self.log)
            self.voice_id = voice_id
            self._sample_rate = None
        self.log(f"Piper voice reloaded for TARGET={target}: {voice_id}")

    def set_voice(self, voice_id):
        """Edge-compatible hook; Piper maps target lang instead of Edge voice ids."""
        if hasattr(self, "set_language_pair"):
            self.set_language_pair()

    def _pcm_parts_to_audio(self, pcm_parts):
        if not pcm_parts:
            return None
        pcm = b"".join(pcm_parts)
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _collect_utterance(self, text):
        """Run Piper once and return the full float32 buffer."""
        text = (text or "").strip()
        if not text:
            return None, None

        pcm_parts = []
        sample_rate = self._sample_rate or 22050
        try:
            for chunk in self._voice.synthesize(text, syn_config=self._syn_config):
                sample_rate = chunk.sample_rate
                self._sample_rate = sample_rate
                pcm_parts.append(chunk.audio_int16_bytes)
        except Exception as exc:
            raise SynthesisError(f"Piper synthesis failed: {exc}") from exc

        audio = self._pcm_parts_to_audio(pcm_parts)
        if audio is None or len(audio) == 0:
            return None, None
        return audio, sample_rate

    def _stream_utterance(self, text, on_segment):
        """
        Stream Piper ONNX output in small playback buffers (~60ms) so audio
        starts before the full sentence is synthesized.
        """
        text = (text or "").strip()
        if not text:
            return None, None

        pcm_buffer = []
        buffer_samples = 0
        sample_rate = self._sample_rate or 22050
        min_samples = max(1, int(sample_rate * self.playback_buffer_ms / 1000))
        emitted = []
        first_flush = True

        try:
            for chunk in self._voice.synthesize(text, syn_config=self._syn_config):
                sample_rate = chunk.sample_rate
                self._sample_rate = sample_rate
                pcm_buffer.append(chunk.audio_int16_bytes)
                buffer_samples += len(chunk.audio_int16_bytes) // 2

                ready = buffer_samples >= min_samples or (
                    first_flush and buffer_samples > 0
                )
                if self.chunk_streaming and ready:
                    audio = self._pcm_parts_to_audio(pcm_buffer)
                    pcm_buffer = []
                    buffer_samples = 0
                    first_flush = False
                    if audio is not None and len(audio) > 0:
                        on_segment(audio, sample_rate)
                        emitted.append(audio)
        except Exception as exc:
            raise SynthesisError(f"Piper synthesis failed: {exc}") from exc

        if pcm_buffer:
            audio = self._pcm_parts_to_audio(pcm_buffer)
            if audio is not None and len(audio) > 0:
                on_segment(audio, sample_rate)
                emitted.append(audio)

        if not emitted:
            return None, None
        return np.concatenate(emitted).astype(np.float32), sample_rate

    def synthesize(self, text):
        """Full utterance synthesis (replay/edit and persistence)."""
        with self._synth_lock:
            return self._collect_utterance(text)

    def synthesize_clause(self, text, on_segment):
        """Synthesize one pre-split clause with ONNX chunk streaming."""
        with self._synth_lock:
            return self._stream_utterance(text, on_segment)

    def synthesize_streaming(self, text, on_segment):
        """Low-latency path with ONNX chunk streaming."""
        with self._synth_lock:
            return self._synthesize_streaming_unlocked(text, on_segment)

    def _synthesize_streaming_unlocked(self, text, on_segment):
        text = (text or "").strip()
        if not text:
            return None, None

        if not self.chunk_streaming:
            audio, sample_rate = self._collect_utterance(text)
            if audio is not None:
                on_segment(audio, sample_rate)
            return audio, sample_rate

        segments = split_piper_segments(text, max_chars=self.segment_min_chars)
        if len(segments) <= 1:
            return self._stream_utterance(text, on_segment)

        merge_tail = getattr(self.cfg, "PIPER_MERGE_TAIL", True)
        if merge_tail and len(segments) > 1:
            first, rest = segments[0], " ".join(segments[1:])
            parts = []
            sample_rate = None
            audio, sample_rate = self._stream_utterance(first, on_segment)
            if audio is not None and len(audio) > 0:
                parts.append(audio)
            if rest.strip():
                audio, sample_rate = self._stream_utterance(rest, on_segment)
                if audio is not None and len(audio) > 0:
                    parts.append(audio)
            if not parts:
                return None, None
            return np.concatenate(parts).astype(np.float32), sample_rate

        parts = []
        sample_rate = None
        for segment in segments:
            audio, sample_rate = self._stream_utterance(segment, on_segment)
            if audio is not None and len(audio) > 0:
                parts.append(audio)

        if not parts:
            return None, None
        return np.concatenate(parts).astype(np.float32), sample_rate
