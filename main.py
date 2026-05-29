"""
main.py
=======
Entry point for the real-time FR -> EN voice translator.

    python main.py

Flow: microphone -> faster-whisper (STT) -> deep-translator (FR->EN)
      -> edge-tts (TTS) -> VB-Cable output device (so Teams hears English).

Press Ctrl+C to stop.
"""

import sys

import config as cfg
from livelingo import devices, ui
from livelingo.llm import GROQ_KEY_HELP, LLMError, LLMTranslator
from livelingo.pipeline import Pipeline
from livelingo.synthesize import Synthesizer
from livelingo.transcribe import Transcriber
from livelingo.translate import Translator


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


def main():
    ui.banner()

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
        f"Voice: {cfg.TTS_VOICE}   |   "
        f"VAD: {'on' if cfg.VAD_ENABLED else 'off'}"
    )

    # --- Translation engine (validate key/model before the slow model load) ---
    translator = _build_translator()

    # --- Load the local Whisper model (auto-downloads on first run) ---
    try:
        transcriber = Transcriber(cfg, log=ui.info)
    except Exception as exc:
        ui.error(f"Could not load the Whisper model '{cfg.WHISPER_MODEL}': {exc}")
        ui.warn("Check your internet connection (first download) and disk space.")
        sys.exit(1)
    ui.success("Whisper model ready.")

    # --- TTS ---
    synthesizer = Synthesizer(cfg)

    # --- Build and start the pipeline ---
    pipeline = Pipeline(
        config=cfg,
        input_device=in_idx,
        output_device=out_idx,
        transcriber=transcriber,
        translator=translator,
        synthesizer=synthesizer,
        monitor_device=monitor_idx,
    )

    print()
    ui.success("Listening — speak French now. Press Ctrl+C to stop.")
    print("-" * 64)

    pipeline.start()
    try:
        # Block here until Ctrl+C or a fatal capture error sets the stop event.
        while not pipeline.stop_event.is_set():
            pipeline.stop_event.wait(0.2)
    except KeyboardInterrupt:
        print()
        ui.info("Ctrl+C received — shutting down...")
    finally:
        pipeline.stop()
        pipeline.join(timeout=5.0)
        print("-" * 64)
        ui.success("Stopped. Au revoir!")


if __name__ == "__main__":
    main()
