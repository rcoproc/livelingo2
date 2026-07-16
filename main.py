"""
main.py
=======
Entry point for the real-time FR -> EN voice translator.

    python main.py

Flow: microphone -> Whisper STT (Groq cloud or local faster-whisper)
      -> translation (Groq LLM or Google) -> edge-tts (TTS)
      -> VB-Cable output device (so Teams hears English).

Press Ctrl+C to stop.
"""

import sys
import threading
import datetime
import re
import unicodedata
import time
from colorama import Fore, Style

import numpy as np

import config as cfg
from livelingo import db, devices, ui
from livelingo.groq_transcribe import GroqSTTError, GroqTranscriber
from livelingo.llm import GROQ_KEY_HELP, LLMError, LLMTranslator
from livelingo.pipeline import Pipeline
from livelingo.synthesize import build_synthesizer
from livelingo.transcribe import Transcriber
from livelingo.synonyms import SynonymError, build_synonym_lookup
from livelingo.translate import Translator


class ListeningIndicator:
    def __init__(self):
        self.thread = None
        self._stop_event = threading.Event()
        self.is_speaking = False
        self.is_typing = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=0.5)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def set_speaking(self, state):
        self.is_speaking = state

    def set_typing(self, state):
        self.is_typing = state
        if state:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def _check_kbhit(self):
        try:
            if sys.platform == 'win32':
                import msvcrt
                return msvcrt.kbhit()
            else:
                import select
                r, w, e = select.select([sys.stdin], [], [], 0.0)
                return len(r) > 0
        except Exception:
            return False

    def _animate(self):
        active_frames = [
            "🎙️  [  •      ]",
            "🎙️  [  ••     ]",
            "🎙️  [  •••    ]",
            "🎙️  [   •••   ]",
            "🎙️  [    •••  ]",
            "🎙️  [     ••  ]",
            "🎙️  [      •  ]",
            "🎙️  [     ••  ]",
            "🎙️  [    •••  ]",
            "🎙️  [   •••   ]",
            "🎙️  [  •••    ]",
            "🎙️  [  ••     ]"
        ]
        idle_frames = [
            "🤖 [ •       ]",
            "🤖 [  •      ]",
            "🤖 [   •     ]",
            "🤖 [    •    ]",
            "🤖 [     •   ]",
            "🤖 [      •  ]",
            "🤖 [       • ]",
            "🤖 [      •  ]",
            "🤖 [     •   ]",
            "🤖 [    •    ]",
            "🤖 [   •     ]",
            "🤖 [  •      ]"
        ]
        idx = 0
        while not self._stop_event.is_set():
            if self.is_typing:
                time.sleep(0.2)
                continue

            if self._check_kbhit():
                self.set_typing(True)
                continue

            if self.is_speaking:
                frame = active_frames[idx % len(active_frames)]
                msg = f"\r\033[K{frame} Listening to active voice..."
                delay = 0.12
            else:
                frame = idle_frames[idx % len(idle_frames)]
                msg = f"\r\033[K{frame} Waiting for speech... (Type any command)"
                delay = 0.25

            sys.stdout.write(msg)
            sys.stdout.flush()
            idx += 1
            time.sleep(delay)


def _resolve_input():
    """Resolve the microphone device, exiting on a bad explicit setting."""
    try:
        return devices.resolve_device(cfg.INPUT_DEVICE, "input")
    except ValueError as exc:
        ui.error(f"Input device problem: {exc}")
        sys.exit(1)


def _resolve_output():
    """
    Resolve the output (VB-Cable) device. If it can't be resolved, detect
    whether VB-Cable is installed at all and give the right guidance.
    """
    try:
        idx, name = devices.resolve_device(cfg.OUTPUT_DEVICE, "output")
        return idx, name
    except ValueError:
        pass  # fall through to VB-Cable detection below

    vb_idx, vb_name = devices.find_vbcable_output()
    if vb_idx is None:
        ui.error(f"Output device '{cfg.OUTPUT_DEVICE}' was not found.")
        print(devices.VBCABLE_INSTALL_MESSAGE)
        sys.exit(1)
    return vb_idx, vb_name


def _vad_label():
    if not cfg.VAD_ENABLED:
        return "off"
    label = getattr(cfg, "VAD_MODE", "energy")
    if getattr(cfg, "ROLLING_CHUNKS", False):
        label += "+rolling"
    return label


def _tts_menu_label():
    engine = (getattr(cfg, "TTS_ENGINE", "edge") or "edge").lower()
    if engine == "hybrid" or (
        engine == "piper" and getattr(cfg, "TTS_HYBRID", False)
    ):
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        return f"hybrid (edge+piper / {voice})"
    if engine == "piper":
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        return f"piper ({voice})"
    return f"edge ({cfg.TTS_VOICE})"


