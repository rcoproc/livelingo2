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
from .playback import INTERRUPT, Player
from .stt_filter import (
    clean_transcript,
    is_hallucination,
    should_discard_transcript,
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
    ):
        self.cfg = config
        self.input_device = input_device
        self.output_device = output_device
        self.monitor_device = monitor_device
        self.session_id = session_id

        self.transcriber = transcriber
        self.translator = translator
        self.synthesizer = synthesizer

        self.chunk_queue = queue.Queue()
        self.playback_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.recorder = Recorder(
            config,
            input_device,
            self.chunk_queue,
            self.stop_event,
            on_listening=on_listening,
            paragraph_split_enabled=self._should_paragraph_split,
            shorter_end_enabled=lambda: not self.is_sound_enabled(),
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

        self._chunk_count = max_chunk
        self._next_release = max_chunk + 1
        self._pending_chunks = {}
        self._chunk_num_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._threads = []
        self._executor = None
        self._player = None
        self._player_lock = threading.Lock()
        self.sound_enabled = True
        self.sound_lock = threading.Lock()
        self._bg_tts_queue = queue.Queue()
        self._stt_lock = threading.Lock()
        self._playback_suppressed = False
        self._playback_suppress_lock = threading.Lock()

    def is_sound_enabled(self):
        with self.sound_lock:
            return self.sound_enabled

    def set_sound_enabled(self, enabled):
        with self.sound_lock:
            self.sound_enabled = bool(enabled)

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
            self.sound_enabled = not self.sound_enabled
            enabled = self.sound_enabled
        if not enabled:
            self._playback_suppressed = True
            self._interrupt_playback()
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

    def _ensure_executor(self):
        if not self._use_parallel_processing():
            return None
        if self._executor is None:
            workers = max(1, getattr(self.cfg, "SOUND_OFF_WORKERS", 2))
            self._executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="chunk-worker"
            )
        return self._executor

    def _enqueue_playback(self, audio, sample_rate, interruptible=False):
        if not self.is_sound_enabled():
            return
        with self._playback_suppress_lock:
            if self._playback_suppressed:
                return
        self.playback_queue.put((audio, sample_rate, interruptible))

    def stop_playback(self):
        """Stop current TTS and drop queued audio for this utterance."""
        if not self.is_sound_enabled():
            return False
        with self._playback_suppress_lock:
            self._playback_suppressed = True
        self._interrupt_playback()
        return True

    def _resume_chunk_playback(self):
        """Allow TTS for the next (or current) chunk after stop or interrupt."""
        with self._playback_suppress_lock:
            self._playback_suppressed = False

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

    def _interrupt_playback(self):
        if not getattr(self.cfg, "PLAYBACK_INTERRUPT", True):
            return
        with self._player_lock:
            if self._player is not None:
                self._player.interrupt()
        while True:
            try:
                item = self.playback_queue.get_nowait()
            except queue.Empty:
                break
            if item is not INTERRUPT:
                continue

    def _use_streaming_llm(self):
        return (
            getattr(self.cfg, "STREAMING_LLM", False)
            and hasattr(self.translator, "translate_stream")
        )

    def _use_streaming_tts(self):
        if getattr(self.synthesizer, "supports_live_streaming", False):
            return True
        return (
            getattr(self.cfg, "STREAMING_TTS", False)
            and hasattr(self.synthesizer, "synthesize_streaming")
        )

    def _use_tts_overlap(self):
        return (
            getattr(self.cfg, "STREAMING_TTS_OVERLAP", True)
            and self._use_streaming_llm()
            and self._use_streaming_tts()
            and hasattr(self.synthesizer, "synthesize_clause")
        )

    def _should_paragraph_split(self):
        if not getattr(self.cfg, "PARAGRAPH_SPLIT", True):
            return False
        if getattr(self.cfg, "PARAGRAPH_SPLIT_SOUND_OFF_ONLY", True):
            return not self.is_sound_enabled()
        return True

    def _use_parallel_processing(self):
        return (
            not self.is_sound_enabled()
            and getattr(self.cfg, "SOUND_OFF_PARALLEL", True)
        )

    def _use_streaming_llm_for_chunk(self):
        return self._use_streaming_llm() and not self._use_parallel_processing()

    def _skip_tts_when_muted(self):
        return (
            not self.is_sound_enabled()
            and getattr(self.cfg, "TTS_SKIP_WHEN_MUTED", True)
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

    def _publish_chunk(
        self, n, heard, translated, audio_path, timing, timing_extra="", note=""
    ):
        if note:
            ui.dim(note)
        if self._use_streaming_llm_for_chunk():
            ui.chunk_stream_done(n, heard, translated)
        else:
            ui.chunk_text_preview(n, heard, translated)
        if timing:
            ui.chunk_timings(n, timing, extra=timing_extra)
        created_at = self._timestamp_now()
        with self.history_lock:
            self.history.append((n, heard, translated, audio_path))
        self._record_transcript(n, heard, translated, created_at=created_at, timing=timing)

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
            if (
                self._pending_chunks
                and self._next_release not in self._pending_chunks
            ):
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
            db.update_chunk(self.session_id, chunk_num, new_text, translated, audio_path)
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
        )

        self._resume_chunk_playback()
        self._enqueue_playback(tts_audio, sample_rate)
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
            self.favorites = [
                item for item in self.favorites if item[0] != chunk_num
            ]

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

    def replay_last(self):
        with self.history_lock:
            if not self.history:
                ui.warn("Nenhuma tradução no histórico para repetir.")
                return
            n, heard, translated, audio_path = self.history[-1]
        self.replay_chunk(n)

    def replay_chunk(self, chunk_num):
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
        if not self.is_sound_enabled():
            ui.warn("Sound is OFF — pressione [s] para ligar o som e usar [r]/rN].")
            return

        cached = bool(audio_path and os.path.isfile(audio_path))
        if cached:
            ui.info(f"Repetindo áudio do chunk {n}...")
        result = self._ensure_chunk_audio(n, heard, translated, audio_path)
        if result is None:
            return
        audio, rate, path = result
        if not cached:
            ui.success(f"Áudio do chunk {n} gerado e pronto para tocar.")
        try:
            self._resume_chunk_playback()
            self._enqueue_playback(audio, rate)
        except Exception as exc:
            ui.error(f"Erro ao enfileirar áudio do chunk {n}: {exc}")

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
        ]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self.stop_event.set()
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

    def _handle_chunk(self, item, n):
        sound_off = not self.is_sound_enabled()
        ordered = self._should_order_chunks(sound_off)

        def _abort(message=None, kind="dim"):
            """Release ordered slot; defer filter messages so they print in chunk order."""
            if ordered:
                if message:
                    self._finish_chunk_slot(n, _ChunkSkip(message, kind=kind))
                else:
                    self._finish_chunk_slot(n, None)
            elif message:
                if kind == "warn":
                    ui.warn(message)
                elif kind == "error":
                    ui.error(message)
                else:
                    ui.dim(message)

        if sound_off:
            ui.info(f"[chunk {n}] Processing (sound OFF)…")

        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] Iniciando processamento do chunk...")
        backlog = self.chunk_queue.qsize()
        if backlog >= 3:
            ui.warn(
                f"processing is {backlog} chunks behind — "
                f"a smaller WHISPER_MODEL would keep up better."
            )

        try:
            self._process_chunk_body(item, n, sound_off, ordered, _abort)
        except Exception as exc:
            ui.error(f"[chunk {n}] processing failed: {exc}")
            _abort()

    def _process_chunk_body(self, item, n, sound_off, ordered, _abort):
        sound_on = not sound_off

        # --- Speech-to-text ---
        t0 = time.perf_counter()
        try:
            if isinstance(item, str):
                heard = item
            else:
                with self._stt_lock:
                    heard = self.transcriber.transcribe(item)
        except Exception as exc:
            ui.error(f"[chunk {n}] STT failed: {exc}")
            _abort()
            return
        t1 = time.perf_counter()
        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] STT concluído com sucesso.")

        if not heard:
            _abort(f"[chunk {n}] (no speech detected — skipped)", kind="warn")
            return

        audio_item = item if isinstance(item, np.ndarray) else None
        strip_note = ""

        # Pure silence-credit lines (whole chunk is "Legenda por …") — drop once.
        if getattr(self.cfg, "STT_HALLUCINATION_FILTER", True) and is_hallucination(
            heard
        ):
            _abort(
                f'[chunk {n}] (filtered STT hallucination — skipped): "{heard}"'
            )
            return

        # Long utterance + silence tail: strip trailing credit, keep real speech.
        original_heard = heard
        heard, stripped = clean_transcript(heard, self.cfg)
        if stripped and heard:
            strip_note = (
                f"[chunk {n}] (removed STT tail hallucination from transcript)"
            )
        if not heard:
            _abort(
                f'[chunk {n}] (only hallucination in STT — skipped): '
                f'"{original_heard}"'
            )
            return

        if should_discard_transcript(audio_item, heard, self.cfg):
            _abort(
                f'[chunk {n}] (filtered STT hallucination — skipped): "{heard}"'
            )
            return

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

        def on_segment(audio, sample_rate_):
            nonlocal tts_first_audio, time_to_audio, sample_rate
            now = time.perf_counter()
            if tts_first_audio is None and t_tts_start is not None:
                tts_first_audio = now - t_tts_start
            if time_to_audio is None:
                time_to_audio = now - t0
            sample_rate = sample_rate_
            self._enqueue_playback(audio, sample_rate_, False)

        will_synthesize = sound_on or (
            sound_off and not self._skip_tts_when_muted()
        )
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

        try:
            if self._use_streaming_llm_for_chunk():
                ui.chunk_stream_start(n, heard)

                def on_token(partial):
                    ui.chunk_stream_update(n, partial)
                    if feeder is not None:
                        enqueue_segments(feeder.feed(partial))

                translated = self.translator.translate_stream(heard, on_token=on_token)
            else:
                translated = self.translator.translate(heard)
        except Exception as exc:
            ui.error(f'[chunk {n}] translation failed for "{heard}": {exc}')
            if segment_queue is not None:
                segment_queue.put(None)
            if tts_thread is not None:
                tts_thread.join(timeout=2.0)
            _abort()
            return
        t2 = time.perf_counter()
        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] Tradução concluída com sucesso.")

        if not translated:
            if segment_queue is not None:
                segment_queue.put(None)
            if tts_thread is not None:
                tts_thread.join(timeout=2.0)
            _abort(f"[chunk {n}] (empty translation — skipped)", kind="warn")
            return

        # Shared stamp for DB + list history (updated when timings finalize).
        record_created_at = self._timestamp_now()
        if sound_on:
            if strip_note:
                ui.dim(strip_note)
            if self._use_streaming_llm_for_chunk():
                ui.chunk_stream_done(n, heard, translated)
            else:
                ui.chunk_text_preview(n, heard, translated)
            with self.history_lock:
                self.history.append((n, heard, translated, audio_path))
            self._record_transcript(
                n, heard, translated, created_at=record_created_at, timing=None
            )

        def _synthesize_chunk_audio():
            nonlocal tts_audio, sample_rate, t_tts_start
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
            }
            if include_tts and t3 is not None:
                tts_start = t_tts_start if t_tts_start is not None else t2
                timing["tts"] = t3 - tts_start
                if tts_first_audio is not None:
                    timing["tts_first"] = tts_first_audio
                if time_to_audio is not None:
                    timing["time_to_audio"] = time_to_audio
                if t_tts_start is not None:
                    timing["tts_start"] = t_tts_start - t0
            return timing

        def _finalize_chunk_timings(t3, print_timings=True):
            if tts_audio is None:
                return
            timing = _build_timing(t3, include_tts=True)
            if print_timings:
                ui.chunk_timings(n, timing)
            self._record_transcript(
                n, heard, translated, created_at=record_created_at, timing=timing
            )
            threading.Thread(
                target=self._persist_chunk,
                args=(
                    n,
                    heard,
                    translated,
                    tts_audio,
                    sample_rate,
                    audio_path,
                    timing,
                    record_created_at,
                ),
                name=f"persist-{n}",
                daemon=True,
            ).start()

            if self.cfg.VERBOSE:
                ui.dim(
                    f"[chunk {n}] [debug] Chunk enviado para reprodução; "
                    f"persistência em background."
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
                if self.cfg.VERBOSE:
                    ui.dim(
                        f"[chunk {n}] [debug] Sound OFF — texto pronto; TTS omitido."
                    )
                return

            def _background_tts():
                _synthesize_chunk_audio()
                t3 = time.perf_counter()
                if self.cfg.VERBOSE:
                    ui.dim(
                        f"[chunk {n}] [debug] TTS em background concluído (sound OFF)."
                    )
                _finalize_chunk_timings(t3, print_timings=False)
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
            self._enqueue_background_tts(n, _background_tts)
            if self.cfg.VERBOSE:
                ui.dim(f"[chunk {n}] [debug] Sound OFF — texto pronto; TTS em background.")
            return

        try:
            _synthesize_chunk_audio()
        except Exception as exc:
            ui.error(f"[chunk {n}] TTS failed: {exc}")
            return

        t3 = time.perf_counter()
        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] Síntese de voz (TTS) concluída com sucesso.")
        _finalize_chunk_timings(t3)

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
        """Write WAV + SQLite off the hot path so playback starts sooner."""
        try:
            sf.write(audio_path, tts_audio, sample_rate)
            if self.cfg.VERBOSE:
                ui.dim(f"[chunk {n}] [debug] Áudio WAV gravado em disco com sucesso.")
        except Exception as exc:
            ui.error(f"[chunk {n}] Erro ao salvar arquivo de áudio: {exc}")
            return

        try:
            created_at = db.upsert_chunk(
                self.session_id,
                n,
                heard,
                translated,
                audio_path,
                timing=timing,
                created_at=created_at,
            )
            self._record_transcript(
                n, heard, translated, created_at=created_at, timing=timing
            )
            if self.cfg.VERBOSE:
                ui.dim(f"[chunk {n}] [debug] Metadados gravados com sucesso no SQLite.")
        except Exception as exc:
            ui.error(f"[chunk {n}] Erro ao salvar no banco de dados: {exc}")

    # ------------------------------------------------------------------ #
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
                        if self._player is not None:
                            self._player.interrupt()
                    continue

                if len(item) == 3:
                    audio, sample_rate, interruptible = item
                else:
                    audio, sample_rate = item
                    interruptible = False

                with self._player_lock:
                    if self._player is None:
                        try:
                            self._player = Player(
                                self.output_device,
                                sample_rate,
                                self.monitor_device,
                                block_ms=getattr(self.cfg, "PLAYBACK_BLOCK_MS", 40),
                            )
                        except Exception as exc:
                            ui.error(
                                f"Could not open output device "
                                f"#{self.output_device}: {exc}"
                            )
                            self.stop_event.set()
                            break
                    try:
                        self._player.play(
                            audio, sample_rate, interruptible=interruptible
                        )
                    except Exception as exc:
                        ui.error(f"playback failed: {exc}")
        finally:
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
