"""
pipeline.py
===========
Orchestrates the full pipeline with three threads connected by queues:

    [recorder thread]  mic -> chunk_queue
    [processor thread] chunk_queue -> STT -> translate -> TTS -> playback_queue
    [playback thread]  playback_queue -> VB-Cable output device

All threads are daemons and watch a shared `stop_event` for clean shutdown.
"""

import datetime
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import soundfile as sf

from . import db, ui
from .capture import Recorder, is_capture_error
from .mic_control import MicController, warn_if_muted
from .playback import INTERRUPT, Player
from .stt_filter import (
    clean_transcript,
    is_hallucination,
    should_discard_transcript,
    transcript_discard_reason,
)


class _ChunkSkip:
    """Ordered sound-OFF placeholder: print message only when this slot releases."""

    __slots__ = ("message", "kind")

    def __init__(self, message="", kind="dim"):
        self.message = message or ""
        self.kind = kind  # dim | warn | error


from .tts_segments import StreamingSegmentFeeder


class Pipeline:
    def __init__(
        self,
        config,
        input_device,
        output_device,
        transcriber,
        translator,
        synthesizer,
        session_id,
        monitor_device=None,
        on_listening=None,
        input_device_name=None,
    ):
        self.cfg = config
        self.input_device = input_device
        self.output_device = output_device
        self.monitor_device = monitor_device
        self.session_id = session_id
        # Human-readable capture name (PortAudio) for Core Audio matching.
        self.input_device_name = input_device_name or ""

        self.transcriber = transcriber
        self.translator = translator
        self.synthesizer = synthesizer

        # Phrase translation memory (exact full-sentence cache)
        try:
            from .phrase_cache import init_phrase_cache

            self.phrase_cache = init_phrase_cache(config)
            n_warm = int(getattr(self.phrase_cache, "_warmup_count", 0) or 0)
            if getattr(config, "PHRASE_CACHE", False) and (
                getattr(config, "VERBOSE", False)
                or getattr(config, "PHRASE_CACHE_LOG", True)
            ):
                ui.info(
                    f"Phrase cache ON · mem warm-up {n_warm} pair(s) · "
                    f"[pc off] desliga · [pc force] re-traduz próxima",
                    indent=0,
                )
        except Exception as exc:
            self.phrase_cache = None
            if getattr(config, "VERBOSE", False):
                ui.dim(f"Phrase cache indisponível: {exc}")

        self.chunk_queue = queue.Queue()
        self.playback_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.mic = MicController(device_name=self.input_device_name)

        self.recorder = Recorder(
            config,
            input_device,
            self.chunk_queue,
            self.stop_event,
            on_listening=on_listening,
            paragraph_split_enabled=self._should_paragraph_split,
            shorter_end_enabled=lambda: not self.is_sound_enabled(),
            # Self-heal: recorder re-opens gate if pipeline left it closed wrongly
            capture_should_run=self.capture_should_run,
        )

        self.history = []
        self.history_lock = threading.Lock()
        self.full_transcript = []

        # Ensure cache directory exists
        self.cache_dir = os.path.join(".cache", "audio_sessions", session_id)
        os.makedirs(self.cache_dir, exist_ok=True)

        # Load existing chunks if resuming a session
        # full_transcript entries: (n, heard, translated, created_at, timing_dict)
        existing_chunks = db.load_session_chunks(session_id)
        max_chunk = 0
        for (
            chunk_num,
            heard_text,
            translated_text,
            audio_path,
            created_at,
            timing,
        ) in existing_chunks:
            self.full_transcript.append(
                (chunk_num, heard_text, translated_text, created_at or "", timing or {})
            )
            self.history.append((chunk_num, heard_text, translated_text, audio_path))
            max_chunk = max(max_chunk, chunk_num)

        # Load existing synonyms if resuming a session
        self.synonyms = []
        existing_synonyms = db.load_session_synonyms(session_id)
        for word, explanation in existing_synonyms:
            self.synonyms.append((word, explanation))

        # Load existing favorites if resuming a session
        self.favorites = []
        existing_favorites = db.load_session_favorites(session_id)
        for chunk_num, heard, translated in existing_favorites:
            self.favorites.append((chunk_num, heard, translated))

        # Load free-text comments per chunk: {chunk_num: [(text, created_at), ...]}
        self.comments = {}
        try:
            self.comments = db.load_session_comments_map(session_id)
        except Exception:
            self.comments = {}

        self._chunk_count = max_chunk
        self._next_release = max_chunk + 1
        self._pending_chunks = {}
        self._chunk_num_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._threads = []
        self._executor = None
        self._player = None
        self._player_lock = threading.Lock()
        # Default OFF: text-only until user enables live TTS with [s].
        self.sound_enabled = False
        self.sound_lock = threading.Lock()
        self._bg_tts_queue = queue.Queue()
        self._stt_lock = threading.Lock()
        self._playback_suppressed = False
        self._playback_suppress_lock = threading.Lock()

        # Capture gate while TTS plays (anti acoustic feedback loop).
        # Refcount covers streaming multi-segment play(); hangover covers speaker ring-out.
        self._capture_hold_lock = threading.Lock()
        self._capture_hold_count = 0
        self._capture_hold_timer = None  # threading.Timer | None
        # Monotonic deadline: keep STT gate closed after TTS (anti speaker echo).
        # Must be part of capture_should_run — otherwise self-heal reopens mid-hangover
        # and VAD latches onto TTS ring-out ("travou após N frases").
        self._capture_hangover_until = 0.0

        # Deferred language swap ([g]): never drop in-flight STT/translate/TTS.
        self._lang_swap_lock = threading.Lock()
        self._processor_busy_count = 0
        self._pending_language_swap = False
        self._on_language_swapped = None  # optional callback(src, tgt, voice)

        # Direct voice bypass ([b]/F2): mic → CABLE without STT/translate.
        # RLock: start path may re-enter is_passthrough_active / arm helpers
        # (plain Lock deadlocked the TUI on F2).
        self._passthrough_lock = threading.RLock()
        self._passthrough_active = False
        self._passthrough_stop = threading.Event()
        self._passthrough_thread = None

    def is_sound_enabled(self):
        with self.sound_lock:
            return self.sound_enabled

    def set_sound_enabled(self, enabled):
        """
        Enable/disable sound and apply pipeline side effects (interrupt,
        parallel workers, ordered-release cursor). Idempotent if already
        in the requested state.
        """
        enabled = bool(enabled)
        with self.sound_lock:
            if self.sound_enabled == enabled:
                return enabled
            self.sound_enabled = enabled
        if not enabled:
            self._playback_suppressed = True
            self._interrupt_playback(force=True)
            # Sound-ON path never advances ordered release — catch up now.
            self._sync_ordered_release_cursor()
            if getattr(self.cfg, "SOUND_OFF_PARALLEL", True):
                self._ensure_executor()
        else:
            self._resume_chunk_playback()
            with self._player_lock:
                if self._player is not None:
                    self._player.clear_interrupt()
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
            # After leaving parallel mode, keep cursor ready for next mute.
            self._sync_ordered_release_cursor()
        return enabled

    def _sync_ordered_release_cursor(self):
        """
        Align ordered sound-OFF publisher with the next chunk number.

        Sound-ON chunks never fill ordered slots, so _next_release can lag far
        behind _chunk_count. Without this, results for new muted chunks sit in
        _pending_chunks forever (Processing… with no Heard/Translated).
        """
        with self._chunk_num_lock:
            with self._release_lock:
                target = self._chunk_count + 1
                if self._next_release < target:
                    # Drop stale slots from a previous muted window (if any).
                    stale = [k for k in self._pending_chunks if k < target]
                    for k in stale:
                        self._pending_chunks.pop(k, None)
                    self._next_release = target

    def toggle_sound(self):
        with self.sound_lock:
            target = not self.sound_enabled
        return self.set_sound_enabled(target)

    # ------------------------------------------------------------------ #
    # Microphone mute (Windows OS + app capture gate)
    # ------------------------------------------------------------------ #
    def is_mic_muted(self):
        """True only when user paused escuta with [n] (app mute)."""
        return self.mic.is_app_muted()

    def mic_endpoint_name(self):
        return self.mic.resolved_name()

    def is_output_playing(self) -> bool:
        """True while TTS is on Cable/monitor or in post-play hangover (mic closed)."""
        try:
            if self._is_capture_held_for_playback():
                return True
        except Exception:
            pass
        try:
            if time.monotonic() < float(self._capture_hangover_until or 0.0):
                return True
        except Exception:
            pass
        return False

    def capture_should_run(self) -> bool:
        """
        Desired capture gate: escuta always ON except:
          - [n] app mute
          - voice bypass [b]
          - TTS playing on Cable/headphones (always — UX: don't speak over TTS)
          - post-TTS hangover window (must stay closed — no self-heal)
        """
        try:
            if self.is_passthrough_active():
                return False
        except Exception:
            pass
        try:
            if self.mic.is_app_muted():
                return False
        except Exception:
            pass
        # Always close mic while TTS plays (Cable and/or headphones/speakers).
        # MUTE_CAPTURE_DURING_PLAYBACK only used to be optional; leaving mic open
        # during long TTS made users think they could speak mid-playback.
        try:
            if self._is_capture_held_for_playback():
                return False
        except Exception:
            pass
        try:
            if time.monotonic() < float(self._capture_hangover_until or 0.0):
                return False
        except Exception:
            pass
        return True

    def sync_capture_gate(self, *, log_resume: bool = False) -> bool:
        """
        Apply capture_should_run() to the recorder.

        Single source of truth so escuta never stays off after TTS / skip /
        OS-mute quirks — only [n] (and bypass/TTS-hold) may close the gate.
        """
        want = bool(self.capture_should_run())
        was = True
        try:
            was = bool(self.recorder.is_capture_enabled())
        except Exception:
            was = True
        try:
            self.recorder.set_capture_enabled(want)
        except Exception:
            pass
        if log_resume and want and not was:
            try:
                ui.listen_ready("escuta ativa restaurada")
            except Exception:
                pass
        return want

    def toggle_mic(self):
        """
        Mute/unmute via [n]: app gate (+ OS tray when possible).

        Escuta stays ON at all other times (sync_capture_gate).
        """
        if self.is_passthrough_active():
            # Leaving bypass first so STT gate state stays consistent
            self.set_voice_passthrough(False)
        muted, os_ok, name = self.mic.toggle()
        self.sync_capture_gate(log_resume=not muted)
        return muted, os_ok, name

    def set_mic_muted(self, muted: bool):
        """
        Force mic mute/unmute (OS when possible + app gate).

        Used by the TUI mute modal ([n] to leave). Returns (muted, os_ok, name).
        """
        if muted and self.is_passthrough_active():
            self.set_voice_passthrough(False)
        muted_now, os_ok, name = self.mic.set_muted(bool(muted))
        self.sync_capture_gate(log_resume=not muted_now)
        return muted_now, os_ok, name

    # ------------------------------------------------------------------ #
    # Force soft-listen ([N]) — low-energy VAD + yellow UI borders
    # ------------------------------------------------------------------ #
    def is_force_soft_listen(self) -> bool:
        try:
            return bool(self.recorder.is_force_soft_listen())
        except Exception:
            return False

    def set_force_soft_listen(self, enabled: bool) -> bool:
        """
        Enable/disable [N] mode: accept soft / low-volume speech for translation.

        When enabling: unmute app mic, open capture gate, arm escuta.
        Returns new state (True = force soft-listen ON).
        """
        enabled = bool(enabled)
        try:
            self.recorder.set_force_soft_listen(enabled)
        except Exception:
            pass
        if enabled:
            # Leave mute / bypass so soft speech can be captured
            try:
                if self.is_passthrough_active():
                    self.set_voice_passthrough(False)
            except Exception:
                pass
            try:
                if self.mic.is_app_muted():
                    self.mic.set_muted(False)
            except Exception:
                pass
            try:
                self.sync_capture_gate(log_resume=True)
            except Exception:
                pass
            try:
                self._arm_listen_after_tts(note="[N] escuta forçada — voz baixa OK")
            except Exception:
                try:
                    self.recorder.set_capture_enabled(True)
                except Exception:
                    pass
        return self.is_force_soft_listen()

    def toggle_force_soft_listen(self) -> bool:
        """Toggle [N] force soft-listen. Returns new ON/OFF state."""
        return self.set_force_soft_listen(not self.is_force_soft_listen())

    # ------------------------------------------------------------------ #
    # Direct voice bypass ([b]) — mic → OUTPUT (CABLE) without translation
    # ------------------------------------------------------------------ #
    def is_passthrough_active(self) -> bool:
        with self._passthrough_lock:
            return self._passthrough_active

    def set_voice_passthrough(self, enabled: bool) -> bool:
        """
        Enable/disable live mic passthrough to OUTPUT_DEVICE (VB-Cable).

        When ON: STT/translate capture is paused; raw voice goes to Teams mic path.
        When OFF: normal listen/translate resumes (if mic not muted).
        Returns the new active state.
        """
        enabled = bool(enabled)
        join_thread = None
        with self._passthrough_lock:
            if enabled == self._passthrough_active:
                return self._passthrough_active
            if enabled:
                self._start_passthrough_unlocked()
                return True
            join_thread = self._signal_stop_passthrough_unlocked()
        # NEVER join / resume while holding _passthrough_lock (deadlock).
        self._join_passthrough_thread(join_thread)
        self._finish_stop_passthrough()
        return False

    def toggle_voice_passthrough(self) -> bool:
        """Toggle bypass; returns True if passthrough is now active."""
        join_thread = None
        with self._passthrough_lock:
            if self._passthrough_active:
                join_thread = self._signal_stop_passthrough_unlocked()
            else:
                self._start_passthrough_unlocked()
                return True
        # Join + resume OUTSIDE the lock — second [b] used to freeze the app:
        # stop held the lock, then join + _resume_chunk_playback →
        # is_passthrough_active() re-entered the same non-reentrant Lock.
        self._join_passthrough_thread(join_thread)
        self._finish_stop_passthrough()
        return False

    def _signal_stop_passthrough_unlocked(self):
        """
        Mark bypass OFF and return the worker thread to join *outside* the lock.

        Caller must hold ``_passthrough_lock``.
        """
        self._passthrough_stop.set()
        t = self._passthrough_thread
        self._passthrough_thread = None
        self._passthrough_active = False
        return t

    def _join_passthrough_thread(self, t):
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        # Ready for a later start (start also clears this flag).
        try:
            self._passthrough_stop.clear()
        except Exception:
            pass

    def _finish_stop_passthrough(self):
        """Resume TTS + STT after bypass OFF (must not hold _passthrough_lock)."""
        try:
            self.recorder.resume_stream()
        except Exception:
            pass
        try:
            self._resume_chunk_playback()
        except Exception:
            pass
        try:
            # Hard re-open escuta (clears hold/hangover; only [n] keeps closed)
            self._arm_listen_after_tts(note="bypass OFF — escuta normal")
        except Exception:
            try:
                self.sync_capture_gate(log_resume=True)
            except Exception:
                pass

    def _start_passthrough_unlocked(self):
        """Caller holds ``_passthrough_lock``. Must not deadlock on nested lock."""
        # Same hard stop as [x]: cut any TTS already going to Cable/Teams
        try:
            with self._playback_suppress_lock:
                self._playback_suppressed = True
            self._interrupt_playback(force=True)
        except Exception:
            pass
        # Clear TTS hold/hangover WITHOUT arming listen (arm would re-open
        # capture and call is_passthrough_active under this same lock → freeze).
        try:
            with self._capture_hold_lock:
                self._cancel_capture_hold_timer_unlocked()
                self._capture_hold_count = 0
                self._capture_hangover_until = 0.0
        except Exception:
            pass
        # Release mic InputStream so bypass can open the same device (Windows
        # PortAudio freezes if two exclusive streams fight for one mic).
        try:
            freed = self.recorder.suspend_stream(wait_s=0.6)
            if not freed:
                try:
                    from . import ui

                    ui.warn(
                        "[b] Mic ainda ocupado pelo VAD — bypass pode falhar; "
                        "tente F2 de novo em 1s.",
                        panel="app",
                    )
                except Exception:
                    pass
        except Exception:
            try:
                self.recorder.abort_utterance()
                self.recorder.set_capture_enabled(False)
            except Exception:
                pass
        # Free OUTPUT device from Player so we can open a live stream
        with self._player_lock:
            if self._player is not None:
                try:
                    self._player.close()
                except Exception:
                    pass
                self._player = None
        # Drop any TTS still queued while we own Cable for raw voice
        try:
            while True:
                self.playback_queue.get_nowait()
        except Exception:
            pass
        try:
            cam = getattr(self, "webcam_service", None)
            if cam is not None and hasattr(cam, "clear_tts_audio"):
                cam.clear_tts_audio()
        except Exception:
            pass

        self._passthrough_stop.clear()
        self._passthrough_active = True
        # Suppress must stay True for entire bypass ON (in-flight chunks
        # call _resume_chunk_playback before TTS — must not re-arm Cable).
        with self._playback_suppress_lock:
            self._playback_suppressed = True
        self._passthrough_thread = threading.Thread(
            target=self._passthrough_loop,
            name="voice-passthrough",
            daemon=True,
        )
        self._passthrough_thread.start()

    def _passthrough_loop(self):
        """
        Low-latency loop: INPUT_DEVICE → OUTPUT_DEVICE (and optional monitor).

        Uses the pipeline sample rate (16 kHz mono) to match capture config.
        """
        import sounddevice as sd

        rate = int(getattr(self.cfg, "SAMPLE_RATE", 16000) or 16000)
        block = max(64, int(rate * 0.02))  # ~20 ms
        in_dev = self.input_device
        out_dev = self.output_device
        mon_dev = self.monitor_device
        # Only open monitor if MONITOR_PLAYBACK is on and a device is set
        use_mon = mon_dev is not None and bool(
            getattr(self.cfg, "MONITOR_PLAYBACK", False)
        )
        # True if loop ended without user toggle / app stop (error or device fail)
        unexpected_exit = False
        me = threading.current_thread()

        try:
            with (
                sd.InputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="float32",
                    blocksize=block,
                    device=in_dev,
                ) as inn,
                sd.OutputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="float32",
                    blocksize=block,
                    device=out_dev,
                ) as out,
            ):
                mon = None
                try:
                    if use_mon:
                        mon = sd.OutputStream(
                            samplerate=rate,
                            channels=1,
                            dtype="float32",
                            blocksize=block,
                            device=mon_dev,
                        )
                        mon.start()
                    while (
                        not self._passthrough_stop.is_set()
                        and not self.stop_event.is_set()
                    ):
                        data, overflowed = inn.read(block)
                        if overflowed:
                            pass
                        if data is None or len(data) == 0:
                            continue
                        if data.ndim > 1:
                            data = data[:, 0:1]
                        else:
                            data = data.reshape(-1, 1)
                        frame = np.ascontiguousarray(data, dtype=np.float32)
                        # Stop may have been set during blocking read — don't write
                        if self._passthrough_stop.is_set() or self.stop_event.is_set():
                            break
                        out.write(frame)
                        if mon is not None:
                            try:
                                mon.write(frame)
                            except Exception:
                                pass
                finally:
                    if mon is not None:
                        try:
                            mon.stop()
                            mon.close()
                        except Exception:
                            pass
        except Exception as exc:
            unexpected_exit = True
            ui.error(f"[b] Bypass de voz falhou: {exc}", panel="app")
        finally:
            user_stop = self._passthrough_stop.is_set() or self.stop_event.is_set()
            with self._passthrough_lock:
                # User stop already cleared active/thread under lock; only clean
                # up if we still own the slot (crash / unexpected exit).
                if self._passthrough_thread is me:
                    self._passthrough_thread = None
                    self._passthrough_active = False
                elif self._passthrough_active and self._passthrough_thread is None:
                    self._passthrough_active = False
            # Crash / device fail: keep TTS cut until user toggles [b] again.
            if unexpected_exit or not user_stop:
                try:
                    with self._playback_suppress_lock:
                        self._playback_suppressed = True
                except Exception:
                    pass

    def check_mic_muted_warn(self, indent=3):
        """Startup / pre-listen warning if OS mic is muted or volume ~0."""
        return warn_if_muted(
            self.mic,
            log_warn=lambda m: ui.warn(m, indent=indent),
            log_info=lambda m: ui.info(m, indent=indent),
        )

    # ------------------------------------------------------------------ #
    # Language pair swap ([g]) — deferred if a chunk is in flight
    # ------------------------------------------------------------------ #
    def language_pair_label(self):
        """Uppercase direction label, e.g. 'EN → PT'."""
        src = (getattr(self.cfg, "SOURCE_LANG", "") or "?").upper()
        tgt = (getattr(self.cfg, "TARGET_LANG", "") or "?").upper()
        return f"{src} → {tgt}"

    def set_language_swap_callback(self, callback):
        """Optional ui callback(src, tgt, voice) after a deferred swap applies."""
        self._on_language_swapped = callback

    def _processor_is_busy(self) -> bool:
        with self._lang_swap_lock:
            return self._processor_busy_count > 0 or not self.chunk_queue.empty()

    def _mark_processor_enter(self):
        with self._lang_swap_lock:
            self._processor_busy_count += 1

    def _mark_processor_leave(self):
        """End of one chunk; apply deferred swap if pipeline is idle."""
        apply_now = False
        with self._lang_swap_lock:
            if self._processor_busy_count > 0:
                self._processor_busy_count -= 1
            if (
                self._pending_language_swap
                and self._processor_busy_count == 0
                and self.chunk_queue.empty()
            ):
                self._pending_language_swap = False
                apply_now = True
        if apply_now:
            result = self.apply_language_swap()
            cb = self._on_language_swapped
            if cb is not None:
                try:
                    cb(*result[:3])
                except Exception:
                    pass

    def request_language_swap(self):
        """
        Invert languages now if idle; otherwise schedule after in-flight work.

        Does NOT drain the chunk queue or stop current TTS mid-sentence.

        Returns dict:
          status: "applied" | "deferred" | "already_pending"
          old_pair, new_pair (labels)
          source, target, voice, warnings  (only when applied)
        """
        with self._lang_swap_lock:
            busy = self._processor_busy_count > 0 or not self.chunk_queue.empty()
            if self._pending_language_swap:
                # Flip pending cancel? User pressed g twice while waiting:
                # cancel the pending swap (back to original intent).
                self._pending_language_swap = False
                return {
                    "status": "cancelled_pending",
                    "old_pair": self.language_pair_label(),
                    "new_pair": self.language_pair_label(),
                }
            if busy:
                self._pending_language_swap = True
                # Preview flipped label without mutating cfg yet.
                src = (getattr(self.cfg, "SOURCE_LANG", "") or "?").upper()
                tgt = (getattr(self.cfg, "TARGET_LANG", "") or "?").upper()
                return {
                    "status": "deferred",
                    "old_pair": f"{src} → {tgt}",
                    "new_pair": f"{tgt} → {src}",
                }

        old_pair = self.language_pair_label()
        src, tgt, voice, warnings = self.apply_language_swap()
        return {
            "status": "applied",
            "old_pair": old_pair,
            "new_pair": self.language_pair_label(),
            "source": src,
            "target": tgt,
            "voice": voice,
            "warnings": warnings,
        }

    def swap_languages(self):
        """
        Backward-compatible entry: request swap; if applied return 4-tuple
        like the old API. Prefer request_language_swap() for full status.
        """
        info = self.request_language_swap()
        if info["status"] == "applied":
            return (
                info["source"],
                info["target"],
                info["voice"],
                info.get("warnings") or [],
            )
        # Deferred / cancelled — no cfg change yet.
        src = getattr(self.cfg, "SOURCE_LANG", "")
        tgt = getattr(self.cfg, "TARGET_LANG", "")
        voice = getattr(self.cfg, "TTS_VOICE", "")
        return src, tgt, voice, []

    def apply_language_swap(self):
        """
        Invert SOURCE_LANG ↔ TARGET_LANG and rebind STT / translate / TTS.

        Safe only when no chunk is mid-pipeline (or after that chunk finishes).
        Does not rewrite historical chunks. Does not drain the work queue.

        Returns (new_source, new_target, new_voice, warnings_list).
        """
        from .synthesize import default_edge_voice_for_lang

        warnings = []

        # Drop only an unfinished mic partial (not yet a queued phrase).
        try:
            self.recorder.abort_utterance()
        except Exception:
            pass

        old_src = (getattr(self.cfg, "SOURCE_LANG", "") or "").strip() or "en"
        old_tgt = (getattr(self.cfg, "TARGET_LANG", "") or "").strip() or "en"
        old_voice = (getattr(self.cfg, "TTS_VOICE", "") or "").strip()
        alt_voice = (getattr(self.cfg, "TTS_VOICE_ALT", "") or "").strip()
        if not alt_voice:
            alt_voice = default_edge_voice_for_lang(old_src)

        # 1) Swap language codes on the shared config object.
        self.cfg.SOURCE_LANG = old_tgt
        self.cfg.TARGET_LANG = old_src

        # 2) Swap Edge voices.
        new_voice = alt_voice
        self.cfg.TTS_VOICE = new_voice
        self.cfg.TTS_VOICE_ALT = old_voice

        # 3) Rebind STT language cache if any.
        tr = self.transcriber
        if hasattr(tr, "language"):
            tr.language = self.cfg.SOURCE_LANG

        # 4) Rebind translator.
        if hasattr(self.translator, "set_language_pair"):
            try:
                self.translator.set_language_pair(
                    self.cfg.SOURCE_LANG, self.cfg.TARGET_LANG
                )
            except Exception as exc:
                warnings.append(f"translator rebind: {exc}")
        elif hasattr(self.translator, "refresh_prompt"):
            try:
                self.translator.refresh_prompt()
            except Exception as exc:
                warnings.append(f"LLM prompt refresh: {exc}")

        # 5) TTS: edge sync now; Piper may run async.
        synth = self.synthesizer
        src_lang = self.cfg.SOURCE_LANG
        tgt_lang = self.cfg.TARGET_LANG

        def _rebind_tts():
            try:
                if hasattr(synth, "edge") and hasattr(synth.edge, "set_voice"):
                    synth.edge.set_voice(new_voice)
                if hasattr(synth, "set_voice") and not hasattr(synth, "piper"):
                    synth.set_voice(new_voice)
                if hasattr(synth, "piper") and hasattr(
                    synth.piper, "set_language_pair"
                ):
                    synth.piper.set_language_pair(src_lang, tgt_lang)
                elif hasattr(synth, "set_language_pair") and not hasattr(synth, "edge"):
                    synth.set_language_pair(src_lang, tgt_lang)
            except Exception as exc:
                ui.warn(f"TTS rebind after swap: {exc}")

        if hasattr(synth, "set_voice") and not hasattr(synth, "piper"):
            try:
                synth.set_voice(new_voice)
            except Exception as exc:
                warnings.append(f"TTS voice rebind: {exc}")
        else:
            try:
                if hasattr(synth, "edge") and hasattr(synth.edge, "set_voice"):
                    synth.edge.set_voice(new_voice)
            except Exception as exc:
                warnings.append(f"Edge voice rebind: {exc}")
            threading.Thread(target=_rebind_tts, name="tts-rebind", daemon=True).start()

        return self.cfg.SOURCE_LANG, self.cfg.TARGET_LANG, new_voice, warnings

    # Codes accepted by runtime TARGET change ([t]) and docs.
    TARGET_LANG_CHOICES = (
        "en",
        "pt",
        "es",
        "fr",
        "de",
        "it",
        "zh",
        "ja",
    )

    @staticmethod
    def normalize_lang_code(code: str) -> str:
        """Normalize user input (EN, pt-BR, GERMAN…) to a 2-letter code."""
        raw = (code or "").strip().lower()
        if not raw:
            return ""
        # Full names / aliases
        aliases = {
            "english": "en",
            "ingles": "en",
            "inglês": "en",
            "portuguese": "pt",
            "portugues": "pt",
            "português": "pt",
            "spanish": "es",
            "espanol": "es",
            "español": "es",
            "french": "fr",
            "frances": "fr",
            "français": "fr",
            "german": "de",
            "alemao": "de",
            "alemão": "de",
            "deutsch": "de",
            "italian": "it",
            "italiano": "it",
            "chinese": "zh",
            "chines": "zh",
            "chinês": "zh",
            "mandarin": "zh",
            "japanese": "ja",
            "japones": "ja",
            "japonês": "ja",
            "jp": "ja",
            "cn": "zh",
            "ger": "de",
            "deu": "de",
            "ita": "it",
        }
        if raw in aliases:
            return aliases[raw]
        # BCP-47 → primary subtag
        if "-" in raw or "_" in raw:
            raw = raw.replace("_", "-").split("-", 1)[0]
        # Strip non-letters
        raw = "".join(ch for ch in raw if ch.isalpha())
        return raw[:2] if len(raw) >= 2 else raw

    def set_target_language(self, code: str):
        """
        Change TARGET_LANG only (SOURCE unchanged). Rebinds translator + TTS.

        Returns dict:
          ok: bool
          error: str (if not ok)
          source, target, voice, old_target, warnings
        """
        from .synthesize import default_edge_voice_for_lang

        new_tgt = self.normalize_lang_code(code)
        if not new_tgt:
            return {
                "ok": False,
                "error": "Código vazio. Use EN, PT, ES, FR, DE, IT, ZH ou JA.",
            }
        if new_tgt not in self.TARGET_LANG_CHOICES:
            allowed = ", ".join(c.upper() for c in self.TARGET_LANG_CHOICES)
            return {
                "ok": False,
                "error": f"Idioma '{code}' não suportado. Use: {allowed}.",
            }

        old_tgt = (getattr(self.cfg, "TARGET_LANG", "") or "").strip().lower() or "en"
        if new_tgt == old_tgt:
            return {
                "ok": True,
                "unchanged": True,
                "source": self.cfg.SOURCE_LANG,
                "target": new_tgt,
                "voice": getattr(self.cfg, "TTS_VOICE", ""),
                "old_target": old_tgt,
                "warnings": [],
            }

        warnings = []
        old_voice = (getattr(self.cfg, "TTS_VOICE", "") or "").strip()
        new_voice = default_edge_voice_for_lang(new_tgt)

        # Keep previous target voice as ALT for [g] swap convenience.
        self.cfg.TARGET_LANG = new_tgt
        self.cfg.TTS_VOICE = new_voice
        if old_voice:
            self.cfg.TTS_VOICE_ALT = old_voice

        # Rebind translator (Google pair / LLM system prompt).
        if hasattr(self.translator, "set_language_pair"):
            try:
                self.translator.set_language_pair(
                    self.cfg.SOURCE_LANG, self.cfg.TARGET_LANG
                )
            except Exception as exc:
                warnings.append(f"translator rebind: {exc}")
        elif hasattr(self.translator, "refresh_prompt"):
            try:
                self.translator.refresh_prompt()
            except Exception as exc:
                warnings.append(f"LLM prompt refresh: {exc}")

        # Rebind TTS (edge sync; piper async if needed).
        synth = self.synthesizer
        src_lang = self.cfg.SOURCE_LANG
        tgt_lang = self.cfg.TARGET_LANG

        def _rebind_tts():
            try:
                if hasattr(synth, "edge") and hasattr(synth.edge, "set_voice"):
                    synth.edge.set_voice(new_voice)
                if hasattr(synth, "set_voice") and not hasattr(synth, "piper"):
                    synth.set_voice(new_voice)
                if hasattr(synth, "piper") and hasattr(
                    synth.piper, "set_language_pair"
                ):
                    synth.piper.set_language_pair(src_lang, tgt_lang)
                elif hasattr(synth, "set_language_pair") and not hasattr(synth, "edge"):
                    synth.set_language_pair(src_lang, tgt_lang)
            except Exception as exc:
                ui.warn(f"TTS rebind after TARGET change: {exc}")

        if hasattr(synth, "set_voice") and not hasattr(synth, "piper"):
            try:
                synth.set_voice(new_voice)
            except Exception as exc:
                warnings.append(f"TTS voice rebind: {exc}")
        else:
            try:
                if hasattr(synth, "edge") and hasattr(synth.edge, "set_voice"):
                    synth.edge.set_voice(new_voice)
            except Exception as exc:
                warnings.append(f"Edge voice rebind: {exc}")
            threading.Thread(
                target=_rebind_tts, name="tts-rebind-target", daemon=True
            ).start()

        return {
            "ok": True,
            "unchanged": False,
            "source": self.cfg.SOURCE_LANG,
            "target": new_tgt,
            "voice": new_voice,
            "old_target": old_tgt,
            "warnings": warnings,
        }

    def set_tts_voice(self, voice_id: str):
        """
        Change TTS_VOICE for upcoming edge-tts synthesis.

        Does not rewrite historical chunks; only future live/replay TTS uses
        the new voice. Never rebinds Piper (that is only for [g]/[t] language
        changes) — hybrid.set_voice would call set_language_pair and stall STT.

        Validation is offline-first (no list_voices network on the hot path).

        Returns dict: ok, error?, voice, old_voice, unchanged?, warnings
        """
        from .synthesize import resolve_edge_voice, warn_tts_voice_language

        # online=False: never block STT/TTS on Microsoft catalog fetch
        ok, canon_or_err, warnings = resolve_edge_voice(voice_id, online=False)
        if not ok:
            return {
                "ok": False,
                "error": canon_or_err,
                "voice": getattr(self.cfg, "TTS_VOICE", ""),
                "old_voice": getattr(self.cfg, "TTS_VOICE", ""),
                "warnings": warnings or [],
            }

        new_voice = canon_or_err
        old_voice = (getattr(self.cfg, "TTS_VOICE", "") or "").strip()
        if new_voice == old_voice:
            return {
                "ok": True,
                "unchanged": True,
                "voice": new_voice,
                "old_voice": old_voice,
                "warnings": warnings or [],
            }

        self.cfg.TTS_VOICE = new_voice
        # Keep previous as ALT so [g] can restore a useful pair.
        if old_voice and old_voice != new_voice:
            try:
                self.cfg.TTS_VOICE_ALT = old_voice
            except Exception:
                pass

        synth = self.synthesizer
        try:
            # Edge-only update. Prefer set_edge_voice / edge.set_voice so we
            # never call hybrid.set_voice() (that rebinds Piper and can stall STT).
            if hasattr(synth, "set_edge_voice"):
                synth.set_edge_voice(new_voice)
            elif hasattr(synth, "edge") and hasattr(synth.edge, "set_voice"):
                synth.edge.set_voice(new_voice)
            elif hasattr(synth, "set_voice") and not hasattr(synth, "piper"):
                # Pure edge Synthesizer
                synth.set_voice(new_voice)
            # piper-only: cfg.TTS_VOICE still updated for badge; no edge engine
        except Exception as exc:
            warnings = list(warnings or [])
            warnings.append(f"TTS rebind: {exc}")

        try:
            warn_tts_voice_language(
                self.cfg, log=lambda m: warnings.append(m) if m else None
            )
        except Exception:
            pass

        return {
            "ok": True,
            "unchanged": False,
            "voice": new_voice,
            "old_voice": old_voice,
            "warnings": warnings or [],
        }

    # ------------------------------------------------------------------ #
    # Mute capture during TTS (acoustic feedback guard)
    # ------------------------------------------------------------------ #
    def _mute_capture_during_playback_enabled(self) -> bool:
        return bool(getattr(self.cfg, "MUTE_CAPTURE_DURING_PLAYBACK", True))

    def _is_capture_held_for_playback(self) -> bool:
        with self._capture_hold_lock:
            return self._capture_hold_count > 0

    def _cancel_capture_hold_timer_unlocked(self):
        timer = self._capture_hold_timer
        self._capture_hold_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _arm_listen_after_tts(self, note: str = "após TTS — escuta limpa"):
        """
        Hard-reset listen gate (post-TTS, sound-off ready, or recovery).

        Always clears hold_count + hangover (leaks left the UI saying
        "Escuta pronta" while capture stayed closed — one phrase then freeze).
        Only [n] app-mute / bypass may keep the gate closed.
        """
        with self._capture_hold_lock:
            self._capture_hold_count = 0
            self._capture_hangover_until = 0.0
            self._cancel_capture_hold_timer_unlocked()
        try:
            self.recorder.abort_utterance()
        except Exception:
            pass
        # Open unless user mute or bypass — ignore stale hold/hangover
        blocked = False
        try:
            blocked = bool(self.mic.is_app_muted()) or bool(
                self.is_passthrough_active()
            )
        except Exception:
            blocked = False
        try:
            self.recorder.set_capture_enabled(not blocked)
        except Exception:
            pass
        try:
            ui.set_tts_playing(False)
        except Exception:
            pass
        if not blocked:
            try:
                ui.listen_ready(note, force=True)
            except Exception:
                pass
            try:
                if not self.recorder.is_capture_enabled():
                    ui.warn(
                        "Escuta: gate ainda FECHADO após arm — force open",
                        panel="app",
                    )
                    self.recorder.set_capture_enabled(True)
            except Exception:
                pass

    def _hold_capture_for_playback(self):
        """Pause STT for one play() (Cable + headphones). Always on for UX."""
        first = False
        with self._capture_hold_lock:
            self._cancel_capture_hold_timer_unlocked()
            self._capture_hangover_until = 0.0
            self._capture_hold_count += 1
            first = self._capture_hold_count == 1
        # Apply gate via single policy (hold count now > 0 → capture off)
        self.sync_capture_gate()
        if first:
            try:
                ui.set_tts_playing(True)
            except Exception:
                pass
            try:
                ui.pipeline_stage("play", source="voz")
            except Exception:
                pass

    def _release_capture_after_playback(self):
        """
        Drop one play() hold. When count hits 0, keep gate closed for hangover
        (ring-out on speakers/headphones), then arm escuta.

        If count already 0 ([x] already force-released), do nothing — avoid
        re-arming hangover after user interrupted.
        """
        hangover_ms = max(0, int(getattr(self.cfg, "MUTE_CAPTURE_HANGOVER_MS", 500)))
        with self._capture_hold_lock:
            if self._capture_hold_count <= 0:
                return
            self._capture_hold_count -= 1
            if self._capture_hold_count > 0:
                return
            self._cancel_capture_hold_timer_unlocked()
            try:
                self.recorder.set_capture_enabled(False)
            except Exception:
                pass
            if hangover_ms <= 0:
                self._capture_hangover_until = 0.0
            else:
                self._capture_hangover_until = time.monotonic() + (hangover_ms / 1000.0)
                try:
                    ui.set_tts_playing(True, detail=" (cauda…)")
                except Exception:
                    pass
                timer = threading.Timer(
                    hangover_ms / 1000.0, self._on_capture_hold_hangover
                )
                timer.daemon = True
                self._capture_hold_timer = timer
                timer.start()
                return
        self._arm_listen_after_tts(note="áudio terminou — pode falar")

    def _on_capture_hold_hangover(self):
        with self._capture_hold_lock:
            # Another play() started (or hold re-entered) while we waited.
            if self._capture_hold_count > 0:
                return
            self._capture_hold_timer = None
            self._capture_hangover_until = 0.0
        self._arm_listen_after_tts(note="áudio terminou — pode falar")

    def _reenable_capture_if_allowed_unlocked(self):
        """Compat: re-open capture via sync policy (call without hold lock)."""
        self._arm_listen_after_tts(note="áudio terminou — pode falar")

    def _force_release_capture_hold(self):
        """Drop all playback holds (shutdown / player teardown / [x])."""
        with self._capture_hold_lock:
            self._cancel_capture_hold_timer_unlocked()
            self._capture_hold_count = 0
            self._capture_hangover_until = 0.0
        self._arm_listen_after_tts(note="áudio parado — pode falar")

    def _ensure_executor(self):
        if not self._use_parallel_processing():
            return None
        if self._executor is None:
            workers = max(1, getattr(self.cfg, "SOUND_OFF_WORKERS", 2))
            self._executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="chunk-worker"
            )
        return self._executor

    def _is_tts_output_blocked(self) -> bool:
        """
        True when nothing may write TTS to OUTPUT_DEVICE (Cable → Teams).

        Covers: voice bypass [b], user [x] suppress, sound-off suppress.
        """
        if self.is_passthrough_active():
            return True
        with self._playback_suppress_lock:
            return bool(self._playback_suppressed)

    def _enqueue_playback(
        self, audio, sample_rate, interruptible=False, *, pre_tts_cue=False
    ):
        """
        Queue TTS for Cable Out.

        ``pre_tts_cue=True``: play headphones warning bip once before this
        item. Use only for the **first** segment of a chunk (or one-shot
        replay/edit). Later sentence/clause segments of the same chunk must
        pass ``False`` so long utterances do not bip on every phrase.
        """
        if not self.is_sound_enabled():
            return
        if self._is_tts_output_blocked():
            # Cable owned by bypass, or [x]/sound-off — never mix TTS in
            return
        self.playback_queue.put((audio, sample_rate, interruptible, bool(pre_tts_cue)))
        # Webcam lip-sync: feed the same TTS waveform that will hit Cable Out.
        try:
            cam = getattr(self, "webcam_service", None)
            if cam is not None and hasattr(cam, "push_tts_audio"):
                cam.push_tts_audio(audio, sample_rate)
        except Exception:
            pass

    def stop_playback(self):
        """Stop current TTS ([x]) and re-open escuta immediately."""
        if not self.is_sound_enabled() and not self.is_output_playing():
            return False
        with self._playback_suppress_lock:
            self._playback_suppressed = True
        # force=True: user [x] always stops even if PLAYBACK_INTERRUPT=false
        self._interrupt_playback(force=True)
        # Play()'s finally will also release, but arm now so user can speak ASAP
        try:
            self._force_release_capture_hold()
        except Exception:
            pass
        # Allow next TTS after [x] unless bypass owns Cable
        try:
            self._resume_chunk_playback()
        except Exception:
            pass
        return True

    def _resume_chunk_playback(self):
        """Allow TTS for the next (or current) chunk after stop or interrupt.

        No-op while voice bypass owns Cable — in-flight chunk workers call this
        before TTS (see process path); clearing suppress there re-armed Player
        and leaked translation audio into Teams during [b].

        Lock order: never hold suppress_lock and passthrough_lock together
        (start bypass takes suppress under passthrough).
        """
        if self.is_passthrough_active():
            return
        with self._playback_suppress_lock:
            self._playback_suppressed = False
        # Bypass may have started between the check and clear — re-arm suppress.
        if self.is_passthrough_active():
            with self._playback_suppress_lock:
                self._playback_suppressed = True
            return
        with self._player_lock:
            player = self._player
        if player is not None:
            player.clear_interrupt()

    def _enqueue_background_tts(self, chunk_num, fn):
        """Serialize muted-mode TTS jobs (one at a time, avoids edge/Piper races)."""
        self._bg_tts_queue.put((chunk_num, fn))

    def _background_tts_loop(self):
        while not self.stop_event.is_set():
            try:
                chunk_num, fn = self._bg_tts_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception as exc:
                ui.error(f"[chunk {chunk_num}] TTS failed: {exc}")

    def _interrupt_playback(self, force=False):
        if not force and not getattr(self.cfg, "PLAYBACK_INTERRUPT", True):
            return
        # Do NOT hold _player_lock across play(); interrupt must be set while
        # the playback thread is inside Player.play() or stop waits until end.
        with self._player_lock:
            player = self._player
        if player is not None:
            player.interrupt()
        while True:
            try:
                self.playback_queue.get_nowait()
            except queue.Empty:
                break
        # Lip-sync: drop scheduled TTS so mouth closes with audio stop
        try:
            cam = getattr(self, "webcam_service", None)
            if cam is not None and hasattr(cam, "clear_tts_audio"):
                cam.clear_tts_audio()
        except Exception:
            pass

    def _use_streaming_llm(self):
        return getattr(self.cfg, "STREAMING_LLM", False) and hasattr(
            self.translator, "translate_stream"
        )

    def _use_streaming_tts(self):
        if getattr(self.synthesizer, "supports_live_streaming", False):
            return True
        return getattr(self.cfg, "STREAMING_TTS", False) and hasattr(
            self.synthesizer, "synthesize_streaming"
        )

    def _use_tts_overlap(self):
        return (
            getattr(self.cfg, "STREAMING_TTS_OVERLAP", True)
            and self._use_streaming_llm()
            and self._use_streaming_tts()
            and hasattr(self.synthesizer, "synthesize_clause")
        )

    def _should_paragraph_split(self):
        """
        Whether capture may early-emit mid-utterance (sentence or paragraph).

        SENTENCE_SPLIT (default on): short pause after min speech → new chunk
        immediately (faster per-phrase text). PARAGRAPH_* used when sentence
        split is disabled.
        """
        if getattr(self.cfg, "SENTENCE_SPLIT", False):
            if getattr(self.cfg, "SENTENCE_SPLIT_SOUND_OFF_ONLY", True):
                return not self.is_sound_enabled()
            return True
        if not getattr(self.cfg, "PARAGRAPH_SPLIT", True):
            return False
        if getattr(self.cfg, "PARAGRAPH_SPLIT_SOUND_OFF_ONLY", True):
            return not self.is_sound_enabled()
        return True

    def _use_parallel_processing(self):
        return not self.is_sound_enabled() and getattr(
            self.cfg, "SOUND_OFF_PARALLEL", True
        )

    def _use_streaming_llm_for_chunk(self):
        return self._use_streaming_llm() and not self._use_parallel_processing()

    def _skip_tts_when_muted(self):
        return not self.is_sound_enabled() and getattr(
            self.cfg, "TTS_SKIP_WHEN_MUTED", True
        )

    def _alloc_chunk_num(self):
        with self._chunk_num_lock:
            self._chunk_count += 1
            return self._chunk_count

    @staticmethod
    def _timestamp_now():
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _record_transcript(self, n, heard, translated, created_at=None, timing=None):
        """Upsert full_transcript row: (n, heard, translated, created_at, timing)."""
        created_at = created_at or self._timestamp_now()
        timing = dict(timing) if timing else {}
        with self.history_lock:
            for idx, item in enumerate(self.full_transcript):
                if item[0] == n:
                    prev_created = item[3] if len(item) > 3 else created_at
                    prev_timing = item[4] if len(item) > 4 and item[4] else {}
                    merged = dict(prev_timing)
                    merged.update(timing)
                    self.full_transcript[idx] = (
                        n,
                        heard,
                        translated,
                        prev_created or created_at,
                        merged,
                    )
                    return created_at
            self.full_transcript.append((n, heard, translated, created_at, timing))
        return created_at

    def _push_webcam_subtitle(self, translated: str) -> None:
        """Replace vcam burn-in with current TARGET only (never append)."""
        cam = getattr(self, "webcam_service", None)
        if cam is None or not hasattr(cam, "push_subtitle_text"):
            return
        t = " ".join((translated or "").split())
        if not t:
            return
        try:
            cam.push_subtitle_text(t)
        except Exception:
            pass

    def _publish_chunk(
        self, n, heard, translated, audio_path, timing, timing_extra="", note=""
    ):
        if note:
            ui.dim(note)
        # Visual badge on Tradução: CACHE vs LIVE (when phrase cache was involved)
        from_cache = None
        if isinstance(timing, dict) and "translate_cache" in timing:
            from_cache = bool(timing.get("translate_cache"))
        if self._use_streaming_llm_for_chunk():
            ui.chunk_stream_done(n, heard, translated, from_cache=from_cache)
        else:
            ui.chunk_text_preview(n, heard, translated, from_cache=from_cache)
        # Always TARGET text for vcam burn-in (shown only if [sub] ON)
        self._push_webcam_subtitle(translated)
        created_at = self._timestamp_now()
        # Path set but file may still be flushing (sound OFF + background TTS)
        pending = bool(audio_path and str(audio_path).strip())
        if timing:
            ui.chunk_timings(
                n,
                timing,
                extra=timing_extra,
                at=created_at,
                audio_path=audio_path or "",
                audio_pending=pending,
            )
        elif audio_path is not None:
            ui.print_audio_ref(n, audio_path or "", pending_write=pending)
        with self.history_lock:
            self.history.append((n, heard, translated, audio_path))
        self._record_transcript(
            n, heard, translated, created_at=created_at, timing=timing
        )

    def _should_order_chunks(self, sound_off):
        return sound_off and getattr(self.cfg, "SOUND_OFF_PARALLEL", True)

    @staticmethod
    def _emit_skip_message(skip):
        if not skip or not skip.message:
            return
        if skip.kind == "warn":
            ui.warn(skip.message)
        elif skip.kind == "error":
            ui.error(skip.message)
        else:
            ui.dim(skip.message)

    def _finish_chunk_slot(self, n, result):
        """Ordered publish for parallel sound-OFF mode.

        result:
          - None              → silent skip (slot advances)
          - _ChunkSkip(...)   → print message in order, then advance
          - (h,t,path,tm,extra[,note]) → publish translation in order
        """
        with self._release_lock:
            self._pending_chunks[n] = result
            # Self-heal gaps left by sound-ON chunks (never entered this queue).
            if self._pending_chunks and self._next_release not in self._pending_chunks:
                min_pending = min(self._pending_chunks.keys())
                if self._next_release < min_pending:
                    self._next_release = min_pending
            while self._next_release in self._pending_chunks:
                entry = self._pending_chunks.pop(self._next_release)
                if isinstance(entry, _ChunkSkip):
                    self._emit_skip_message(entry)
                elif entry is not None:
                    if len(entry) == 6:
                        h, t, path, tm, extra, note = entry
                    else:
                        h, t, path, tm, extra = entry
                        note = ""
                    self._publish_chunk(
                        self._next_release, h, t, path, tm, extra, note=note
                    )
                self._next_release += 1

    def _release_chunk_result(
        self, n, heard, translated, audio_path, timing, timing_extra="", note=""
    ):
        self._finish_chunk_slot(
            n, (heard, translated, audio_path, timing, timing_extra, note)
        )

    def _persist_text_only(self, n, heard, translated, timing=None, created_at=None):
        try:
            created_at = db.upsert_chunk(
                self.session_id,
                n,
                heard,
                translated,
                "",
                timing=timing,
                created_at=created_at,
            )
            self._record_transcript(
                n, heard, translated, created_at=created_at, timing=timing
            )
            if self.cfg.VERBOSE:
                ui.dim(f"[chunk {n}] [debug] Texto gravado no SQLite (sem áudio).")
        except Exception as exc:
            ui.error(f"[chunk {n}] Erro ao salvar no banco de dados: {exc}")

    def _segment_max_chars(self):
        if getattr(self.synthesizer, "supports_live_streaming", False):
            return min(getattr(self.synthesizer, "segment_min_chars", 70), 55)
        return 120

    def _synthesize_clause(self, text, on_segment):
        if hasattr(self.synthesizer, "synthesize_clause"):
            return self.synthesizer.synthesize_clause(text, on_segment)
        return self.synthesizer.synthesize_streaming(text, on_segment)

    # ------------------------------------------------------------------ #
    def get_full_transcript(self):
        with self.history_lock:
            return list(self.full_transcript)

    def get_last_heard(self):
        with self.history_lock:
            if not self.history:
                return None
            n, heard, translated, audio_path = self.history[-1]
            return heard

    def get_heard_by_chunk(self, chunk_num):
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    return heard
        return None

    def get_audio_path_by_chunk(self, chunk_num):
        """Return stored audio_path for chunk N, or '' if none."""
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    return audio_path or ""
        # Fallback: conventional cache path if file exists.
        candidate = os.path.join(self.cache_dir, f"chunk_{chunk_num}.wav")
        if os.path.isfile(candidate):
            return candidate
        return ""

    def get_last_audio_path(self):
        with self.history_lock:
            if not self.history:
                return ""
            return self.history[-1][3] or ""

    def get_audio_path_map(self):
        """chunk_num → audio_path for list/export helpers."""
        with self.history_lock:
            return {n: (path or "") for n, _h, _t, path in self.history}

    def edit_chunk(self, chunk_num, new_text):
        """Translate, synthesize, play and overwrite a past chunk."""
        # Find the existing chunk to verify it exists
        found = False
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    found = True
                    break

        if not found:
            ui.warn(f"Chunk {chunk_num} não encontrado no histórico para editar.")
            return

        ui.info(f"Retraduzindo chunk {chunk_num}...")
        try:
            translated = self.translator.translate(new_text)
        except Exception as exc:
            ui.error(f"Erro ao traduzir: {exc}")
            return

        if not translated:
            ui.warn("Tradução vazia. Edição cancelada.")
            return

        ui.info("Sintetizando áudio novo...")
        try:
            tts_audio, sample_rate = self.synthesizer.synthesize(translated)
        except Exception as exc:
            ui.error(f"Erro ao sintetizar: {exc}")
            return

        # Overwrite WAV file
        audio_path = os.path.join(self.cache_dir, f"chunk_{chunk_num}.wav")
        try:
            sf.write(audio_path, tts_audio, sample_rate)
        except Exception as exc:
            ui.error(f"Erro ao salvar arquivo de áudio: {exc}")
            return

        # Update SQLite DB
        try:
            db.update_chunk(
                self.session_id, chunk_num, new_text, translated, audio_path
            )
        except Exception as exc:
            ui.error(f"Erro ao atualizar banco de dados: {exc}")

        # Update RAM structures
        with self.history_lock:
            # Update self.history
            for idx, (n, heard, translated_old, path) in enumerate(self.history):
                if n == chunk_num:
                    self.history[idx] = (chunk_num, new_text, translated, audio_path)
                    break

        self._record_transcript(chunk_num, new_text, translated, timing={})

        ui.chunk_status(
            chunk_num,
            new_text,
            translated,
            {"stt": 0.0, "translate": 0.0, "tts": 0.0, "total": 0.0},
            at=self._timestamp_now(),
        )

        self._resume_chunk_playback()
        self._enqueue_playback(tts_audio, sample_rate, pre_tts_cue=True)
        if self.is_sound_enabled():
            ui.success(f"Chunk {chunk_num} atualizado e reproduzido com sucesso!")
        else:
            ui.success(f"Chunk {chunk_num} atualizado (sound OFF — audio not played).")

    # ------------------------------------------------------------------ #
    def delete_last_chunk(self):
        """Delete the last chunk from the database, history, and disk cache."""
        with self.history_lock:
            if not self.history:
                ui.warn("Nenhuma tradução no histórico para apagar.")
                return False
            n, heard, translated, audio_path = self.history[-1]
        return self.delete_chunk(n)

    def delete_chunk(self, chunk_num):
        """Delete a specific chunk by its chunk number from database, history, and disk cache."""
        target_chunk = None
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    target_chunk = (n, heard, translated, audio_path)
                    break

        if target_chunk is None:
            ui.warn(f"Chunk {chunk_num} não encontrado no histórico para apagar.")
            return False

        n, heard, translated, audio_path = target_chunk

        # Remove audio file from disk
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
                if self.cfg.VERBOSE:
                    ui.dim(f"[chunk {n}] [debug] Arquivo de áudio deletado do disco.")
            except Exception as exc:
                ui.error(f"[chunk {n}] Erro ao deletar arquivo de áudio físico: {exc}")

        # Remove chunk from SQLite database
        try:
            db.delete_chunk(self.session_id, chunk_num)
            if self.cfg.VERBOSE:
                ui.dim(f"[chunk {n}] [debug] Registro deletado do SQLite.")
        except Exception as exc:
            ui.error(f"[chunk {n}] Erro ao deletar do banco de dados: {exc}")

        # Remove chunk from RAM structures
        with self.history_lock:
            self.history = [item for item in self.history if item[0] != chunk_num]
            self.full_transcript = [
                item for item in self.full_transcript if item[0] != chunk_num
            ]
            self.favorites = [item for item in self.favorites if item[0] != chunk_num]
            if self.comments is not None:
                self.comments.pop(int(chunk_num), None)

        ui.success(f"Chunk {chunk_num} removido com sucesso!")
        return True

    def _ensure_chunk_audio(self, n, heard, translated, audio_path):
        """
        Return (audio, sample_rate, path) for a history chunk.

        If the WAV is missing (sound-OFF + TTS_SKIP_WHEN_MUTED), synthesize
        from the stored translation, write cache, and update history/DB.
        """
        path = (audio_path or "").strip() or os.path.join(
            self.cache_dir, f"chunk_{n}.wav"
        )

        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                audio, rate = sf.read(path, dtype="float32")
                if audio is not None and len(audio) > 0:
                    return audio, rate, path
            except Exception as exc:
                ui.warn(f"[chunk {n}] cache de áudio inválido, regenerando: {exc}")

        text = (translated or "").strip()
        if not text:
            ui.warn(f"Chunk {n} sem texto traduzido — não dá para gerar áudio.")
            return None

        ui.info(f"Gerando áudio do chunk {n} (sem cache; TTS sob demanda)...")
        try:
            if hasattr(self.synthesizer, "begin_utterance"):
                self.synthesizer.begin_utterance()
            tts_audio, sample_rate = self.synthesizer.synthesize(text)
        except Exception as exc:
            ui.error(f"[chunk {n}] TTS falhou: {exc}")
            return None

        if tts_audio is None or len(tts_audio) == 0:
            ui.warn(f"[chunk {n}] TTS retornou áudio vazio.")
            return None

        try:
            sf.write(path, tts_audio, sample_rate)
        except Exception as exc:
            ui.error(f"[chunk {n}] erro ao salvar WAV: {exc}")
            return None

        try:
            db.update_chunk(self.session_id, n, heard, translated, path)
        except Exception as exc:
            ui.error(f"[chunk {n}] erro ao atualizar DB com áudio: {exc}")

        with self.history_lock:
            for idx, (cn, h, t, p) in enumerate(self.history):
                if cn == n:
                    self.history[idx] = (n, h, t, path)
                    break

        return tts_audio, sample_rate, path

    def replay_last(self, *, use_heard=False):
        with self.history_lock:
            if not self.history:
                ui.warn("Nenhuma tradução no histórico para repetir.")
                return
            n, heard, translated, audio_path = self.history[-1]
        self.replay_chunk(n, use_heard=use_heard)

    def replay_chunk(self, chunk_num, *, use_heard=False):
        """
        Replay chunk N.

        use_heard=False (r / rN): play translated TTS (target language).
        use_heard=True  (rs / rsN): synthesize and play **Heard** (source text)
        with a SOURCE-language voice; separate cache (does not overwrite r WAV).
        """
        target_chunk = None
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    target_chunk = (n, heard, translated, audio_path)
                    break

        if target_chunk is None:
            ui.warn(
                f"Chunk {chunk_num} não encontrado no histórico "
                f"(não existe ou já foi descartado)."
            )
            return

        n, heard, translated, audio_path = target_chunk
        # r / rN / rs / rsN always need live playback — force sound ON.
        if not self.is_sound_enabled():
            self.set_sound_enabled(True)
            ui.dim("Som ligado automaticamente ([s]) para o replay.")

        if use_heard:
            ui.info(f"Repetindo áudio Heard (source) do chunk {n}...")
            self._print_replay_texts(heard, translated, emphasize="heard")
            result = self._ensure_heard_audio(n, heard)
            label = "Heard"
        else:
            cached = bool(audio_path and os.path.isfile(audio_path))
            ui.info(f"Repetindo áudio do chunk {n}...")
            self._print_replay_texts(heard, translated, emphasize="translated")
            result = self._ensure_chunk_audio(n, heard, translated, audio_path)
            label = "target"
            if result is not None and not cached:
                ui.success(f"Áudio do chunk {n} gerado e pronto para tocar.")

        if result is None:
            return
        audio, rate, path = result
        # Always show folder+file (cache hit never prints "Gerando áudio…")
        self._print_audio_path_line(path, chunk_num=n)
        try:
            self._resume_chunk_playback()
            self._enqueue_playback(audio, rate, pre_tts_cue=True)
        except Exception as exc:
            ui.error(f"Erro ao enfileirar áudio {label} do chunk {n}: {exc}")
            return
        # Blank after replay block (after "…gerado e pronto para tocar." when present)
        ui.raw("")

    def _ensure_heard_audio(self, n, heard):
        """
        Return (audio, sample_rate, path) synthesizing the **Heard** text.

        Uses a separate cache file chunk_{n}_heard.wav so it never overwrites
        the normal translated chunk_{n}.wav used by [r]/[rN].
        Temporarily switches TTS voice to a SOURCE_LANG default when possible.
        """
        text = (heard or "").strip()
        if not text:
            ui.warn(f"Chunk {n} sem texto Heard — não dá para gerar áudio source.")
            return None

        path = os.path.join(self.cache_dir, f"chunk_{n}_heard.wav")
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                audio, rate = sf.read(path, dtype="float32")
                if audio is not None and len(audio) > 0:
                    return audio, rate, path
            except Exception as exc:
                ui.warn(f"[chunk {n}] cache Heard inválido, regenerando: {exc}")

        ui.info(f"Gerando áudio Heard do chunk {n} (TTS source)...")
        try:
            from .synthesize import default_edge_voice_for_lang

            src_lang = getattr(self.cfg, "SOURCE_LANG", "en") or "en"
            src_voice = default_edge_voice_for_lang(src_lang)
        except Exception:
            src_voice = None

        tts_audio, sample_rate = self._synthesize_with_optional_voice(text, src_voice)
        if tts_audio is None or len(tts_audio) == 0:
            ui.warn(f"[chunk {n}] TTS Heard retornou áudio vazio.")
            return None

        try:
            sf.write(path, tts_audio, sample_rate)
        except Exception as exc:
            ui.error(f"[chunk {n}] erro ao salvar WAV Heard: {exc}")
            # Still allow playback from memory
            return tts_audio, sample_rate, path

        ui.success(f"Áudio Heard do chunk {n} gerado e pronto para tocar.")
        return tts_audio, sample_rate, path

    @staticmethod
    def _print_audio_path_line(path, chunk_num=None):
        """Print full host path with chunk ref on Sistema (TUI)."""
        if not path:
            return
        try:
            display = ui.resolve_share_path(path) or str(path)
        except Exception:
            display = str(path)
        display = (display or "").strip()
        if not display:
            return
        n = chunk_num
        if n is not None:
            ui.print_audio_ref(n, display)
        else:
            # Fallback without number
            try:
                import os

                base = os.path.basename(display.replace("\\", "/"))
            except Exception:
                base = ""
            prefix = f"[Chunk ?] " if base else ""
            ui.dim(f"{prefix}audio: {display}", panel="app")
            if base:
                ui.dim(f"{prefix}arquivo: {base}", panel="app")

    def _synthesize_with_optional_voice(self, text, voice_id=None):
        """
        Synthesize `text`, optionally using `voice_id` then restoring prior voice.

        Prefers mutating the edge engine `.voice` only (hybrid-safe — avoids
        Piper rebind via HybridTTS.set_voice). Returns (audio, rate) or (None, None).
        """
        synth = self.synthesizer
        # Resolve the object that actually holds Edge voice id
        edge = None
        try:
            if hasattr(synth, "edge") and hasattr(synth.edge, "voice"):
                edge = synth.edge
            elif hasattr(synth, "voice"):
                edge = synth
        except Exception:
            edge = None

        old_voice = None
        if voice_id and edge is not None:
            try:
                old_voice = getattr(edge, "voice", None)
                edge.voice = voice_id
            except Exception as exc:
                ui.dim(f"[debug] voz Heard '{voice_id}' não aplicada: {exc}")
                old_voice = None

        try:
            if hasattr(synth, "begin_utterance"):
                try:
                    synth.begin_utterance()
                except Exception:
                    pass
            return synth.synthesize(text)
        except Exception as exc:
            ui.error(f"TTS falhou (Heard): {exc}")
            return None, None
        finally:
            if old_voice is not None and edge is not None:
                try:
                    edge.voice = old_voice
                except Exception:
                    pass

    @staticmethod
    def _print_replay_texts(heard, translated, emphasize="translated"):
        """
        After "Repetindo áudio…", print:
          "source / heard text"
          "target / translated text"   ← green by default

        emphasize="heard": highlight Heard (source replay [rs]).
        """
        heard_s = (heard or "").strip()
        translated_s = (translated or "").strip()
        if not heard_s and not translated_s:
            return
        try:
            from colorama import Fore, Style
        except Exception:
            Fore = Style = None  # type: ignore
        # Prefer TUI rich path when a log sink is active.
        if getattr(ui, "get_log_sink", lambda: None)() is not None:
            try:
                e = getattr(ui, "_rich_escape", None)
                if e is None:

                    def e(s):  # type: ignore
                        return (s or "").replace("[", "\\[")

                if heard_s:
                    if emphasize == "heard":
                        ui.rich(
                            f'[bold yellow]"{e(heard_s)}"[/]  [dim](Heard → TTS)[/]'
                        )
                    else:
                        ui.rich(f'"{e(heard_s)}"')
                if translated_s:
                    if emphasize == "heard":
                        ui.rich(f'[dim]"{e(translated_s)}"[/]')
                    else:
                        ui.rich(f'[bold green]"{e(translated_s)}"[/]')
                return
            except Exception:
                pass
        # Classic terminal
        if heard_s:
            if emphasize == "heard" and Fore is not None and Style is not None:
                print(
                    Fore.YELLOW
                    + Style.BRIGHT
                    + f'"{heard_s}"'
                    + Style.RESET_ALL
                    + "  (Heard → TTS)"
                )
            else:
                print(f'"{heard_s}"')
        if translated_s:
            if emphasize == "heard":
                print(f'"{translated_s}"')
            elif Fore is not None and Style is not None:
                print(Fore.GREEN + Style.BRIGHT + f'"{translated_s}"' + Style.RESET_ALL)
            else:
                print(f'"{translated_s}"')

    # ------------------------------------------------------------------ #
    def start(self):
        self._ensure_executor()
        self._threads = [
            threading.Thread(target=self.recorder.run, name="recorder", daemon=True),
            threading.Thread(target=self._process_loop, name="processor", daemon=True),
            threading.Thread(target=self._playback_loop, name="playback", daemon=True),
            threading.Thread(
                target=self._background_tts_loop, name="tts-background", daemon=True
            ),
            threading.Thread(
                target=self._listen_watchdog_loop,
                name="listen-watchdog",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()
        # Immediate: escuta ON (do not wait for first VAD / first TTS cycle)
        try:
            self.recorder.set_capture_enabled(True)
            ui.listen_ready("entrada — mic aberto", force=True)
        except Exception:
            try:
                self.recorder.set_capture_enabled(True)
            except Exception:
                pass

    def _listen_watchdog_loop(self):
        """
        Every ~0.8s: if policy says escuta ON but gate is closed, force open.
        Does not open during TTS hold/hangover (capture_should_run is False).
        """
        while not self.stop_event.is_set():
            try:
                self.stop_event.wait(0.8)
                if self.stop_event.is_set():
                    break
                # Hangover expired but timer missed? Clear by wall clock.
                try:
                    with self._capture_hold_lock:
                        if (
                            self._capture_hold_count <= 0
                            and float(self._capture_hangover_until or 0.0) > 0
                            and time.monotonic()
                            >= float(self._capture_hangover_until or 0.0)
                        ):
                            self._capture_hangover_until = 0.0
                except Exception:
                    pass
                if not self.capture_should_run():
                    continue
                if not self.recorder.is_capture_enabled():
                    # Stale closed gate (hold leak / race) — hard arm
                    self._arm_listen_after_tts(note="watchdog reabriu escuta")
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        try:
            if self.is_passthrough_active():
                self.set_voice_passthrough(False)
        except Exception:
            pass
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

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
                # Idle tick: still apply deferred [g] if nothing is running.
                self._try_apply_pending_language_swap()
                # Keep escuta alive — heal any stuck capture gate ([n] still wins).
                try:
                    self.sync_capture_gate(log_resume=False)
                except Exception:
                    pass
                continue

            # The recorder forwards device errors through the queue.
            if is_capture_error(item):
                ui.error(f"Audio capture failed: {item.exc}")
                self.stop_event.set()
                break

            n = self._alloc_chunk_num()
            executor = self._ensure_executor()
            if executor is not None:
                executor.submit(self._handle_chunk, item, n)
            else:
                self._handle_chunk(item, n)

    def _try_apply_pending_language_swap(self):
        """If [g] was deferred and the pipeline is idle, apply the swap now."""
        apply_now = False
        with self._lang_swap_lock:
            if (
                self._pending_language_swap
                and self._processor_busy_count == 0
                and self.chunk_queue.empty()
            ):
                self._pending_language_swap = False
                apply_now = True
        if not apply_now:
            return
        result = self.apply_language_swap()
        cb = self._on_language_swapped
        if cb is not None:
            try:
                cb(*result[:3])
            except Exception:
                pass

    def _handle_chunk(self, item, n):
        self._mark_processor_enter()
        try:
            sound_off = not self.is_sound_enabled()
            ordered = self._should_order_chunks(sound_off)

            def _abort(message=None, kind="dim"):
                """Release ordered slot; always return VOZ bar to escuta pronta.

                Silent STT/filter skips used to leave the UI on
                "Fim da fala — processando…" forever (looked like mic dead).
                """
                if ordered:
                    if message:
                        self._finish_chunk_slot(n, _ChunkSkip(message, kind=kind))
                    else:
                        self._finish_chunk_slot(n, None)
                elif message:
                    # Abort/filter notes belong on Sistema (not VOZ phrases)
                    if kind == "warn":
                        ui.warn(message, panel="app")
                    elif kind == "error":
                        ui.error(message, panel="app")
                    else:
                        ui.dim(message, panel="app")
                try:
                    note = (message or "").strip()
                    if note:
                        # Keep note short for the ready line
                        if len(note) > 80:
                            note = note[:77] + "…"
                        ui.listen_ready(note)
                    else:
                        ui.listen_ready("chunk ignorado")
                except Exception:
                    try:
                        ui.pipeline_stage("idle", source="voz")
                    except Exception:
                        pass
                # Always re-assert capture gate after a skip (escuta stays ON)
                try:
                    self.sync_capture_gate(log_resume=False)
                except Exception:
                    pass

            if sound_off and self.cfg.VERBOSE:
                ui.info(f"[chunk {n}] Processing (sound OFF)…", panel="app")

            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] Iniciando processamento do chunk...",
                    panel="app",
                )
            backlog = self.chunk_queue.qsize()
            # Backlog is operational signal — keep always; rest of progress is VERBOSE
            if backlog >= 3:
                ui.warn(
                    f"processing is {backlog} chunks behind — "
                    f"a smaller WHISPER_MODEL would keep up better.",
                    panel="app",
                )

            try:
                self._process_chunk_body(item, n, sound_off, ordered, _abort)
            except Exception as exc:
                ui.error(f"[chunk {n}] processing failed: {exc}", panel="app")
                _abort()
        finally:
            self._mark_processor_leave()

    def _process_chunk_body(self, item, n, sound_off, ordered, _abort):
        sound_on = not sound_off
        # Sistema: wipe prior noise; keep Languages|TTS|Sound|Mic + this chunk only
        try:
            ui.begin_chunk_sistema(n, pipeline=self)
        except Exception:
            pass

        def _log_summary(heared_text, note=""):
            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            total_ms = int((time.perf_counter() - t0) * 1000)
            words = len((heared_text or "").split())
            if self.is_mic_muted():
                escuta = "OFF (mic mutado)"
            elif self.is_output_playing():
                escuta = "OFF (reproduzindo TTS — aguarde ou [x])"
            elif sound_on:
                escuta = "ON (mic ativo + som ON)"
            elif sound_off:
                escuta = "OFF (som mute, apenas texto)"
            else:
                escuta = "OFF"
            ui.dim(
                f"  [chunk {n}] {ts}  📝 Palavras: {words}  |  "
                f"⏱ Total: {total_ms}ms  |  🔊 Escuta ativa: {escuta}"
                + (f"  |  {note}" if note else ""),
                panel="app",
            )

        # --- Speech-to-text (or typed text via enew / edit re-queue) ---
        t0 = time.perf_counter()
        text_only = isinstance(item, str)
        strip_note = ""
        audio_item = item if isinstance(item, np.ndarray) else None

        if text_only:
            # Typed/queued text: skip STT, Whisper, and STT hallucination filters.
            heard = (item or "").strip()
            t1 = t0
            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] Texto tipado (sem STT) — {len(heard)} chars.",
                    panel="app",
                )
        else:
            ts_stt = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            ui.dim(f"  [chunk {n}] ⏱ STT         {ts_stt} — início", panel="app")
            ui.chunk_progress(n, "stt")
            try:
                with self._stt_lock:
                    heard = self.transcriber.transcribe(item)
            except Exception as exc:
                ui.error(f"[chunk {n}] STT failed: {exc}", panel="app")
                _abort()
                return
            t1 = time.perf_counter()
            ts_stt_end = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            stt_ms = int((t1 - t0) * 1000)
            ui.dim(
                f"  [chunk {n}] ⏱ STT         {ts_stt_end} — fim ({stt_ms}ms)",
                panel="app",
            )
            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] STT concluído com sucesso.",
                    panel="app",
                )

            if not heard:
                _abort(
                    f"[chunk {n}] STT vazio — sem fala detectada (escuta segue)",
                    kind="dim",
                )
                return

            # Pure silence-credit lines (whole chunk is "Legenda por …") — drop once.
            if getattr(self.cfg, "STT_HALLUCINATION_FILTER", True) and is_hallucination(
                heard
            ):
                _abort(
                    f'[chunk {n}] STT filtrado (alucinação): "{heard}"',
                    kind="dim",
                )
                return

            # Long utterance + silence tail: strip trailing credit, keep real speech.
            original_heard = heard
            heard, stripped = clean_transcript(heard, self.cfg)
            if stripped and heard and self.cfg.VERBOSE:
                strip_note = (
                    f"[chunk {n}] (removed STT tail hallucination from transcript)"
                )
            if not heard:
                _abort(
                    f'[chunk {n}] STT só alucinação — skipped: "{original_heard}"',
                    kind="dim",
                )
                return

            discard_why = transcript_discard_reason(audio_item, heard, self.cfg)
            if discard_why:
                _abort(
                    f'[chunk {n}] STT descartado ({discard_why}): "{heard}"',
                    kind="dim",
                )
                return

        if not heard:
            _abort(f"[chunk {n}] texto vazio — skipped", kind="dim")
            return

        ui.chunk_progress(n, "heard", detail=heard)

        if sound_on:
            self._resume_chunk_playback()
            self._interrupt_playback()

        # --- Translation (+ optional overlapped TTS) ---
        overlap_tts = self._use_tts_overlap() and sound_on
        t_tts_start = None
        tts_first_audio = None
        time_to_audio = None
        tts_audio = None
        sample_rate = None
        audio_parts = []
        audio_path = os.path.join(self.cache_dir, f"chunk_{n}.wav")

        segment_queue = queue.Queue() if overlap_tts else None
        tail_buffer = []
        merge_tail = getattr(self.cfg, "PIPER_MERGE_TAIL", True)
        feeder = (
            StreamingSegmentFeeder(
                max_chars=self._segment_max_chars(),
                first_chars=getattr(self.cfg, "PIPER_STREAM_FIRST_CHARS", 30),
            )
            if overlap_tts
            else None
        )

        # Pre-TTS cue once per chunk (first segment only), not every sentence.
        pre_tts_cue_remaining = True
        tts_engine_name = str(
            getattr(self.synthesizer, "engine_name", None)
            or getattr(self.cfg, "TTS_ENGINE", "edge")
            or "edge"
        ).lower()
        tts_voice_label = str(
            getattr(self.synthesizer, "voice_label", None)
            or getattr(self.synthesizer, "voice_id", None)
            or getattr(self.cfg, "TTS_VOICE", "")
            or ""
        )

        def on_segment(audio, sample_rate_):
            nonlocal tts_first_audio, time_to_audio, sample_rate, pre_tts_cue_remaining
            now = time.perf_counter()
            if tts_first_audio is None and t_tts_start is not None:
                tts_first_audio = now - t_tts_start
                # Instrument: prove which engine produced first audio + ms
                try:
                    ms = int(tts_first_audio * 1000)
                    ui.dim(
                        f"  [chunk {n}] ⏱ TTS first_chunk {ms}ms · "
                        f"engine={tts_engine_name} · voice={tts_voice_label or '—'}",
                        panel="app",
                    )
                except Exception:
                    pass
            if time_to_audio is None:
                time_to_audio = now - t0
            sample_rate = sample_rate_
            do_cue = pre_tts_cue_remaining
            pre_tts_cue_remaining = False
            self._enqueue_playback(audio, sample_rate_, False, pre_tts_cue=do_cue)

        will_synthesize = sound_on or (sound_off and not self._skip_tts_when_muted())
        if will_synthesize and hasattr(self.synthesizer, "begin_utterance"):
            self.synthesizer.begin_utterance()

        def tts_worker():
            while True:
                segment = segment_queue.get()
                if segment is None:
                    break
                audio, _ = self._synthesize_clause(segment, on_segment)
                if audio is not None and len(audio) > 0:
                    audio_parts.append(audio)

        tts_thread = None
        if overlap_tts:
            tts_thread = threading.Thread(
                target=tts_worker, name=f"tts-{n}", daemon=True
            )
            tts_thread.start()

        def _join_tail(parts):
            return " ".join((p or "").strip() for p in parts if (p or "").strip())

        def enqueue_segments(segments):
            nonlocal t_tts_start
            if not segments:
                return
            if merge_tail:
                if t_tts_start is None:
                    t_tts_start = time.perf_counter()
                    segment_queue.put(segments[0])
                    if len(segments) > 1:
                        tail_buffer.extend(segments[1:])
                else:
                    tail_buffer.extend(segments)
            else:
                for segment in segments:
                    if t_tts_start is None:
                        t_tts_start = time.perf_counter()
                    segment_queue.put(segment)

        # --- Translate (phrase cache → live LLM/Google) ---
        cache = getattr(self, "phrase_cache", None)
        src_lang = (getattr(self.cfg, "SOURCE_LANG", "") or "").lower()
        tgt_lang = (getattr(self.cfg, "TARGET_LANG", "") or "").lower()
        force_live = False
        if cache is not None and cache.enabled:
            force_live = cache.consume_force_next()

        translated = None
        cache_hit = False
        if cache is not None and cache.enabled and not force_live:
            try:
                cached = cache.lookup(src_lang, tgt_lang, heard)
            except Exception:
                cached = None
            if cached:
                translated = cached
                cache_hit = True
                if getattr(self.cfg, "PHRASE_CACHE_LOG", True) or self.cfg.VERBOSE:
                    ui.dim(
                        f"[chunk {n}] cache HIT · {src_lang}→{tgt_lang} · "
                        f'"{(heard or "")[:48]}" → "{(translated or "")[:48]}"',
                        panel="app",
                    )
                    ui.dim(
                        "[pc last] revisar · [pc good]/[pc bad] · [pc force] re-traduzir",
                        panel="app",
                    )

        try:
            ts_tr = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            ui.dim(
                f"  [chunk {n}] ⏱ Tradução    {ts_tr} — início "
                f"({'cache HIT' if cache_hit else 'live LLM/Google'})",
                panel="app",
            )
            ui.chunk_progress(n, "translate")
            if cache_hit:
                # Still show stream-less preview path as a normal translate
                if self._use_streaming_llm_for_chunk():
                    # No token stream on HIT — jump straight to full text UI later
                    pass
            elif self._use_streaming_llm_for_chunk():
                ui.chunk_stream_start(n, heard)

                def on_token(partial):
                    ui.chunk_stream_update(n, partial)
                    # Live TARGET on vcam while tokens stream (if [sub] ON)
                    self._push_webcam_subtitle(partial)
                    if feeder is not None:
                        enqueue_segments(feeder.feed(partial))

                translated = self.translator.translate_stream(heard, on_token=on_token)
            else:
                translated = self.translator.translate(heard)
        except Exception as exc:
            ui.error(
                f'[chunk {n}] translation failed for "{heard}": {exc}',
                panel="app",
            )
            if segment_queue is not None:
                segment_queue.put(None)
            if tts_thread is not None:
                tts_thread.join(timeout=2.0)
            _abort()
            return
        t2 = time.perf_counter()
        ts_tr_end = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        tr_ms = int((t2 - t1) * 1000)
        ui.dim(
            f"  [chunk {n}] ⏱ Tradução    {ts_tr_end} — fim ({tr_ms}ms)",
            panel="app",
        )

        # Store MISS / forced live result into TM
        if (
            cache is not None
            and cache.enabled
            and translated
            and (not cache_hit or force_live)
        ):
            try:
                cache.store(
                    src_lang,
                    tgt_lang,
                    heard,
                    translated,
                    from_force=force_live,
                )
            except Exception:
                pass
            if (
                getattr(self.cfg, "PHRASE_CACHE_LOG", True) or self.cfg.VERBOSE
            ) and not cache_hit:
                ui.dim(
                    f"[chunk {n}] cache MISS · stored live translation",
                    panel="app",
                )
            elif force_live and (
                getattr(self.cfg, "PHRASE_CACHE_LOG", True) or self.cfg.VERBOSE
            ):
                ui.dim(
                    f"[chunk {n}] cache FORCE · live translate overwrote pair",
                    panel="app",
                )

        if self.cfg.VERBOSE:
            ui.dim(
                f"[chunk {n}] [debug] Tradução concluída "
                f"({'cache' if cache_hit else 'live'}).",
                panel="app",
            )

        if not translated:
            if segment_queue is not None:
                segment_queue.put(None)
            if tts_thread is not None:
                tts_thread.join(timeout=2.0)
            if self.cfg.VERBOSE:
                _abort(f"[chunk {n}] (empty translation — skipped)", kind="warn")
            else:
                _abort()
            return

        ui.chunk_progress(n, "translated", detail=translated)

        # Badge on Tradução when cache system is enabled (or this was a HIT)
        cache_badge = None
        if cache is not None and (cache.enabled or cache_hit or force_live):
            cache_badge = bool(cache_hit)

        # Shared stamp for DB + list history (updated when timings finalize).
        record_created_at = self._timestamp_now()
        if sound_on:
            if strip_note:
                ui.dim(strip_note, panel="app")
            if self._use_streaming_llm_for_chunk() and not cache_hit:
                ui.chunk_stream_done(n, heard, translated, from_cache=cache_badge)
            else:
                # HIT has no token stream — always use preview with badge
                ui.chunk_text_preview(n, heard, translated, from_cache=cache_badge)
            # TARGET burn-in as soon as translation is ready (before/during TTS)
            self._push_webcam_subtitle(translated)
            with self.history_lock:
                self.history.append((n, heard, translated, audio_path))
            self._record_transcript(
                n, heard, translated, created_at=record_created_at, timing=None
            )

        def _synthesize_chunk_audio(announce=True):
            nonlocal tts_audio, sample_rate, t_tts_start
            if announce:
                ts_tts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                ui.dim(
                    f"  [chunk {n}] ⏱ TTS         {ts_tts} — início · "
                    f"engine={tts_engine_name} · voice={tts_voice_label or '—'}",
                    panel="app",
                )
                ui.chunk_progress(n, "tts")
            if overlap_tts:
                enqueue_segments(feeder.flush(translated))
                if merge_tail and tail_buffer:
                    segment_queue.put(_join_tail(tail_buffer))
                segment_queue.put(None)
                tts_thread.join()
                if audio_parts:
                    tts_audio = np.concatenate(audio_parts).astype(np.float32)
                return

            t_tts_start = time.perf_counter()
            played_any = False

            if self._use_streaming_tts():

                def on_segment_collect(audio, sample_rate_):
                    nonlocal played_any
                    on_segment(audio, sample_rate_)
                    played_any = True

                tts_audio, sample_rate = self.synthesizer.synthesize_streaming(
                    translated, on_segment_collect
                )
                if tts_audio is not None and not played_any:
                    on_segment(tts_audio, sample_rate)
            else:
                tts_audio, sample_rate = self.synthesizer.synthesize(translated)
                if tts_audio is not None:
                    on_segment(tts_audio, sample_rate)

        def _build_timing(t3, include_tts=True):
            timing = {
                "stt": t1 - t0,
                "translate": t2 - t1,
                "total": (t3 - t0) if t3 is not None else (t2 - t0),
                "translate_cache": bool(cache_hit),
            }
            if include_tts:
                timing["tts_engine"] = tts_engine_name
                if tts_voice_label:
                    timing["tts_voice"] = tts_voice_label
            if include_tts and t3 is not None:
                tts_start = t_tts_start if t_tts_start is not None else t2
                timing["tts"] = t3 - tts_start
                if tts_first_audio is not None:
                    timing["tts_first"] = tts_first_audio
                    timing["tts_first_ms"] = int(tts_first_audio * 1000)
                if time_to_audio is not None:
                    timing["time_to_audio"] = time_to_audio
                if t_tts_start is not None:
                    timing["tts_start"] = t_tts_start - t0
            return timing

        def _finalize_chunk_timings(t3, print_timings=True):
            nonlocal tts_audio, sample_rate
            # Overlap path may only fill audio_parts — assemble before persist.
            if tts_audio is None and audio_parts:
                try:
                    tts_audio = np.concatenate(audio_parts).astype(np.float32)
                except Exception:
                    tts_audio = None
            if tts_audio is None or len(tts_audio) == 0:
                timing = _build_timing(t3, include_tts=True)
                if print_timings:
                    ui.chunk_timings(n, timing, at=record_created_at, audio_path="")
                self._record_transcript(
                    n, heard, translated, created_at=record_created_at, timing=timing
                )
                return

            timing = _build_timing(t3, include_tts=True)
            # Write WAV *before* UI shows the path — background write was
            # racing playback UI and often left the path with no file yet
            # (or failed silently if the process moved on).
            written_path = self._persist_chunk(
                n,
                heard,
                translated,
                tts_audio,
                sample_rate,
                audio_path,
                timing,
                record_created_at,
            )
            path_for_ui = written_path or audio_path or ""
            if print_timings:
                ui.chunk_timings(
                    n,
                    timing,
                    at=record_created_at,
                    audio_path=path_for_ui,
                    audio_pending=False,
                )
            self._record_transcript(
                n, heard, translated, created_at=record_created_at, timing=timing
            )

            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] Áudio persistido em disco; "
                    f"reprodução já enfileirada."
                )

        # --- Text-to-speech ---
        if not sound_on:
            timing = _build_timing(None, include_tts=False)
            if self._skip_tts_when_muted():
                timing["tts_skipped"] = True
                extra = "(sound OFF — só texto, TTS omitido)"
                if ordered:
                    self._release_chunk_result(
                        n, heard, translated, "", timing, extra, note=strip_note
                    )
                    self._persist_text_only(
                        n,
                        heard,
                        translated,
                        timing=timing,
                        created_at=record_created_at,
                    )
                else:
                    self._publish_chunk(
                        n, heard, translated, "", timing, extra, note=strip_note
                    )
                    self._persist_text_only(
                        n,
                        heard,
                        translated,
                        timing=timing,
                        created_at=record_created_at,
                    )
                ui.chunk_progress(n, "ready_text")
                _log_summary(heard)
                # Sound OFF: no Cable playback — re-arm escuta now
                try:
                    self._arm_listen_after_tts()
                except Exception:
                    pass
                if self.cfg.VERBOSE:
                    ui.dim(
                        f"[chunk {n}] [debug] Sound OFF — texto pronto; TTS omitido."
                    )
                return

            def _background_tts():
                ui.chunk_progress(n, "tts_bg")
                _synthesize_chunk_audio(announce=False)
                t3 = time.perf_counter()
                if self.cfg.VERBOSE:
                    ui.dim(
                        f"[chunk {n}] [debug] TTS em background concluído (sound OFF)."
                    )
                _finalize_chunk_timings(t3, print_timings=False)
                # print_timings=False skips path line — log file on Sistema here
                try:
                    p = self.get_audio_path_by_chunk(n) or ""
                    if p:
                        ui.print_audio_ref(n, p)
                except Exception:
                    pass
                ui.chunk_progress(n, "ready", detail="voz em cache")
                if self.cfg.VERBOSE:
                    ui.dim(
                        f"[chunk {n}] [debug] Cache de áudio gravado em background "
                        f"({t3 - t2:.2f}s, sound OFF)."
                    )

            timing["tts_skipped"] = True
            extra = "(sound OFF — TTS em background)"
            if ordered:
                self._release_chunk_result(
                    n, heard, translated, audio_path, timing, extra, note=strip_note
                )
            else:
                self._publish_chunk(
                    n, heard, translated, audio_path, timing, extra, note=strip_note
                )
            # Persist text+partial timing now; background TTS will refresh timing.
            self._persist_text_only(
                n, heard, translated, timing=timing, created_at=record_created_at
            )
            ui.chunk_progress(n, "ready_text", detail="voz em segundo plano")
            _log_summary(heard, note="TTS em background")
            self._enqueue_background_tts(n, _background_tts)
            try:
                self._arm_listen_after_tts()
            except Exception:
                pass
            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] Sound OFF — texto pronto; TTS em background.",
                    panel="app",
                )
            return

        try:
            _synthesize_chunk_audio()
        except Exception as exc:
            ui.error(f"[chunk {n}] TTS failed: {exc}", panel="app")
            return

        t3 = time.perf_counter()
        ts_tts_end = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        tts_ms = int((t3 - (t_tts_start if t_tts_start else t2)) * 1000)
        ui.dim(
            f"  [chunk {n}] ⏱ TTS         {ts_tts_end} — fim ({tts_ms}ms)",
            panel="app",
        )
        if self.cfg.VERBOSE:
            ui.dim(
                f"[chunk {n}] [debug] Síntese de voz (TTS) concluída com sucesso.",
                panel="app",
            )
        _finalize_chunk_timings(t3)
        ui.chunk_progress(n, "ready")
        _log_summary(heard, note="TTS concluído")
        # Do NOT sync/open capture here — playback thread still holds the gate
        # until Cable/monitor play finishes. Opening early → eco TTS + freeze.

    def _persist_chunk(
        self,
        n,
        heard,
        translated,
        tts_audio,
        sample_rate,
        audio_path,
        timing=None,
        created_at=None,
    ):
        """
        Write WAV + SQLite. Returns absolute path written, or '' on failure.

        Ensures cache dir exists and path is absolute so Explorer matches UI.
        """
        if tts_audio is None or len(tts_audio) == 0:
            ui.warn(
                f"[chunk {n}] Sem áudio na memória — WAV não gravado.",
                panel="app",
            )
            return ""

        path = (audio_path or "").strip() or os.path.join(
            self.cache_dir, f"chunk_{n}.wav"
        )
        try:
            path = os.path.abspath(path)
        except OSError:
            pass

        try:
            parent = os.path.dirname(path) or self.cache_dir
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:
            ui.error(
                f"[chunk {n}] Não criou pasta de áudio ({parent}): {exc}",
                panel="app",
            )
            return ""

        rate = int(sample_rate or 24000)
        try:
            # soundfile wants float32 mono/array
            audio = np.asarray(tts_audio, dtype=np.float32)
            if audio.ndim > 1:
                audio = audio.mean(axis=1).astype(np.float32)
            sf.write(path, audio, rate)
        except Exception as exc:
            ui.error(
                f"[chunk {n}] Erro ao salvar arquivo de áudio: {exc}",
                panel="app",
            )
            ui.dim(f"   path={path}", panel="app")
            return ""

        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            ui.error(
                f"[chunk {n}] WAV não encontrado após gravar: {path}",
                panel="app",
            )
            return ""

        if self.cfg.VERBOSE:
            ui.dim(
                f"[chunk {n}] [debug] Áudio WAV gravado "
                f"({os.path.getsize(path)} bytes): {path}",
                panel="app",
            )

        # Keep RAM history pointing at the real path
        with self.history_lock:
            for idx, entry in enumerate(self.history):
                if entry[0] == n:
                    h, t = entry[1], entry[2]
                    self.history[idx] = (n, h, t, path)
                    break

        try:
            created_at = db.upsert_chunk(
                self.session_id,
                n,
                heard,
                translated,
                path,
                timing=timing,
                created_at=created_at,
            )
            self._record_transcript(
                n, heard, translated, created_at=created_at, timing=timing
            )
            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] Metadados gravados com sucesso no SQLite.",
                    panel="app",
                )
        except Exception as exc:
            ui.error(
                f"[chunk {n}] Erro ao salvar no banco de dados: {exc}",
                panel="app",
            )
            # File is on disk even if DB fails
        return path

    # ------------------------------------------------------------------ #
    def _play_monitor_pre_tts_cue(self, player, sample_rate: int) -> None:
        """
        ~1s before Cable TTS: bip on MONITOR only — never Cable/Teams.

        Called only for the first playback item of a chunk (see
        ``pre_tts_cue`` on ``_enqueue_playback``), not for every
        sentence/clause of a long multi-segment utterance.

        - Writes only via monitor_cue (separate OutputStream on headphones).
        - Pauses Player Cable stream for the whole lead window so the bip
          cannot leak into CABLE Input (Teams mic path).
        - Lead after the short bip is wall-clock sleep (no silence pad on a
          long stream that could dual-route on Windows shared mode).
        """
        if not bool(getattr(self.cfg, "TTS_MONITOR_CUE", True)):
            return

        from .monitor_cue import make_double_beep, play_cue_on_headphones

        lead = float(getattr(self.cfg, "TTS_MONITOR_CUE_LEAD_S", 1.0) or 1.0)
        lead = max(0.2, min(3.0, lead))
        sr = int(sample_rate or 24000)
        try:
            cue = make_double_beep(
                sr,
                duration_s=float(
                    getattr(self.cfg, "TTS_MONITOR_CUE_DURATION_S", 0.14) or 0.14
                ),
                freq_hz=float(getattr(self.cfg, "TTS_MONITOR_CUE_FREQ_HZ", 880) or 880),
                amplitude=float(
                    getattr(self.cfg, "TTS_MONITOR_CUE_AMPLITUDE", 0.22) or 0.22
                ),
            )
        except Exception:
            return

        def _clog(msg):
            try:
                ui.info(msg, indent=3, panel="app")
            except Exception:
                pass

        # Prefer index resolved at startup (pipeline.monitor_device)
        mon_idx = self.monitor_device
        mon_spec = str(getattr(self.cfg, "MONITOR_DEVICE", "") or "")
        if mon_idx is None and not mon_spec.strip():
            _clog("TTS cue SKIP: MONITOR_DEVICE vazio")
            return

        # Mute Cable for entire cue+lead window (bip must not reach Teams)
        if player is not None:
            try:
                player.pause_main_output()
            except Exception:
                pass
        t0 = time.perf_counter()
        try:
            play_cue_on_headphones(
                cue,  # short bip only — no silence pad on the stream
                sr,
                monitor_index=mon_idx,
                monitor_spec=mon_spec,
                cable_index=self.output_device,
                log=_clog,
            )
            # Remaining lead: sleep while Cable stays paused (silent)
            remaining = lead - (time.perf_counter() - t0)
            if remaining > 0.01:
                time.sleep(remaining)
        finally:
            if player is not None:
                try:
                    player.resume_main_output()
                except Exception:
                    pass

    def _playback_loop(self):
        """Send synthesized audio to the VB-Cable output device."""
        try:
            while not self.stop_event.is_set():
                try:
                    item = self.playback_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if item is INTERRUPT:
                    with self._player_lock:
                        player = self._player
                    if player is not None:
                        player.interrupt()
                    continue

                # (audio, sr, interruptible[, pre_tts_cue])
                pre_tts_cue = False
                if len(item) == 4:
                    audio, sample_rate, interruptible, pre_tts_cue = item
                    pre_tts_cue = bool(pre_tts_cue)
                elif len(item) == 3:
                    audio, sample_rate, interruptible = item
                else:
                    audio, sample_rate = item
                    interruptible = False

                # Drop leftovers after [x] / sound-off / voice bypass (queue race).
                if self._is_tts_output_blocked():
                    continue

                # Create/get player under lock, but NEVER hold lock during play()
                # so interrupt() can fire mid-write. Never open Player while
                # bypass owns Cable (would mix TTS into Teams mic path).
                with self._player_lock:
                    if self.is_passthrough_active():
                        continue
                    if self._player is None:
                        try:
                            self._player = Player(
                                self.output_device,
                                sample_rate,
                                self.monitor_device,
                                block_ms=getattr(self.cfg, "PLAYBACK_BLOCK_MS", 40),
                                monitor_full_playback=bool(
                                    getattr(self.cfg, "MONITOR_PLAYBACK", False)
                                ),
                            )
                        except Exception as exc:
                            ui.error(
                                f"Could not open output device "
                                f"#{self.output_device}: {exc}"
                            )
                            self.stop_event.set()
                            break
                    player = self._player

                # clear + re-check suppress/bypass so concurrent [x]/[b] is not
                # undone by play()'s default clear.
                player.clear_interrupt()
                if self._is_tts_output_blocked():
                    player.interrupt()
                    continue

                self._hold_capture_for_playback()
                try:
                    try:
                        ui.pipeline_stage("play", source="voz")
                    except Exception:
                        pass
                    # Heads-up bip once per chunk (first segment only), never
                    # again for each sentence/clause of a long utterance.
                    if pre_tts_cue:
                        self._play_monitor_pre_tts_cue(player, sample_rate)
                    if self._is_tts_output_blocked() or player.is_interrupted():
                        continue
                    player.play(
                        audio,
                        sample_rate,
                        interruptible=interruptible,
                        clear=False,
                    )
                except Exception as exc:
                    ui.error(f"playback failed: {exc}")
                finally:
                    try:
                        # Back to listening after Cable Out
                        ui.pipeline_stage("idle", source="voz")
                    except Exception:
                        pass
                    # release → hangover (if mute-capture) → _arm_listen_after_tts
                    self._release_capture_after_playback()
        finally:
            self._force_release_capture_hold()
            with self._player_lock:
                if self._player is not None:
                    self._player.close()
                    self._player = None

    # ------------------------------------------------------------------ #
    def add_synonym(self, word, explanation):
        """Add synonym search log to database and local memory."""
        try:
            db.insert_synonym(self.session_id, word, explanation)
            if self.cfg.VERBOSE:
                ui.dim(f"[debug] Sinônimo '{word}' gravado com sucesso no SQLite.")
        except Exception as exc:
            ui.error(f"Erro ao salvar sinônimo no banco de dados: {exc}")

        with self.history_lock:
            self.synonyms.append((word, explanation))

    def get_synonyms(self):
        """Retrieve copy of all synonym search logs for this session."""
        with self.history_lock:
            return list(self.synonyms)

    # ------------------------------------------------------------------ #
    def add_favorite(self, chunk_num):
        """Add a specific chunk to the session favorites (SQLite & RAM)."""
        # Find chunk in history
        target_chunk = None
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    target_chunk = (chunk_num, heard, translated)
                    break

        if target_chunk is None:
            ui.warn(f"Chunk {chunk_num} não encontrado no histórico para favoritar.")
            return False

        chunk_num, heard, translated = target_chunk

        # Check if already favorited
        with self.history_lock:
            for n, _, _ in self.favorites:
                if n == chunk_num:
                    ui.warn(f"Chunk {chunk_num} já está nos favoritos.")
                    return False

        # Save to SQLite DB
        try:
            db.insert_favorite(self.session_id, chunk_num, heard, translated)
            if self.cfg.VERBOSE:
                ui.dim(f"[debug] Chunk {chunk_num} gravado nos favoritos do SQLite.")
        except Exception as exc:
            ui.error(f"Erro ao salvar favorito no banco de dados: {exc}")
            return False

        # Add to memory
        with self.history_lock:
            self.favorites.append((chunk_num, heard, translated))

        ui.success(f"Chunk {chunk_num} adicionado aos favoritos com sucesso! ⭐")
        return True

    def get_favorites(self):
        """Retrieve a copy of all favorited sentences for this session."""
        with self.history_lock:
            return list(self.favorites)

    def get_comments_map(self):
        """chunk_num → list of (id, comment_text, created_at)."""
        with self.history_lock:
            return {int(k): list(v) for k, v in (self.comments or {}).items()}

    def get_comments_for_chunk(self, chunk_num):
        """List of (id, comment_text, created_at) for one chunk."""
        with self.history_lock:
            return list((self.comments or {}).get(int(chunk_num), []))

    def add_comment(self, chunk_num, comment_text):
        """
        Append a free-text comment to a chunk (SQLite + RAM).
        Returns (id, created_at) or (None, None) on failure.
        """
        text = (comment_text or "").strip()
        if not text:
            return None, None

        # Ensure chunk exists in session history / transcript
        found = False
        with self.history_lock:
            for n, *_rest in self.history:
                if n == chunk_num:
                    found = True
                    break
            if not found:
                for entry in self.full_transcript:
                    if entry and entry[0] == chunk_num:
                        found = True
                        break
        if not found:
            ui.warn(
                f"Chunk {chunk_num} não encontrado nesta sessão — "
                f"não dá para comentar.",
            )
            return None, None

        try:
            comment_id, created_at = db.insert_chunk_comment(
                self.session_id, chunk_num, text
            )
        except Exception as exc:
            ui.error(f"Erro ao salvar comentário no banco: {exc}")
            return None, None

        with self.history_lock:
            self.comments.setdefault(int(chunk_num), []).append(
                (int(comment_id), text, created_at)
            )
        return int(comment_id), created_at

    def delete_comment(self, comment_id):
        """
        Delete comment by primary key id (no confirmation).
        Returns True if deleted.
        """
        try:
            deleted = db.delete_chunk_comment(self.session_id, int(comment_id))
        except Exception as exc:
            ui.error(f"Erro ao excluir comentário #{comment_id}: {exc}")
            return False
        if not deleted:
            ui.warn(f"Comentário #{comment_id} não encontrado nesta sessão.")
            return False
        chunk_num, text = deleted
        with self.history_lock:
            lst = (self.comments or {}).get(int(chunk_num), [])
            self.comments[int(chunk_num)] = [
                item for item in lst if int(item[0]) != int(comment_id)
            ]
            if not self.comments.get(int(chunk_num)):
                self.comments.pop(int(chunk_num), None)
        preview = (text or "").strip()
        if len(preview) > 60:
            preview = preview[:57] + "…"
        ui.success(f"Comentário #{comment_id} removido (chunk {chunk_num}): {preview}")
        return True