def _print_streaming_info():
    if not (
        getattr(cfg, "STREAMING_LLM", False)
        or getattr(cfg, "STREAMING_TTS", False)
    ):
        return
    ui.dim(
        f"   streaming: LLM={'on' if cfg.STREAMING_LLM else 'off'} | "
        f"TTS={'on' if cfg.STREAMING_TTS else 'off'} | "
        f"playback_interrupt={'on' if cfg.PLAYBACK_INTERRUPT else 'off'}"
    )


def _print_device_overview(in_idx, in_name, out_idx, out_name):
    """Print the full device list (compact) and confirm the selected ones."""
    ui.info("Detected audio devices (idx / in-ch / out-ch / name):")
    for idx, name, in_ch, out_ch, _hostapi in devices.summary_rows():
        marker = ""
        if idx == in_idx:
            marker = "  <= INPUT"
        elif idx == out_idx:
            marker = "  <= OUTPUT"
        ui.dim(f"   {idx:>3}  {in_ch:>2}/{out_ch:<2}  {name}{marker}")

    print()
    ui.info("Selected devices:")
    ui.device_line("INPUT", in_idx, in_name)
    ui.device_line("OUTPUT", out_idx, out_name)

    if out_name and "cable" not in out_name.lower():
        ui.warn(
            "The output device is not a VB-Cable device. Other apps (Teams/Zoom) "
            "will only hear the translation if you route to VB-Cable."
        )


def _build_translator():
    """
    Pick the translation engine from config and return an object with a
    `.translate(text)` method. For the LLM engine, do a quick self-test so a
    bad key / model name fails fast with a clear message.
    """
    engine = (cfg.TRANSLATION_ENGINE or "auto").lower()
    if engine == "auto":
        engine = "llm" if cfg.GROQ_API_KEY else "google"

    if engine == "llm":
        if not cfg.GROQ_API_KEY:
            ui.error("TRANSLATION_ENGINE=llm but GROQ_API_KEY is empty.")
            print(GROQ_KEY_HELP)
            sys.exit(1)
        translator = LLMTranslator(cfg)
        try:
            sample = translator.translate("Bonjour, ceci est un test.")
        except LLMError as exc:
            ui.error(f"Groq self-test failed: {exc}")
            print(GROQ_KEY_HELP)
            sys.exit(1)
        ui.success(f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).")
        ui.dim(f'   self-test: "Bonjour, ceci est un test." -> "{sample}"')
        return translator

    ui.info(
        "Translation engine: Google (free). Tip: add a free GROQ_API_KEY in "
        ".env for much more natural results."
    )
    return Translator(cfg)


def _build_local_transcriber():
    """Load the local faster-whisper model (auto-downloads on first run)."""
    try:
        transcriber = Transcriber(cfg, log=ui.info)
    except Exception as exc:
        ui.error(f"Could not load the Whisper model '{cfg.WHISPER_MODEL}': {exc}")
        ui.warn("Check your internet connection (first download) and disk space.")
        sys.exit(1)
    ui.success("Whisper model ready (local).")
    return transcriber


def _build_transcriber():
    """
    Pick the speech-to-text engine from config and return an object with a
    `.transcribe(audio)` method. For the Groq engine, do a quick self-test so a
    bad key / model name is caught early; on failure, fall back to local Whisper.
    """
    engine = (cfg.STT_ENGINE or "auto").lower()
    if engine == "auto":
        engine = "groq" if cfg.GROQ_API_KEY else "local"

    if engine == "groq":
        if not cfg.GROQ_API_KEY:
            ui.warn("STT_ENGINE=groq but GROQ_API_KEY is empty — using local Whisper.")
            print(GROQ_KEY_HELP)
            return _build_local_transcriber()

        transcriber = GroqTranscriber(cfg, log=ui.info)
        # Self-test with a short silent clip so a bad key/model fails fast.
        try:
            silence = np.zeros(int(0.5 * cfg.SAMPLE_RATE), dtype=np.float32)
            transcriber.transcribe(silence)
        except GroqSTTError as exc:
            ui.error(f"Groq STT self-test failed: {exc}")
            ui.warn(
                "Falling back to the local Whisper model. Fix GROQ_API_KEY, or "
                "set STT_ENGINE=local to skip this check."
            )
            return _build_local_transcriber()
        ui.success(f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).")
        return transcriber

    return _build_local_transcriber()


