"""
pipeline.py
===========
Orchestrates the full pipeline with three threads connected by queues:

    [recorder thread]  mic -> chunk_queue
    [processor thread] chunk_queue -> STT -> translate -> TTS -> playback_queue
    [playback thread]  playback_queue -> VB-Cable output device

All threads are daemons and watch a shared `stop_event` for clean shutdown.
"""

import os
import queue
import threading
import time
import soundfile as sf

from . import db, ui
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
            config, input_device, self.chunk_queue, self.stop_event, on_listening=on_listening
        )

        self.history = []
        self.history_lock = threading.Lock()
        self.full_transcript = []

        # Ensure cache directory exists
        self.cache_dir = os.path.join(".cache", "audio_sessions", session_id)
        os.makedirs(self.cache_dir, exist_ok=True)

        # Load existing chunks if resuming a session
        existing_chunks = db.load_session_chunks(session_id)
        max_chunk = 0
        for chunk_num, heard_text, translated_text, audio_path in existing_chunks:
            self.full_transcript.append((chunk_num, heard_text, translated_text))
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
        self._threads = []

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

            # Update self.full_transcript
            for idx, (n, heard, translated_old) in enumerate(self.full_transcript):
                if n == chunk_num:
                    self.full_transcript[idx] = (chunk_num, new_text, translated)
                    break

        ui.chunk_status(
            chunk_num,
            new_text,
            translated,
            {"stt": 0.0, "translate": 0.0, "tts": 0.0, "total": 0.0},
        )

        # Play the new audio
        self.playback_queue.put((tts_audio, sample_rate))
        ui.success(f"Chunk {chunk_num} atualizado e reproduzido com sucesso!")

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

    def replay_last(self):
        with self.history_lock:
            if not self.history:
                ui.warn("Nenhuma tradução no histórico para repetir.")
                return
            n, heard, translated, audio_path = self.history[-1]

        ui.info(f"Repetindo áudio do chunk {n}...")
        try:
            audio, rate = sf.read(audio_path, dtype="float32")
            self.playback_queue.put((audio, rate))
        except Exception as exc:
            ui.error(f"Erro ao ler áudio do disco para o chunk {n}: {exc}")

    def replay_chunk(self, chunk_num):
        target_chunk = None
        with self.history_lock:
            for n, heard, translated, audio_path in self.history:
                if n == chunk_num:
                    target_chunk = (n, heard, translated, audio_path)
                    break

        if target_chunk is None:
            ui.warn(
                f"Chunk {chunk_num} não encontrado no histórico (não existe ou já foi descartado)."
            )
            return

        n, heard, translated, audio_path = target_chunk
        ui.info(f"Repetindo áudio do chunk {n}...")
        try:
            audio, rate = sf.read(audio_path, dtype="float32")
            self.playback_queue.put((audio, rate))
        except Exception as exc:
            ui.error(f"Erro ao ler áudio do disco para o chunk {n}: {exc}")

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

    def _handle_chunk(self, item):
        self._chunk_count += 1
        n = self._chunk_count

        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] Iniciando processamento do chunk...")
        backlog = self.chunk_queue.qsize()
        if backlog >= 3:
            ui.warn(
                f"processing is {backlog} chunks behind — "
                f"a smaller WHISPER_MODEL would keep up better."
            )

        # --- Speech-to-text ---
        t0 = time.perf_counter()
        try:
            if isinstance(item, str):
                heard = item
            else:
                heard = self.transcriber.transcribe(item)
        except Exception as exc:
            ui.error(f"[chunk {n}] STT failed: {exc}")
            return
        t1 = time.perf_counter()
        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] STT concluído com sucesso.")

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
        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] Tradução concluída com sucesso.")

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
        if self.cfg.VERBOSE:
            ui.dim(f"[chunk {n}] [debug] Síntese de voz (TTS) concluída com sucesso.")

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
            audio_path = os.path.join(self.cache_dir, f"chunk_{n}.wav")
            try:
                sf.write(audio_path, tts_audio, sample_rate)
                if self.cfg.VERBOSE:
                    ui.dim(f"[chunk {n}] [debug] Áudio WAV gravado em disco com sucesso.")
            except Exception as exc:
                ui.error(f"[chunk {n}] Erro ao salvar arquivo de áudio: {exc}")
                return

            # Insert chunk metadata into SQLite database
            try:
                db.insert_chunk(self.session_id, n, heard, translated, audio_path)
                if self.cfg.VERBOSE:
                    ui.dim(f"[chunk {n}] [debug] Metadados gravados com sucesso no SQLite.")
            except Exception as exc:
                ui.error(f"[chunk {n}] Erro ao salvar no banco de dados: {exc}")

            with self.history_lock:
                self.history.append((n, heard, translated, audio_path))
                self.full_transcript.append((n, heard, translated))

            self.playback_queue.put((tts_audio, sample_rate))
            if self.cfg.VERBOSE:
                ui.dim(f"[chunk {n}] [debug] Chunk processado e enviado para reprodução.")

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