def _print_session_duration(start_time, title):
    """Format and print the session duration beautifully."""
    elapsed = time.time() - start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    seconds = int(elapsed % 60)

    time_str = ""
    if hours > 0:
        time_str += f"{hours}h "
    if minutes > 0 or hours > 0:
        time_str += f"{minutes}m "
    time_str += f"{seconds}s"

    print()
    print(Fore.GREEN + "=" * 64)
    print(Fore.GREEN + Style.BRIGHT + " 🏁 SESSION CLOSED SUCCESSFULLY")
    print(Fore.GREEN + "=" * 64)
    print("  Subject: " + Fore.WHITE + Style.BRIGHT + title)
    print("  Session duration: " + Fore.CYAN + Style.BRIGHT + time_str)
    print(Fore.GREEN + "=" * 64)
    print()


def _print_menu(pipeline=None):
    """Print the configuration metadata (if pipeline provided) and the compact terminal menu in English."""
    if pipeline is not None:
        print()
        sound = "ON" if pipeline.is_sound_enabled() else "OFF"
        ui.info(
            f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG}   |   "
            f"TTS: {_tts_menu_label()}   |   "
            f"Sound: {sound}   |   "
            f"VAD: {_vad_label()}"
        )
        _print_streaming_info()

        # Translation Engine status
        from livelingo.llm import LLMTranslator

        if isinstance(pipeline.translator, LLMTranslator):
            ui.success(f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).")
            ui.dim('   self-test: "Bonjour, ceci est un test." -> "Hello, this is a test."')
        else:
            ui.info("Translation engine: Google (free).")

        # Speech-to-text Engine status
        from livelingo.groq_transcribe import GroqTranscriber

        if isinstance(pipeline.transcriber, GroqTranscriber):
            ui.success(f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).")
        else:
            ui.success("Speech-to-text ready (local Whisper).")

        print()
        ui.success(
            f"Listening — speak {cfg.SOURCE_LANG.upper()} now. Press Ctrl+C to stop."
        )

    print()
    ui.info("Terminal Commands:")
    sound_hint = "ON/OFF"
    if pipeline is not None:
        sound_hint = "ON" if pipeline.is_sound_enabled() else "OFF"

    # Fixed-width two-column menu so the "|" divider always lines up.
    left_w = 42
    rows = [
        ("[r]  Replay last (gera TTS se faltar)", "[rN] Replay chunk N (ex: r3, r99)"),
        ("[e]  Edit last sentence", "[eN] Edit specific chunk (ex: e3)"),
        ("[d]  Delete last sentence", "[dN] Delete specific chunk (ex: d3)"),
        ("[f]  Favorite last sentence", "[fN] Favorite specific chunk (ex: f3)"),
        ("[F]  List favorites (Modal)", "[c]  Export history (.md)"),
        (f"[s]  Sound ({sound_hint})", "[o]  Synonyms / Word meaning"),
        ("[x]  Stop playback (interrupt reading)", "[l]  List session messages"),
        ("[v]  Switch/Restart session", "[q]  Exit application (Quit)"),
        ("[m]  Show this menu", ""),
    ]
    for left, right in rows:
        left_pad = left.ljust(left_w)
        if right:
            line = f"  {left_pad} |  {right}"
        else:
            line = f"  {left_pad} |"
        print("\r\033[K" + Fore.CYAN + line + Style.RESET_ALL)
    print("\r\033[K" + "-" * 76)


def _unpack_transcript_entry(entry):
    """
    Normalize full_transcript rows.

    New format: (chunk_num, heard, translated, created_at, timing_dict)
    Legacy:     (chunk_num, heard, translated)
    """
    if not entry:
        return None, "", "", "", {}
    chunk_num = entry[0]
    heard = entry[1] if len(entry) > 1 else ""
    translated = entry[2] if len(entry) > 2 else ""
    created_at = entry[3] if len(entry) > 3 else ""
    timing = entry[4] if len(entry) > 4 else {}
    if not isinstance(timing, dict):
        timing = {}
    return chunk_num, heard or "", translated or "", created_at or "", timing


def _entry_heard(entry):
    return _unpack_transcript_entry(entry)[1]


# Stopwords ignored by export word counter (as requested).
_WORD_COUNT_STOP = frozenset({"e", "a", "ou", "para", "ao", "à"})
_VOWELS_PT = set("aeiouáéíóúâêôãõàäëïöüýy")


def _pt_syllable_count(word):
    """
    Approximate Portuguese syllable count via vowel-group runs.
    Good enough for filtering mono vs multi-syllable words.
    """
    w = unicodedata.normalize("NFC", (word or "").lower())
    w = re.sub(r"[^a-zàáâãäèéêëìíîïòóôõöùúûüçýÿ]", "", w)
    if not w:
        return 0
    count = 0
    prev_vowel = False
    for ch in w:
        is_v = ch in _VOWELS_PT
        if is_v and not prev_vowel:
            count += 1
        prev_vowel = is_v
    return count if count > 0 else 1


def _count_content_words(texts):
    """
    Count words with more than one syllable, excluding short stopwords
    (e, a, ou, para, ao, à, …).
    """
    total = 0
    for text in texts:
        for raw in re.findall(r"[A-Za-zÀ-ÿ]+(?:'[A-Za-zÀ-ÿ]+)?", text or ""):
            # Strip trailing/leading punctuation already handled by regex.
            key = unicodedata.normalize("NFC", raw.lower())
            # Compare stopwords with and without combining marks for "à".
            key_plain = "".join(
                c for c in unicodedata.normalize("NFD", key) if not unicodedata.combining(c)
            )
            if key in _WORD_COUNT_STOP or key_plain in _WORD_COUNT_STOP:
                continue
            if _pt_syllable_count(key) > 1:
                total += 1
    return total


def _input_loop(pipeline, synonym_lookup):
    """
    Read user input from standard input in a daemon thread.
    Supported commands:
      r       -> Replay last chunk (synthesize TTS if no WAV from sound-OFF)
      r<num>  -> Replay chunk <num> (e.g. r5, r99); generate audio if missing
      e       -> Edit the last transcribed chunk
    """
    while not pipeline.stop_event.is_set():
        try:
            line = sys.stdin.readline()
            if not line:
                break
            raw_cmd = line.strip()
            cmd = raw_cmd.lower()
            if not cmd:
                continue

            if raw_cmd == "F":
                favs = pipeline.get_favorites()
                lang_map = {
                    "fr": "Francês",
                    "en": "Inglês",
                    "pt": "Português",
                    "es": "Espanhol",
                    "de": "Alemão",
                    "it": "Italiano",
                }
                src_lang = lang_map.get(cfg.SOURCE_LANG.lower(), cfg.SOURCE_LANG.upper())
                tgt_lang = lang_map.get(cfg.TARGET_LANG.lower(), cfg.TARGET_LANG.upper())
                ui.favorites_popup(favs, src_lang, tgt_lang)
                _print_menu(pipeline)
            elif cmd == "r":
                pipeline.replay_last()
            elif cmd.startswith("r") and cmd[1:].isdigit():
                chunk_num = int(cmd[1:])
                pipeline.replay_chunk(chunk_num)
            elif cmd == "e":
                last_heard = pipeline.get_last_heard()
                if not last_heard:
                    ui.warn("No sentences in history to edit.")
                    continue
                
                has_readline = False
                try:
                    import readline
                    def hook():
                        readline.insert_text(last_heard)
                        readline.redisplay()
                    readline.set_pre_input_hook(hook)
                    has_readline = True
                except ImportError:
                    ui.warn(
                        "Tip: Install 'pyreadline3' (on Windows) or 'gnureadline' (on Linux/macOS) "
                        "to pre-populate text inside the editor."
                    )

                try:
                    if has_readline:
                        new_text = input("Edit sentence: ").strip()
                    else:
                        print(f'Last sentence: "{last_heard}"')
                        print("Enter correction (or Enter to cancel): ", end="", flush=True)
                        new_text = sys.stdin.readline().strip()
                except (KeyboardInterrupt, EOFError):
                    new_text = ""
                finally:
                    if has_readline:
                        readline.set_pre_input_hook(None)

                if new_text and new_text != last_heard:
                    pipeline.chunk_queue.put(new_text)
                    ui.info("New sentence queued for translation!")
                elif not new_text:
                    ui.info("Editing canceled.")
                else:
                    ui.info("No changes made.")
            elif cmd.startswith("e") and cmd[1:].isdigit():
                chunk_num = int(cmd[1:])
                last_heard = pipeline.get_heard_by_chunk(chunk_num)
                if not last_heard:
                    ui.warn(f"Chunk {chunk_num} not found in history to edit.")
                    continue

                has_readline = False
                try:
                    import readline
                    def hook():
                        readline.insert_text(last_heard)
                        readline.redisplay()
                    readline.set_pre_input_hook(hook)
                    has_readline = True
                except ImportError:
                    ui.warn(
                        "Tip: Install 'pyreadline3' (on Windows) or 'gnureadline' (on Linux/macOS) "
                        "to pre-populate text inside the editor."
                    )

                try:
                    if has_readline:
                        new_text = input(f"Edit sentence {chunk_num}: ").strip()
                    else:
                        print(f'Sentence of chunk {chunk_num}: "{last_heard}"')
                        print("Enter correction (or Enter to cancel): ", end="", flush=True)
                        new_text = sys.stdin.readline().strip()
                except (KeyboardInterrupt, EOFError):
                    new_text = ""
                finally:
                    if has_readline:
                        readline.set_pre_input_hook(None)

                if new_text and new_text != last_heard:
                    pipeline.edit_chunk(chunk_num, new_text)
                elif not new_text:
                    ui.info("Editing canceled.")
                else:
                    ui.info("No changes made.")
            elif cmd == "d":
                last_heard = pipeline.get_last_heard()
                if not last_heard:
                    ui.warn("No sentences in history to delete.")
                    continue
                print(f'Last sentence: "{last_heard}"')
                print("Are you sure you want to delete this sentence? (y/n): ", end="", flush=True)
                confirm = sys.stdin.readline().strip().lower()
                if confirm in ("y", "yes", "s", "sim"):
                    pipeline.delete_last_chunk()
                else:
                    ui.info("Deletion canceled.")
            elif cmd.startswith("d") and cmd[1:].isdigit():
                chunk_num = int(cmd[1:])
                last_heard = pipeline.get_heard_by_chunk(chunk_num)
                if not last_heard:
                    ui.warn(f"Chunk {chunk_num} not found in history to delete.")
                    continue
                print(f'Sentence of chunk {chunk_num}: "{last_heard}"')
                print(f"Are you sure you want to delete sentence {chunk_num}? (y/n): ", end="", flush=True)
                confirm = sys.stdin.readline().strip().lower()
                if confirm in ("y", "yes", "s", "sim"):
                    pipeline.delete_chunk(chunk_num)
                else:
                    ui.info("Deletion canceled.")
            elif cmd == "f":
                last_heard = pipeline.get_last_heard()
                if not last_heard:
                    ui.warn("No sentences in history to favorite.")
                    continue
                with pipeline.history_lock:
                    n = pipeline.history[-1][0]
                pipeline.add_favorite(n)
            elif cmd.startswith("f") and cmd[1:].isdigit():
                chunk_num = int(cmd[1:])
                pipeline.add_favorite(chunk_num)
            elif cmd == "s":
                enabled = pipeline.toggle_sound()
                if enabled:
                    ui.success(
                        "Sound ON — próximas traduções tocam. "
                        "Use [r] / [rN] para ouvir chunks sem áudio (gera TTS se faltar)."
                    )
                else:
                    ui.warn(
                        "Sound OFF — só texto (TTS omitido se TTS_SKIP_WHEN_MUTED). "
                        "Ligue [s] e use [r]/[rN] para gerar e tocar áudio depois."
                    )
                _print_menu(pipeline)
            elif cmd == "x":
                if pipeline.stop_playback():
                    ui.info("Playback stopped — remaining audio for this chunk skipped.")
                else:
                    ui.warn("Sound is OFF — nothing playing to stop.")
            elif cmd == "o":
                print("Enter a word in English: ", end="", flush=True)
                word = sys.stdin.readline().strip()
                if not word:
                    continue
                ui.info(f"Searching meaning and synonyms for '{word}'...")
                try:
                    explanation = synonym_lookup.explain(word)
                    pipeline.add_synonym(word, explanation)
                    ui.synonyms_result(word, explanation)
                except SynonymError as exc:
                    ui.error(f"Synonym lookup failed: {exc}")
                except Exception as exc:
                    ui.error(f"Error searching synonyms: {exc}")
            elif cmd == "c":
                full_trans = pipeline.get_full_transcript()
                if not full_trans:
                    ui.warn("No conversations recorded in this session to export.")
                    continue

                print("Enter the title/subject for the file: ", end="", flush=True)
                title = sys.stdin.readline().strip()
                if not title:
                    ui.info("Share operation canceled.")
                    continue

                # Remove accents and normalize to form slug
                normalized = (
                    unicodedata.normalize("NFKD", title)
                    .encode("ascii", "ignore")
                    .decode("ascii")
                )
                slug = re.sub(r"[^\w\s-]", "", normalized.lower())
                slug = re.sub(r"[-\s]+", "-", slug).strip("-_")

                date_str = datetime.date.today().strftime("%Y-%m-%d")
                filename = f"{date_str}_{slug}.md"

                # Map language codes to Portuguese names
                lang_map = {
                    "fr": "Francês",
                    "en": "Inglês",
                    "pt": "Português",
                    "es": "Espanhol",
                    "de": "Alemão",
                    "it": "Italiano",
                }
                src_lang = lang_map.get(cfg.SOURCE_LANG.lower(), cfg.SOURCE_LANG.upper())
                tgt_lang = lang_map.get(cfg.TARGET_LANG.lower(), cfg.TARGET_LANG.upper())

                # Generate AI summary if GROQ_API_KEY is available
                summary_text = ""
                if cfg.GROQ_API_KEY:
                    ui.info("Analyzing transcription and generating AI executive summary...")
                    
                    # Decouple summary generator from active translation engine:
                    # Use existing translator if it's an LLMTranslator, otherwise spin up a temp one
                    from livelingo.llm import LLMTranslator
                    summary_generator = pipeline.translator
                    if not hasattr(summary_generator, "generate_meeting_summary"):
                        summary_generator = LLMTranslator(cfg)

                    # Concatenate all original heard lines for analysis
                    transcript_full = "\n".join(
                        f"- {_entry_heard(e)}" for e in full_trans
                    )
                    try:
                        summary_text = summary_generator.generate_meeting_summary(transcript_full)
                    except Exception as exc:
                        ui.error(f"Could not generate AI summary: {exc}")
                else:
                    ui.warn("Note: AI summary disabled (requires GROQ_API_KEY to be set in .env).")

                synonyms = pipeline.get_synonyms()

                try:
                    with open(filename, "w", encoding="utf-8") as f:
                        f.write(f"# {title}\n\n")
                        
                        if summary_text:
                            f.write(f"{summary_text}\n\n")
                            f.write("---\n\n")  # horizontal rule before content
                        
                        f.write("## 💬 Transcrição Detalhada\n\n")
                        for entry in full_trans:
                            chunk_num, heard, translated, _created_at, _timing = (
                                _unpack_transcript_entry(entry)
                            )
                            f.write(f"### Chunk {chunk_num}\n")
                            f.write(f"{tgt_lang}: {translated}\n")
                            f.write("\n")
                            f.write(f"{src_lang}: {heard}\n")
                            f.write("\n")
                        
                        # Export synonym vocab searches chronologically
                        if synonyms:
                            f.write("## 📚 Vocabulário e Sinônimos Consultados\n\n")
                            for word, explanation in synonyms:
                                f.write(f"### {word.upper()}\n")
                                f.write(f"{explanation}\n\n")

                        word_count = _count_content_words(
                            _entry_heard(e) for e in full_trans
                        )
                        f.write("---\n")
                        f.write(f"**Total de frases traduzidas:** {len(full_trans)}\n")
                        f.write(f"**Total de sinônimos consultados:** {len(synonyms)}\n")
                        f.write(
                            f"**Total de palavras** (fonte; >1 sílaba; "
                            f"sem e/a/ou/para/ao/à): {word_count}\n"
                        )
                    ui.success(f"File generated and exported successfully: '{filename}'")
                except Exception as exc:
                    ui.error(f"Error saving share file: {exc}")
            elif cmd == "l":
                full_trans = pipeline.get_full_transcript()
                if not full_trans:
                    ui.warn("No messages recorded in this session.")
                    continue

                print()
                print(Fore.CYAN + "=" * 64)
                print(Fore.CYAN + Style.BRIGHT + " CURRENT SESSION HISTORY (Chronological)")
                print(Fore.CYAN + "=" * 64)

                lang_map = {
                    "fr": "Francês",
                    "en": "Inglês",
                    "pt": "Português",
                    "es": "Espanhol",
                    "de": "Alemão",
                    "it": "Italiano",
                }
                src_lang = lang_map.get(cfg.SOURCE_LANG.lower(), cfg.SOURCE_LANG.upper())
                tgt_lang = lang_map.get(cfg.TARGET_LANG.lower(), cfg.TARGET_LANG.upper())

                for entry in full_trans:
                    chunk_num, heard, translated, created_at, timing = (
                        _unpack_transcript_entry(entry)
                    )
                    prefix = f"[Chunk {chunk_num}] "
                    indent = " " * len(prefix)

                    # 1. Target (translated) — main content
                    print(
                        Fore.YELLOW
                        + Style.BRIGHT
                        + prefix
                        + Fore.BLUE
                        + Style.BRIGHT
                        + f"{tgt_lang}: "
                        + Style.RESET_ALL
                        + Fore.WHITE
                        + translated
                    )
                    # 2. Source (heard) — main content
                    print(
                        indent
                        + Fore.WHITE
                        + f"{src_lang}: "
                        + Fore.GREEN
                        + heard
                        + Style.RESET_ALL
                    )
                    # 3. Meta (timing + timestamp) below source, separated by a blank line
                    timing_line = ui.format_timing_line(timing)
                    if timing_line or created_at:
                        print()
                        if timing_line:
                            print(indent + Style.DIM + timing_line + Style.RESET_ALL)
                        if created_at:
                            print(
                                indent
                                + Style.DIM
                                + f"registrado: {created_at}"
                                + Style.RESET_ALL
                            )
                    print()

                print(Fore.CYAN + "=" * 64)
                print(Fore.CYAN + Style.BRIGHT + f" Total translated sentences: {len(full_trans)}")
                print(Fore.CYAN + "=" * 64)
                print()
            elif cmd == "v":
                print("Are you sure you want to switch or restart the session? (y/n): ", end="", flush=True)
                confirm = sys.stdin.readline().strip().lower()
                if confirm in ("y", "yes", "s", "sim"):
                    pipeline.switch_session = True
                    pipeline.stop()
                    break
                else:
                    ui.info("Operation canceled.")
            elif cmd == "m":
                _print_menu(pipeline)
            elif cmd in ("q", "quit"):
                ui.info("Stopping application...")
                pipeline.stop()
                break
            else:
                ui.warn(
                    f"Unknown command: '{cmd}'. Use 'r', 'rN', 'e', 'eN', 'd', 'dN', "
                    f"'f', 'fN', 'F', 's', 'w', 'c', 'l', 'v', 'm' or 'q'."
                )
        except Exception as exc:
            ui.error(f"Error inside input loop: {exc}")
            break


def _select_session():
    """
    Prompt the user to start a new session, resume an existing one, or delete an existing one in English.
    Returns (session_id, session_title)
    """
    from livelingo import db

    db.init_db()

    print()
    ui.info("Select a Session Option:")
    print("  [1] Start a NEW session")
    print("  [2] RESUME a previous session")
    print("  [99] DELETE a previous session (Atomic)")
    print()

    choice = ""
    while choice not in ("1", "2", "99"):
        print("Option (1, 2 or 99): ", end="", flush=True)
        choice = sys.stdin.readline().strip()

    if choice == "2":
        sessions = db.list_sessions(limit=5)
        if not sessions:
            ui.warn("No previous sessions found. Creating a new session...")
            choice = "1"
        else:
            ui.info("Last sessions found:")
            for idx, (sid, title, created_at) in enumerate(sessions, 1):
                print(f"  [{idx}] {title} (ID: {sid}, Created at: {created_at})")
            print()

            sel = None
            while sel is None:
                print(
                    f"Choose session number (1 to {len(sessions)}): ",
                    end="",
                    flush=True,
                )
                sel_str = sys.stdin.readline().strip()
                if sel_str.isdigit():
                    num = int(sel_str)
                    if 1 <= num <= len(sessions):
                        sel = num - 1

            sid, title, _ = sessions[sel]
            ui.success(f"Resuming session: '{title}' (ID: {sid})")
            return sid, title

    elif choice == "99":
        sessions = db.list_sessions(limit=10)
        if not sessions:
            ui.warn("No previous sessions found to delete.")
            return _select_session()

        ui.info("Last sessions found:")
        for idx, (sid, title, created_at) in enumerate(sessions, 1):
            print(f"  [{idx}] {title} (ID: {sid}, Created at: {created_at})")
        print()

        sel = None
        while sel is None:
            print(
                f"Choose session number to DELETE (1 to {len(sessions)}, or Enter to cancel): ",
                end="",
                flush=True,
            )
            sel_str = sys.stdin.readline().strip()
            if not sel_str:
                ui.info("Deletion canceled.")
                return _select_session()
            if sel_str.isdigit():
                num = int(sel_str)
                if 1 <= num <= len(sessions):
                    sel = num - 1

        sid, title, _ = sessions[sel]
        print(f"Are you absolutely sure you want to delete session '{title}' and ALL associated data?")
        print("This operation is IRREVERSIBLE! (y/n): ", end="", flush=True)
        confirm = sys.stdin.readline().strip().lower()

        if confirm in ("y", "yes", "s", "sim"):
            ui.info(f"Starting atomic transaction to delete session '{title}'...")
            try:
                db.delete_session_atomic(sid)
                ui.success(f"Session '{title}' and all its dependencies deleted successfully!")
            except Exception as exc:
                ui.error(f"Error deleting session: {exc}. Rollback executed.")
        else:
            ui.info("Deletion canceled.")

        return _select_session()

    # Choice is 1 (new session)
    print(
        "Enter title/subject for the new session (or Enter for automatic): ",
        end="",
        flush=True,
    )
    title = sys.stdin.readline().strip()
    if not title:
        title = f"Session {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    # Generate unique ID
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Slugify the title to make a clean ID
    normalized = (
        unicodedata.normalize("NFKD", title)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^\w\s-]", "", normalized.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-_")
    session_id = f"{timestamp}_{slug}"

    db.create_session(session_id, title)
    ui.success(f"New session created: '{title}' (ID: {session_id})")
    return session_id, title


def _ensure_wrapper_scripts():
    """Ensure that livelingo.sh and livelingo.bat wrapper scripts exist in the project directory."""
    import os

    sh_path = "livelingo.sh"
    bat_path = "livelingo.bat"

    # 1. Write livelingo.sh if not exists
    if not os.path.exists(sh_path):
        try:
            with open(sh_path, "w", newline="\n", encoding="utf-8") as f:
                f.write("#!/bin/bash\n\n")
                f.write("# ======================================================================= #\n")
                f.write("# LiveLingo Global Execution Script (Linux/WSL/macOS)\n")
                f.write("# ======================================================================= #\n\n")
                f.write('PROJECT_DIR="/mnt/c/Users/rcopr/LiveLingo/LiveLingo"\n\n')
                f.write('cd "$PROJECT_DIR" || {\n')
                f.write('    echo -e "\\033[1;31m[x] Error: Project directory not found ($PROJECT_DIR).\\033[0m"\n')
                f.write('    exit 1\n')
                f.write('}\n\n')
                f.write('python3 main.py "$@"\n')

            # Make it executable
            os.chmod(sh_path, 0o755)
        except Exception:
            pass

    # 2. Write livelingo.bat if not exists
    if not os.path.exists(bat_path):
        try:
            with open(bat_path, "w", newline="\r\n", encoding="utf-8") as f:
                f.write("@echo off\n")
                f.write(":: =======================================================================\n")
                f.write(":: LiveLingo Global Execution Script (Windows)\n")
                f.write(":: =======================================================================\n\n")
                f.write('cd /d "C:\\Users\\rcopr\\LiveLingo\\LiveLingo"\n\n')
                f.write("python main.py %*\n")
        except Exception:
            pass


def main():
    # --- Ensure wrapper scripts are generated locally ---
    _ensure_wrapper_scripts()

    # --- Enable verbose debug logs if --verbose flag is passed ---
    cfg.VERBOSE = "--verbose" in sys.argv

    while True:
        sys.stdout.write("\033[H\033[J")  # Clear screen on startup or restart
        sys.stdout.flush()

        ui.banner()

        # --- Session Setup ---
        session_id, session_title = _select_session()
        session_start_time = time.time()

        # --- Devices ---
        in_idx, in_name = _resolve_input()
        out_idx, out_name = _resolve_output()
        _print_device_overview(in_idx, in_name, out_idx, out_name)

        monitor_idx = None
        if cfg.MONITOR_PLAYBACK:
            if cfg.MONITOR_DEVICE:
                try:
                    monitor_idx, _ = devices.resolve_device(cfg.MONITOR_DEVICE, "output")
                except ValueError as exc:
                    ui.warn(f"Monitor device problem ({exc}); using default output.")
                    monitor_idx = devices.default_output_index()
            else:
                monitor_idx = devices.default_output_index()
            ui.info(f"Monitor playback ON -> {devices.device_name(monitor_idx)}")

        # --- Settings summary ---
        print()
        ui.info(
            f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG}   |   "
            f"TTS: {_tts_menu_label()}   |   "
            f"Sound: ON   |   "
            f"VAD: {_vad_label()}"
        )
        _print_streaming_info()

        # --- Translation engine (validate key/model before the slow model load) ---
        translator = _build_translator()
        synonym_lookup = build_synonym_lookup(cfg, translator, log=ui.info)

        # --- Speech-to-text engine (Groq cloud or local Whisper) ---
        transcriber = _build_transcriber()

        # --- TTS (edge online or Piper local) ---
        synthesizer = build_synthesizer(cfg, log=ui.info)

        indicator = ListeningIndicator()

        def on_listening(is_speaking):
            if is_speaking:
                indicator.start()
            else:
                indicator.stop()

        # --- Build and start the pipeline ---
        pipeline = Pipeline(
            config=cfg,
            input_device=in_idx,
            output_device=out_idx,
            transcriber=transcriber,
            translator=translator,
            synthesizer=synthesizer,
            session_id=session_id,
            monitor_device=monitor_idx,
            on_listening=on_listening,
        )

        print()
        ui.success("Listening — speak French now. Press Ctrl+C to stop.")
        _print_menu()

        pipeline.start()

        # Start terminal command listener in a daemon thread
        cmd_thread = threading.Thread(
            target=_input_loop,
            args=(pipeline, synonym_lookup),
            name="input_listener",
            daemon=True,
        )
        cmd_thread.start()

        try:
            # Block here until Ctrl+C or a switch session event stops us.
            while not pipeline.stop_event.is_set():
                pipeline.stop_event.wait(0.2)
        except KeyboardInterrupt:
            print()
            ui.info("Ctrl+C received — shutting down...")
            pipeline.stop()
            cmd_thread.join(timeout=2.0)
            break
        finally:
            pipeline.stop()
            pipeline.join(timeout=5.0)
            cmd_thread.join(timeout=5.0)
            print("-" * 64)
            ui.success("Stopped. Au revoir!")
            _print_session_duration(session_start_time, session_title)

        # Check if we should switch session, otherwise break and quit
        if getattr(pipeline, "switch_session", False):
            continue
        else:
            break


if __name__ == "__main__":
    main()
