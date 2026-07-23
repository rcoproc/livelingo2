"""
command_help.py
===============
Catalog of LiveLingo menu commands for the TUI "Command list" tab.

Descriptions follow SOURCE_LANG. Groups and commands are ordered
alphabetically (group title A–Z, then command token A–Z).
"""

from __future__ import annotations

import unicodedata

# --------------------------------------------------------------------------- #
# Catalog (language-neutral tokens). sort_key drives A–Z within a group.
# --------------------------------------------------------------------------- #


def _alpha_key(text: str) -> str:
    """Case- and accent-insensitive sort key (Áudio → audio)."""
    s = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


# group: audio | idiom | keys | sentence | session
_COMMANDS: list[dict[str, str]] = [
    # --- Audio ---
    {"id": "a", "group": "audio", "token": "a", "sort": "a"},
    {"id": "aN", "group": "audio", "token": "aN", "sort": "an"},
    {"id": "b", "group": "audio", "token": "b", "sort": "b"},
    {"id": "ctts", "group": "audio", "token": "ctts", "sort": "ctts"},
    {"id": "lav", "group": "audio", "token": "lav", "sort": "lav"},
    {"id": "ld", "group": "audio", "token": "ld", "sort": "ld"},
    {"id": "lv", "group": "audio", "token": "lv", "sort": "lv"},
    {"id": "n", "group": "audio", "token": "n", "sort": "n"},
    {"id": "N", "group": "audio", "token": "N", "sort": "n2"},
    {"id": "p", "group": "audio", "token": "p", "sort": "p"},
    {"id": "pN", "group": "audio", "token": "pN", "sort": "pn"},
    {"id": "r", "group": "audio", "token": "r", "sort": "r"},
    {"id": "rN", "group": "audio", "token": "rN", "sort": "rn"},
    {"id": "rs", "group": "audio", "token": "rs", "sort": "rs"},
    {"id": "rsN", "group": "audio", "token": "rsN", "sort": "rsn"},
    {"id": "s", "group": "audio", "token": "s", "sort": "s"},
    {"id": "x", "group": "audio", "token": "x", "sort": "x"},
    # --- Idiom / language ---
    {"id": "g", "group": "idiom", "token": "g", "sort": "g"},
    {"id": "o", "group": "idiom", "token": "o", "sort": "o"},
    {"id": "t", "group": "idiom", "token": "t", "sort": "t"},
    # --- Keyboard ---
    {"id": "ctrl_c", "group": "keys", "token": "Ctrl+C", "sort": "ctrl+c"},
    {"id": "ctrl_q", "group": "keys", "token": "Ctrl+Q", "sort": "ctrl+q"},
    {
        "id": "ctrl_shift_c",
        "group": "keys",
        "token": "Ctrl+Shift+C",
        "sort": "ctrl+shift+c",
    },
    {"id": "f1", "group": "keys", "token": "F1", "sort": "f1"},
    {"id": "f2", "group": "keys", "token": "F2", "sort": "f2"},
    {"id": "f3", "group": "keys", "token": "F3", "sort": "f3"},
    {"id": "f4", "group": "keys", "token": "F4", "sort": "f4"},
    {"id": "f5", "group": "keys", "token": "F5", "sort": "f5"},
    {"id": "search", "group": "keys", "token": "/", "sort": "search"},
    {"id": "search_n", "group": "keys", "token": "/n", "sort": "search-n"},
    {"id": "search_p", "group": "keys", "token": "/p", "sort": "search-p"},
    {"id": "up_down", "group": "keys", "token": "↑ / ↓", "sort": "up-down"},
    # --- Sentence ---
    {"id": "c", "group": "sentence", "token": "c", "sort": "c"},
    {"id": "cls", "group": "sentence", "token": "cls", "sort": "cls"},
    {"id": "cls1", "group": "sentence", "token": "cls1", "sort": "cls1"},
    {"id": "cls2", "group": "sentence", "token": "cls2", "sort": "cls2"},
    {"id": "co", "group": "sentence", "token": "co", "sort": "co"},
    {"id": "coN", "group": "sentence", "token": "coN", "sort": "con"},
    {"id": "codN", "group": "sentence", "token": "codN", "sort": "codn"},
    {"id": "d", "group": "sentence", "token": "d", "sort": "d"},
    {"id": "dN", "group": "sentence", "token": "dN", "sort": "dn"},
    {"id": "e", "group": "sentence", "token": "e", "sort": "e"},
    {"id": "eN", "group": "sentence", "token": "eN", "sort": "en"},
    {"id": "enew", "group": "sentence", "token": "enew", "sort": "enew"},
    {"id": "f", "group": "sentence", "token": "f", "sort": "f"},
    {"id": "fN", "group": "sentence", "token": "fN", "sort": "fn"},
    {"id": "F", "group": "sentence", "token": "F", "sort": "f-list"},
    {"id": "gg", "group": "sentence", "token": "gg / gt", "sort": "gg"},
    {"id": "GG", "group": "sentence", "token": "GG / gf", "sort": "gg2"},
    {"id": "l", "group": "sentence", "token": "l", "sort": "l"},
    {"id": "lc", "group": "sentence", "token": "lc", "sort": "lc"},
    {"id": "lc_hide", "group": "sentence", "token": "lc hide", "sort": "lc-hide"},
    {"id": "lc_off", "group": "sentence", "token": "lc off", "sort": "lc-off"},
    {"id": "lc_on", "group": "sentence", "token": "lc on", "sort": "lc-on"},
    {"id": "lc_show", "group": "sentence", "token": "lc show", "sort": "lc-show"},
    {"id": "lc_status", "group": "sentence", "token": "lc status", "sort": "lc-status"},
    {"id": "cam", "group": "audio", "token": "cam", "sort": "cam"},
    {"id": "cam_off", "group": "audio", "token": "cam off", "sort": "cam-off"},
    {"id": "cam_on", "group": "audio", "token": "cam on", "sort": "cam-on"},
    {"id": "cam_status", "group": "audio", "token": "cam status", "sort": "cam-status"},
    {
        "id": "cam_snap_closed",
        "group": "audio",
        "token": "cam snap closed",
        "sort": "cam-snap-closed",
    },
    {
        "id": "cam_closed",
        "group": "audio",
        "token": "cam closed",
        "sort": "cam-closed",
    },
    {
        "id": "cam_full",
        "group": "audio",
        "token": "cam full",
        "sort": "cam-full",
    },
    {
        "id": "sub",
        "group": "audio",
        "token": "sub",
        "sort": "sub",
    },
    {
        "id": "cam_sub",
        "group": "audio",
        "token": "cam sub",
        "sort": "cam-sub",
    },
    {"id": "lo", "group": "sentence", "token": "lo", "sort": "lo"},
    {"id": "lt", "group": "sentence", "token": "lt", "sort": "lt"},
    # --- Session ---
    {"id": "m", "group": "session", "token": "m", "sort": "m"},
    {"id": "pc", "group": "session", "token": "pc", "sort": "pc"},
    {"id": "pc_backup", "group": "session", "token": "pc backup", "sort": "pc-backup"},
    {"id": "pc_bad", "group": "session", "token": "pc bad", "sort": "pc-bad"},
    {"id": "pc_force", "group": "session", "token": "pc force", "sort": "pc-force"},
    {"id": "pc_good", "group": "session", "token": "pc good", "sort": "pc-good"},
    {"id": "pc_import", "group": "session", "token": "pc import", "sort": "pc-import"},
    {"id": "pc_last", "group": "session", "token": "pc last", "sort": "pc-last"},
    {"id": "pc_off", "group": "session", "token": "pc off", "sort": "pc-off"},
    {"id": "pc_on", "group": "session", "token": "pc on", "sort": "pc-on"},
    {
        "id": "pc_restore",
        "group": "session",
        "token": "pc restore",
        "sort": "pc-restore",
    },
    {"id": "pc_undo", "group": "session", "token": "pc undo", "sort": "pc-undo"},
    {"id": "q", "group": "session", "token": "q", "sort": "q"},
    {"id": "u", "group": "session", "token": "u", "sort": "u"},
    {"id": "v", "group": "session", "token": "v", "sort": "v"},
]

_GROUP_IDS = ("audio", "idiom", "keys", "sentence", "session")

# --------------------------------------------------------------------------- #
# i18n packs: tab title, intro, group titles, per-command title + description
# --------------------------------------------------------------------------- #

_I18N: dict[str, dict[str, str]] = {
    "en": {
        "tab": "Command list",
        "intro": (
            "All LiveLingo menu commands, **grouped** by area. "
            "Groups and commands are ordered **alphabetically**. "
            "Type a command in the box below and press **Enter**."
        ),
        "group_audio": "Audio",
        "group_idiom": "Language",
        "group_keys": "Keyboard",
        "group_sentence": "Sentence",
        "group_session": "Session",
        # Audio
        "title_a": "Copy audio path",
        "desc_a": "Copies the **full** absolute path of the **last** chunk audio file to the clipboard (host path; WSL-friendly; no middle ellipsis).",
        "title_aN": "Copy audio path N",
        "desc_aN": "Copies the **full** absolute path of **chunk N** audio file to the clipboard. Example: `a12`.",
        "title_b": "Voice bypass",
        "desc_b": (
            "Toggle bypass. **First press:** stop any TTS on Cable (same as `[x]`), "
            "then send **raw mic → CABLE** without STT/translation (`BYPASS` header). "
            "**Second press:** leave bypass and resume normal listen/translate/TTS. "
            "Same as **F2** or the white badge. Aliases: `bypass`, `hot`."
        ),
        "title_ctts": "Change TTS voice",
        "desc_ctts": "Change the Edge TTS voice for upcoming synthesis. Use `ctts <ShortName>` (e.g. `ctts en-US-AndrewMultilingualNeural`) or `ctts` alone to be prompted. Validates against the voice catalog.",
        "title_lav": "List all TTS voices",
        "desc_lav": "Runs `edge-tts --list-voices` and prints the full catalog into the log.",
        "title_ld": "List audio devices",
        "desc_ld": "Runs `list_devices.py` and prints input/output devices (indices and names) into the log.",
        "title_lv": "List filtered TTS voices",
        "desc_lv": "Lists Edge TTS voices filtered to common locales (`en-US`, `en-GB`, `es-ES`, `es-MX`, `fr-FR`).",
        "title_n": "Mic mute",
        "desc_n": (
            "Toggle microphone mute (Windows Core Audio when available + app capture gate). "
            "In the TUI a **centered red modal** (white text) appears while muted; "
            "only action: press **n** again (**Cmd n** / desmutar o microfone - Cmd n). "
            "Header also shows MUTED. TUI stays open behind the popup. "
            "Case-sensitive: lowercase **n** only (capital **N** is force soft-listen)."
        ),
        "title_N": "Force soft-listen",
        "desc_N": (
            "Capital **N** only (not mute). Arms **force soft-listen** for translation: "
            "yellow borders on the TUI, VAD accepts **low-volume** speech (no need to speak loudly). "
            "Unmutes the mic if needed. Press **N** again to return to normal energy VAD. "
            "Does not replace **n** (mute)."
        ),
        "title_p": "Open audio folder",
        "desc_p": "Opens the folder of the **last** chunk audio file in the system file manager (Explorer).",
        "title_pN": "Open audio folder N",
        "desc_pN": "Opens the folder of **chunk N** audio file in the file manager. Example: `p5`.",
        "title_r": "Replay last",
        "desc_r": (
            "Replays the **last** chunk **translated** TTS. Turns sound **ON** if it was OFF. "
            "Shows Heard + target; synthesizes on demand if the WAV is missing. "
            "Always prints the full `audio: C:\\…\\chunk_N.wav` path (one line). "
            "Blank line after the block."
        ),
        "title_rN": "Replay chunk N",
        "desc_rN": (
            "Replays **translated** TTS for chunk N (example: `r133`). Turns sound ON if OFF. "
            "Always prints the full WAV path. Blank line after the replay block."
        ),
        "title_rs": "Replay Heard (source) last",
        "desc_rs": (
            "Like `r`, but TTS from **Heard** (source) text with a default `SOURCE_LANG` voice. "
            "Separate file `chunk_N_heard.wav`. Turns sound ON if OFF. "
            "Prints full `audio:` path. Blank line after the block."
        ),
        "title_rsN": "Replay Heard chunk N",
        "desc_rsN": (
            "TTS from **Heard** for chunk N (example: `rs133`). Does not overwrite `r`/`rN` audio. "
            "Turns sound ON if OFF. Prints full `audio:` path."
        ),
        "title_s": "Sound ON/OFF",
        "desc_s": (
            "Toggle live TTS playback. Default is OFF (text-only). When ON, translations play to the output device. "
            "After `[s]`, the menu reprints `Pair … · áudio ON/OFF [s]` plus a blank log line. "
            "`r`/`rs` can auto-enable sound."
        ),
        "title_x": "Stop playback",
        "desc_x": "Stops the current TTS playback and clears the remaining audio queue.",
        # Idiom
        "title_g": "Swap languages",
        "desc_g": "Swap `SOURCE_LANG` ↔ `TARGET_LANG` at runtime (STT, translator, TTS). Does not rewrite old chunks. If a chunk is in flight, the swap may be deferred until idle.",
        "title_o": "Synonyms / meaning",
        "desc_o": "Look up synonyms or meaning for a word/phrase (AI helper). Prompts for the term when used alone.",
        "title_t": "Change TARGET language",
        "desc_t": "Set only the **target** language (EN/PT/ES/FR/DE/IT/ZH/JA). Example: `t EN`. Input is forced to UPPERCASE for this command. Source/STT stay unchanged; TTS voice follows the new target when possible.",
        # Keys
        "title_ctrl_c": "Copy selection",
        "desc_ctrl_c": "Copies the selected text in the active log tab. If nothing is selected, copies the full log of the active tab.",
        "title_ctrl_q": "Quit",
        "desc_ctrl_q": "Quit the application (same idea as command `q`).",
        "title_ctrl_shift_c": "Copy full log",
        "desc_ctrl_shift_c": "Always copies the **entire** content of the focused log pane (Tradução LC or VOZ, Sistema, …) to the clipboard.",
        "title_f1": "Help (F1)",
        "desc_f1": (
            "Prints startup help (banner, devices, engines, tips) into the **Sistema** tab and focuses that tab. "
            "Ends with the same **phrase-cache inventory** shown on Tradução at startup "
            "(pairs, words per language direction, ON/OFF, quality)."
        ),
        "title_f2": "Voice bypass (F2)",
        "desc_f2": (
            "Toggle **voice bypass** (same as command `b` / click the white badge left of the command box). "
            "ON = raw mic → output (no STT/translation). OFF = translated audio path. "
            "Copy full log remains **Ctrl+Shift+C**."
        ),
        "title_f3": "Cycle log tabs (F3)",
        "desc_f3": "Cycles tabs: **Tradução → Sistema → Novidades → Command list → …**.",
        "title_f4": "Compact UI (F4)",
        "desc_f4": "Toggle compact UI (hide the command menu strip; command line stays). Same as `u`.",
        "title_f5": "Auto-scroll Tradução (F5)",
        "desc_f5": (
            "Toggle **auto-scroll** for both Tradução panes (**LC** left + **VOZ** right). "
            "ON (default, green badge) = new lines / post-chunk / command output jump to the bottom. "
            "OFF (amber badge) = viewport stays put so you can read upper history while live lines still append. "
            "Footer key shows `Auto↓ ON` / `Auto↓ OFF`. Click the F5 chip or press **F5**. "
            "`GG`/`gf` still jump to the end once without re-enabling follow when OFF."
        ),
        "title_search": "Search in log (vim-style)",
        "desc_search": (
            "Type `/text` and Enter to search the **focused** log pane (case-insensitive). "
            "On Tradução, click **LC** or **VOZ** first (default VOZ). Also Sistema, Novidades, Command list. "
            "Scrolls to the first match and shows a counter (e.g. `1/7`). "
            "Use `/` alone to re-run the last query on the current pane. "
            "If `/` cannot be typed in your terminal, use aliases: `find text`, `find:text`, or `s?text`."
        ),
        "title_search_n": "Next search match",
        "desc_search_n": (
            "Go to the **next** match of the last `/` search (wraps to the first). "
            "If you switched tabs, re-runs the same query on the active tab. "
            "Does **not** conflict with mic mute `n` (prefix `/` is required)."
        ),
        "title_search_p": "Previous search match",
        "desc_search_p": (
            "Go to the **previous** match of the last `/` search (wraps to the last). "
            "Does **not** conflict with folder command `p` (prefix `/` is required)."
        ),
        "title_up_down": "Command history",
        "desc_up_down": "In the command field, ↑ / ↓ walk previous commands (persisted under `.cache/cmd_history.txt`).",
        # Sentence
        "title_c": "Export Markdown",
        "desc_c": (
            "Export the session to a `.md` file. **Dual channel:** chronological body tags "
            "`[LC N] LiveCaptions` (entrada) vs `[Chunk N] LiveLingo VOZ` (mic+áudio); "
            "VOZ lines include audio path when present. After the body: annexes "
            "**só LiveCaptions** and **só VOZ**. Optional AI executive summary "
            "(auto-chunks long transcripts via `SUMMARY_MAX_INPUT_TOKENS`)."
        ),
        "title_cls": "Clear log",
        "desc_cls": "Clears **Tradução LC**, **Tradução VOZ**, and **Sistema** (not Novidades / Command list). Classic mode clears the terminal.",
        "title_cls1": "Clear LC (left)",
        "desc_cls1": "Clears only the **left** Tradução column — LiveCaptions (`#log-lc`). Use `cls2` for VOZ, `cls` for both + Sistema.",
        "title_cls2": "Clear VOZ (right)",
        "desc_cls2": "Clears only the **right** Tradução column — VOZ mic + commands (`#log`). Use `cls1` for LC, `cls` for both + Sistema.",
        "title_co": "Comment last",
        "desc_co": "Attach a free-text comment to the **last** chunk (stored in SQLite; shown on list `l` with `#id`).",
        "title_coN": "Comment chunk N",
        "desc_coN": "Comment **chunk N**. Forms: `coN` (prompt) or `coN text` (inline). Example: `co3 note here`.",
        "title_codN": "Delete comment #N",
        "desc_codN": "Delete a comment by its primary-key id (`#id` from the list). Example: `cod12`.",
        "title_d": "Delete last",
        "desc_d": "Delete the **last** chunk (history, DB, and audio file when present).",
        "title_dN": "Delete chunk N",
        "desc_dN": "Delete **chunk N** and its dependencies. Example: `d8`.",
        "title_e": "Edit last",
        "desc_e": "Edit the **last** sentence: pre-fills the command field (TUI) or prompts; re-translates and can re-synthesize audio.",
        "title_eN": "Edit chunk N",
        "desc_eN": "Edit **chunk N** the same way as `e`. Example: `e4`.",
        "title_enew": "New text (no mic)",
        "desc_enew": "Queue a new translation from **typed** text only (no microphone/STT). Example: `enew Hello world`. TTS follows current sound mode (`s`).",
        "title_f": "Favorite last",
        "desc_f": "Mark the **last** chunk as a favorite.",
        "title_fN": "Favorite chunk N",
        "desc_fN": "Mark **chunk N** as a favorite. Example: `f2`.",
        "title_F": "List favorites",
        "desc_F": "Show all favorite phrases for this session (popup / list).",
        "title_gg": "Go top",
        "desc_gg": "Jump to the **start** of the active log tab and turn auto-scroll off. Aliases: `gg`, `gt`.",
        "title_GG": "Go bottom",
        "desc_GG": "Jump to the **end** of the active log tab and re-enable auto-scroll. Aliases: `GG` (case-sensitive), `gf`.",
        "title_l": "List messages",
        "desc_l": (
            "List all session phrases (chronological). **Split panes:** "
            "LiveCaptions → **left** log (magenta), LiveLingo VOZ → **right** log (yellow). "
            "Drag the sash to resize; Expandir/Restaurar maximizes a side. "
            "Header shows LC vs VOZ counts. Timing, stamp, audio path, comments (`#id`)."
        ),
        "title_lc": "Live Captions pause/resume",
        "desc_lc": (
            "Toggle **Windows LiveCaptions** translation (strip above the log tabs). "
            "Default **OFF** at launch (`LIVE_CAPTIONS_START_ON_LAUNCH=false`) — VOZ/mic is the active path. "
            "Independent of mic→Whisper. Requires Win11 + `LIVE_CAPTIONS_ENABLED=true` + `uiautomation`. "
            "Also: `lc on` / `lc off` / `lc show` / `lc hide` / `lc status`."
        ),
        "title_lc_on": "Start / resume LiveCaptions",
        "desc_lc_on": "Start LiveCaptions scrape if off, or resume after pause. Aliases: `lc resume`, `lc start`.",
        "title_lc_off": "Pause LiveCaptions",
        "desc_lc_off": "Turn LC OFF (pause strip; mic/VOZ pipeline keeps running). Aliases: `lc pause`, `lc stop`.",
        "title_lc_show": "Show LiveCaptions window",
        "desc_lc_show": "Restore the Windows LiveCaptions window (unhide). Alias: `lc restore`.",
        "title_lc_hide": "Hide LiveCaptions window",
        "desc_lc_hide": "Minimize/hide the Windows LiveCaptions window again (tool-window style).",
        "title_lc_status": "LiveCaptions status",
        "desc_lc_status": "Print service snapshot: status, paused, hidden, last error. Aliases: `lc st`, `lc ?`.",
        "title_cam": "Webcam lip-sync (toggle)",
        "desc_cam": (
            "Toggle **webcam → lip-sync → virtual camera** (Teams/Meet). "
            "Requires `WEBCAM_ENABLED=true` and `opencv-python` + `mediapipe` + `pyvirtualcam`. "
            "TTS audio from Cable Out drives the mouth. See `docs/webcam-lipsync.md`."
        ),
        "title_cam_on": "Webcam on",
        "desc_cam_on": (
            "Start threads (if needed) and open physical cam + virtual cam stream."
        ),
        "title_cam_off": "Webcam off",
        "desc_cam_off": (
            "Release physical cam and OBS Virtual Cam so other apps can use them. "
            "Threads idle; [cam on] re-opens devices (no full app restart)."
        ),
        "title_cam_status": "Webcam status",
        "desc_cam_status": "FPS, face lock, template, engine, resolution, backend, last error.",
        "title_cam_snap_closed": "Snap closed-mouth photo",
        "desc_cam_snap_closed": (
            "Save a **closed-mouth photo template** (idle while mic listens). "
            "Close your mouth, face the camera, then run this. "
            "Files: `.cache/webcam/closed_mouth.png` (+ landmarks JSON)."
        ),
        "title_cam_closed": "Toggle closed mouth (F10)",
        "desc_cam_closed": (
            "**F10** or `cam closed`: show/hide closed-mouth **face plate** on live video. "
            "No auto on mic speech — only F10/F11. `cam closed off` = live again."
        ),
        "title_cam_full": "Full-frame closed freeze (F11)",
        "desc_cam_full": (
            "**F11** or `cam full` / `cam freeze`: fill the **entire** virtual camera "
            "with the closed-mouth photo (hides live video; does **not** fullscreen the TUI). "
            "Press again to restore live. Requires `cam snap closed` template."
        ),
        "title_sub": "VCam subtitle burn-in (TARGET)",
        "desc_sub": (
            "Toggle **burn-in TARGET (translated) text** on OBS Virtual Camera frames. "
            "Stays until **`sub off`** or the **next translation** (no auto-hide). "
            "Pixels only — not Teams/Zoom CC. "
            "`sub on` / `sub off` / `sub status`. Default OFF. `[u]` = compact UI."
        ),
        "title_cam_sub": "VCam subtitle via cam",
        "desc_cam_sub": (
            "Same as `sub`: `cam sub` / `cam sub on` / `cam sub off` — "
            "draw latest **TARGET** translation at the bottom of the virtual-cam frame."
        ),
        "title_lo": "List source only",
        "desc_lo": "List only **source** (heard) lines for the session.",
        "title_lt": "List target only",
        "desc_lt": "List only **target** (translated) lines for the session.",
        # Session
        "title_m": "Show menu",
        "desc_m": "Show the command menu summary again (status + compact command list).",
        "title_pc": "Phrase cache status",
        "desc_pc": (
            "Show phrase-cache stats (hits/misses/hit rate/mem) and the last HIT/MISS/store event. "
            "Full-sentence TM across **all sessions** (not only the current one). "
            "See also `pc on`, `pc off`, `pc force`, `pc last`, `pc good`, `pc bad`, "
            "`pc undo`, `pc backup`, `pc restore`, `pc import`. Env: `PHRASE_CACHE=true`."
        ),
        "title_pc_on": "Enable phrase cache",
        "desc_pc_on": (
            "Turn the translation memory **ON** for this process and warm RAM from SQLite. "
            "Repeated phrases can show **Translated [CACHE]** (magenta) and skip the LLM. "
            "Compare latency with `pc off`."
        ),
        "title_pc_off": "Disable phrase cache",
        "desc_pc_off": (
            "Turn the translation memory **OFF**. Every phrase is translated live (Google/LLM) "
            "and shows **Translated [LIVE]** (cyan) while you compare quality/latency. "
            "Pairs remain in the database; only lookup is disabled."
        ),
        "title_pc_force": "Force next live translate",
        "desc_pc_force": (
            "The **next** chunk ignores a cache HIT: live translate and **overwrite** the stored pair. "
            "Previous target is saved in history for `pc undo`. Use after `pc bad` to fix a wrong CACHE line."
        ),
        "title_pc_last": "Review last cache event",
        "desc_pc_last": (
            "Print the last HIT/MISS/store: source phrase, target phrase, layer (memory/sqlite), quality mark. "
            "Use to judge whether a CACHE result fits the context."
        ),
        "title_pc_good": "Mark last pair good",
        "desc_pc_good": "Mark the last HIT/store pair as **good** quality in SQLite (`pc last` to inspect).",
        "title_pc_bad": "Mark last pair bad",
        "desc_pc_bad": (
            "Mark the last HIT/store as **bad** (out of context). Then `pc force` and re-speak the phrase "
            "to store a better translation, or `pc undo` if an overwrite was wrong."
        ),
        "title_pc_undo": "Undo last overwrite",
        "desc_pc_undo": (
            "Restore the **previous** target text for the last pair from `translation_pairs_history`. "
            "Does not delete the pair; only reverts the target field and RAM entry."
        ),
        "title_pc_backup": "Backup phrase cache",
        "desc_pc_backup": (
            "Write all `translation_pairs` to JSON under `.cache/phrase_cache_backups/` "
            "(timestamped file + `phrase_cache_latest.json`). Safe before import or experiments."
        ),
        "title_pc_restore": "Restore phrase cache",
        "desc_pc_restore": (
            "`pc restore` loads `phrase_cache_latest.json`; `pc restore path.json` loads a specific file. "
            "Auto-backup runs before restore. Merges via upsert (no needless duplicates)."
        ),
        "title_pc_import": "Import CSV into phrase cache",
        "desc_pc_import": (
            "`pc import file.csv` or `pc import file.csv reverse`. Columns: SourceText, TranslatedText, "
            "TargetLanguage. Dedupes by normalized source + language pair. "
            "CLI: `python -m livelingo.import_phrase_csv file.csv [--dry-run] [--also-reverse]`. "
            "Typical exports are EN→PT; use `reverse` for PT→EN hits."
        ),
        "title_q": "Quit",
        "desc_q": "Stop the session and exit. The session id is printed so you can resume later with `livelingo <id>`.",
        "title_u": "Compact UI",
        "desc_u": "Toggle compact TUI: hide the multi-line command menu; keep the command input. Same as **F4**. Aliases: `ui`, `compact`.",
        "title_v": "Switch session",
        "desc_v": "Leave the current session and return to the session picker (new / resume / delete).",
    },
    "pt": {
        "tab": "Lista de comandos",
        "intro": (
            "Todos os comandos do menu do LiveLingo, **agrupados** por área. "
            "Grupos e comandos em ordem **alfabética**. "
            "Digite o comando na caixa abaixo e pressione **Enter**."
        ),
        "group_audio": "Áudio",
        "group_idiom": "Idioma",
        "group_keys": "Teclado",
        "group_sentence": "Frase",
        "group_session": "Sessão",
        "title_a": "Copiar caminho do áudio",
        "desc_a": "Copia o caminho absoluto **completo** do áudio do **último** chunk (host; WSL; sem reticências no meio).",
        "title_aN": "Copiar caminho do áudio N",
        "desc_aN": "Copia o caminho absoluto **completo** do áudio do **chunk N**. Exemplo: `a12`.",
        "title_b": "Bypass de voz",
        "desc_b": (
            "Alterna bypass. **1ª tecla:** corta qualquer TTS no Cable (como `[x]`) e "
            "envia o **mic cru → CABLE** sem STT/tradução (cabeçalho `BYPASS`). "
            "**2ª tecla:** sai do bypass e retoma escuta/tradução/TTS normal. "
            "Igual a **F2** ou o badge branco. Aliases: `bypass`, `hot`."
        ),
        "title_ctts": "Trocar voz TTS",
        "desc_ctts": "Altera a voz Edge TTS para as próximas sínteses. Use `ctts <ShortName>` (ex.: `ctts en-US-AndrewMultilingualNeural`) ou só `ctts` para digitar. Valida no catálogo de vozes.",
        "title_lav": "Listar todas as vozes TTS",
        "desc_lav": "Executa `edge-tts --list-voices` e imprime o catálogo completo no log.",
        "title_ld": "Listar dispositivos de áudio",
        "desc_ld": "Executa `list_devices.py` e lista entradas/saídas (índices e nomes) no log.",
        "title_lv": "Listar vozes TTS filtradas",
        "desc_lv": "Lista vozes Edge TTS filtradas para locales comuns (`en-US`, `en-GB`, `es-ES`, `es-MX`, `fr-FR`).",
        "title_n": "Mudo do microfone",
        "desc_n": (
            "Liga/desliga o mute do microfone (Core Audio no Windows quando disponível + gate do app). "
            "Na TUI abre um **popup vermelho centralizado** (texto branco) enquanto estiver mudo; "
            "única ação: **n** de novo (**desmutar o microfone - Cmd n**). Header mostra MUTED. "
            "Case-sensitive: só **n** minúsculo (**N** maiúsculo = escuta forçada)."
        ),
        "title_N": "Escuta forçada (voz baixa)",
        "desc_N": (
            "Só **N** maiúsculo (não é mute). Liga **escuta forçada** para tradução: "
            "bordas amarelas na TUI, VAD aceita **voz baixa** (sem precisar falar alto). "
            "Desmuta o mic se precisar. **N** de novo volta ao VAD normal. "
            "Não substitui **n** (mute)."
        ),
        "title_p": "Abrir pasta do áudio",
        "desc_p": "Abre a pasta do áudio do **último** chunk no gerenciador de arquivos.",
        "title_pN": "Abrir pasta do áudio N",
        "desc_pN": "Abre a pasta do áudio do **chunk N**. Exemplo: `p5`.",
        "title_r": "Repetir último",
        "desc_r": (
            "Repete o TTS **traduzido** do **último** chunk. Liga o som se estiver OFF. "
            "Mostra Heard + target; sintetiza se faltar WAV. "
            "Sempre imprime o path completo `audio: C:\\…\\chunk_N.wav` (uma linha). "
            "Linha em branco após o bloco."
        ),
        "title_rN": "Repetir chunk N",
        "desc_rN": (
            "Repete o TTS **traduzido** do chunk N (ex.: `r133`). Liga o som se OFF. "
            "Sempre imprime o path completo do WAV. Linha em branco após o bloco."
        ),
        "title_rs": "Repetir Heard (source) último",
        "desc_rs": (
            "Como `r`, mas TTS do texto **Heard** com voz padrão de `SOURCE_LANG`. "
            "Arquivo separado `chunk_N_heard.wav`. Liga o som se OFF. "
            "Imprime o path completo `audio:`. "
        ),
        "title_rsN": "Repetir Heard chunk N",
        "desc_rsN": (
            "TTS do **Heard** do chunk N (ex.: `rs133`). Não sobrescreve o áudio de `r`/`rN`. "
            "Liga o som se OFF. Imprime o path completo `audio:`."
        ),
        "title_s": "Som ON/OFF",
        "desc_s": (
            "Liga/desliga TTS ao vivo. Padrão OFF (só texto). Depois de `[s]`, reimprime "
            "`Pair … · áudio ON/OFF [s]` e uma linha em branco. `r`/`rs` podem religar o som."
        ),
        "title_x": "Parar reprodução",
        "desc_x": "Interrompe o TTS atual e esvazia a fila de áudio restante.",
        "title_g": "Trocar idiomas",
        "desc_g": "Inverte `SOURCE_LANG` ↔ `TARGET_LANG` em tempo real (STT, tradutor, TTS). Não reescreve chunks antigos. Se houver chunk em andamento, a troca pode ser adiada.",
        "title_o": "Sinônimos / significado",
        "desc_o": "Consulta sinônimos ou significado de uma palavra/frase (assistente IA). Pede o termo se usado sozinho.",
        "title_t": "Mudar idioma TARGET",
        "desc_t": "Define só o idioma **alvo** (EN/PT/ES/FR/DE/IT/ZH/JA). Exemplo: `t EN`. Entrada forçada em MAIÚSCULAS neste comando. Source/STT permanecem; a voz TTS acompanha o novo alvo quando possível.",
        "title_ctrl_c": "Copiar seleção",
        "desc_ctrl_c": "Copia o texto selecionado na aba de log ativa. Sem seleção, copia o log inteiro da aba ativa.",
        "title_ctrl_q": "Sair",
        "desc_ctrl_q": "Encerra a aplicação (mesmo espírito do comando `q`).",
        "title_ctrl_shift_c": "Copiar log inteiro",
        "desc_ctrl_shift_c": "Sempre copia **todo** o conteúdo do painel de log focado (Tradução LC ou VOZ, Sistema, …).",
        "title_f1": "Ajuda (F1)",
        "desc_f1": (
            "Reimprime a ajuda de início (banner, dispositivos, motores, dicas) na aba **Sistema** e foca nela. "
            "No final, o mesmo **resumo do cache de frases** da aba Tradução "
            "(pares, palavras por direção, ON/OFF, qualidade)."
        ),
        "title_f2": "Bypass de voz (F2)",
        "desc_f2": (
            "Alterna o **bypass de voz** (igual ao comando `b` / clique no badge branco à esquerda da caixa de comando). "
            "ON = mic cru → saída (sem STT/tradução). OFF = áudio traduzido. "
            "Copiar log completo continua em **Ctrl+Shift+C**."
        ),
        "title_f3": "Ciclar abas (F3)",
        "desc_f3": "Cicla as abas: **Tradução → Sistema → Novidades → Lista de comandos → …**.",
        "title_f4": "UI compacta (F4)",
        "desc_f4": "Alterna UI compacta (esconde o menu de comandos; a linha de comando permanece). Igual a `u`.",
        "title_f5": "Auto-scroll Tradução (F5)",
        "desc_f5": (
            "Alterna o **auto-scroll** dos dois painéis de Tradução (**LC** esquerda + **VOZ** direita). "
            "ON (padrão, badge verde) = linhas novas / pós-chunk / saída de comandos vão ao fim. "
            "OFF (badge âmbar) = a vista fica onde você deixou — dá para ler o histórico enquanto chegam linhas. "
            "No rodapé: `Auto↓ ON` / `Auto↓ OFF`. Clique no chip F5 ou pressione **F5**. "
            "`GG`/`gf` ainda saltam ao fim uma vez sem religar o follow se estiver OFF."
        ),
        "title_search": "Buscar no log (estilo vim)",
        "desc_search": (
            "Digite `/texto` e Enter para buscar no painel de log **focado** (sem diferenciar maiúsculas). "
            "Em Tradução, clique em **LC** ou **VOZ** antes (padrão VOZ). Também Sistema, Novidades e Lista de comandos. "
            "Rola até a 1ª ocorrência e mostra contador (ex.: `1/7`). "
            "Só `/` repete a última busca no painel atual. "
            "Se a tecla `/` não digitar na barra (terminal/layout), use: `find texto`, `find:texto` ou `s?texto`."
        ),
        "title_search_n": "Próxima ocorrência",
        "desc_search_n": (
            "Vai à **próxima** ocorrência da última busca `/` (volta ao início no fim). "
            "Se trocou de aba, refaz a mesma query na aba ativa. "
            "Não conflita com mute do mic `n` (o `/` é obrigatório)."
        ),
        "title_search_p": "Ocorrência anterior",
        "desc_search_p": (
            "Vai à ocorrência **anterior** da última busca `/` (volta ao fim no início). "
            "Não conflita com abrir pasta `p` (o `/` é obrigatório)."
        ),
        "title_up_down": "Histórico de comandos",
        "desc_up_down": "No campo de comando, ↑ / ↓ navegam comandos anteriores (salvos em `.cache/cmd_history.txt`).",
        "title_c": "Exportar Markdown",
        "desc_c": (
            "Exporta a sessão para um `.md`. **Dois canais:** corpo cronológico com "
            "`[LC N] LiveCaptions` (entrada) vs `[Chunk N] LiveLingo VOZ` (mic+áudio); "
            "VOZ inclui path de áudio quando houver. Depois: anexos "
            "**só LiveCaptions** e **só VOZ**. Resumo executivo por IA opcional "
            "(divide transcrições longas via `SUMMARY_MAX_INPUT_TOKENS`)."
        ),
        "title_cls": "Limpar log",
        "desc_cls": "Limpa **Tradução LC**, **Tradução VOZ** e **Sistema** (não limpa Novidades / Lista de comandos). No modo classic, limpa o terminal.",
        "title_cls1": "Limpar LC (esquerda)",
        "desc_cls1": "Limpa só a coluna **esquerda** de Tradução — LiveCaptions (`#log-lc`). Use `cls2` para VOZ, `cls` para tudo + Sistema.",
        "title_cls2": "Limpar VOZ (direita)",
        "desc_cls2": "Limpa só a coluna **direita** de Tradução — VOZ mic + comandos (`#log`). Use `cls1` para LC, `cls` para tudo + Sistema.",
        "title_co": "Comentar último",
        "desc_co": "Anexa um comentário de texto livre ao **último** chunk (SQLite; aparece no `l` com `#id`).",
        "title_coN": "Comentar chunk N",
        "desc_coN": "Comenta o **chunk N**. Formas: `coN` (pede texto) ou `coN texto`. Exemplo: `co3 observação`.",
        "title_codN": "Apagar comentário #N",
        "desc_codN": "Apaga um comentário pelo id primário (`#id` da lista). Exemplo: `cod12`.",
        "title_d": "Apagar último",
        "desc_d": "Apaga o **último** chunk (histórico, banco e arquivo de áudio se existir).",
        "title_dN": "Apagar chunk N",
        "desc_dN": "Apaga o **chunk N** e dependências. Exemplo: `d8`.",
        "title_e": "Editar último",
        "desc_e": "Edita a **última** frase: pré-preenche o campo (TUI) ou pede texto; retraduz e pode ressintetizar áudio.",
        "title_eN": "Editar chunk N",
        "desc_eN": "Edita o **chunk N** como o `e`. Exemplo: `e4`.",
        "title_enew": "Novo texto (sem mic)",
        "desc_enew": "Enfileira uma tradução só com texto **digitado** (sem microfone/STT). Exemplo: `enew Olá mundo`. O TTS segue o modo de som (`s`).",
        "title_f": "Favoritar último",
        "desc_f": "Marca o **último** chunk como favorito.",
        "title_fN": "Favoritar chunk N",
        "desc_fN": "Marca o **chunk N** como favorito. Exemplo: `f2`.",
        "title_F": "Listar favoritos",
        "desc_F": "Mostra todas as frases favoritas desta sessão (popup / lista).",
        "title_gg": "Ir ao topo",
        "desc_gg": "Vai ao **início** da aba de log ativa e desliga auto-scroll. Aliases: `gg`, `gt`.",
        "title_GG": "Ir ao fim",
        "desc_GG": "Vai ao **fim** da aba de log ativa e religa auto-scroll. Aliases: `GG` (sensível a maiúsculas), `gf`.",
        "title_l": "Listar mensagens",
        "desc_l": (
            "Lista todas as frases da sessão (cronológico). **Painéis separados:** "
            "LiveCaptions → log **esquerdo** (magenta), LiveLingo VOZ → log **direito** (amarelo). "
            "Arraste a barra ║ para redimensionar; Expandir/Restaurar maximiza um lado. "
            "Cabeçalho com contagem LC vs VOZ. Timing, data, path de áudio, comentários (`#id`)."
        ),
        "title_lc": "Live Captions pausar/retomar",
        "desc_lc": (
            "Liga/desliga tradução de **Windows LiveCaptions** (faixa acima das abas de log). "
            "Padrão **OFF** na entrada (`LIVE_CAPTIONS_START_ON_LAUNCH=false`) — escuta ativa = caminho VOZ/mic. "
            "Independente do mic→Whisper. Requer Win11 + `LIVE_CAPTIONS_ENABLED=true` + `uiautomation`. "
            "Também: `lc on` / `lc off` / `lc show` / `lc hide` / `lc status`."
        ),
        "title_lc_on": "Iniciar / retomar LiveCaptions",
        "desc_lc_on": "Inicia o scrape se estiver OFF, ou retoma após pausa. Aliases: `lc resume`, `lc start`.",
        "title_lc_off": "Desligar LiveCaptions",
        "desc_lc_off": "Desliga LC (pausa a faixa; pipeline VOZ/mic continua). Aliases: `lc pause`, `lc stop`.",
        "title_lc_show": "Mostrar janela LiveCaptions",
        "desc_lc_show": "Restaura a janela do Windows LiveCaptions (desoculta). Alias: `lc restore`.",
        "title_lc_hide": "Ocultar janela LiveCaptions",
        "desc_lc_hide": "Minimiza/oculta de novo a janela do Windows LiveCaptions.",
        "title_lc_status": "Status LiveCaptions",
        "desc_lc_status": "Mostra snapshot do serviço: status, paused, hidden, último erro. Aliases: `lc st`, `lc ?`.",
        "title_cam": "Webcam lip-sync (liga/desliga)",
        "desc_cam": (
            "Alterna **webcam → lip-sync → câmera virtual** (Teams/Meet). "
            "Requer `WEBCAM_ENABLED=true` e `opencv-python` + `mediapipe` + `pyvirtualcam`. "
            "O áudio TTS do Cable Out move a boca. Ver `docs/webcam-lipsync.md`."
        ),
        "title_cam_on": "Webcam on",
        "desc_cam_on": (
            "Inicia threads (se preciso) e abre webcam física + câmera virtual."
        ),
        "title_cam_off": "Webcam off",
        "desc_cam_off": (
            "Libera webcam física e OBS Virtual Cam para outros apps. "
            "Threads ficam idle; [cam on] reabre (sem reiniciar o app)."
        ),
        "title_cam_status": "Status webcam",
        "desc_cam_status": "FPS, face, template, engine, resolução, backend, último erro.",
        "title_cam_snap_closed": "Foto boca fechada",
        "desc_cam_snap_closed": (
            "Salva **template de boca fechada** (idle enquanto o mic escuta). "
            "Feche a boca, olhe a câmera e rode o comando. "
            "Arquivos: `.cache/webcam/closed_mouth.png` (+ JSON de landmarks)."
        ),
        "title_cam_closed": "Boca calada (F10)",
        "desc_cam_closed": (
            "**F10** ou `cam closed`: mostra/tira a **placa de rosto** (boca calada) no vídeo ao vivo. "
            "Sem auto ao falar no mic — só F10/F11. `cam closed off` = ao vivo de novo."
        ),
        "title_cam_full": "Tela closed inteira (F11)",
        "desc_cam_full": (
            "**F11** ou `cam full` / `cam freeze` / `cam tela`: preenche a câmera virtual "
            "com a **foto closed inteira** (esconde o vídeo ao vivo; **não** maximiza a TUI). "
            "Outro F11 volta ao vivo. Precisa de `cam snap closed`."
        ),
        "title_sub": "Legenda vcam burn-in (TARGET)",
        "desc_sub": (
            "Liga/desliga **texto TARGET (traduzido)** na OBS Virtual Cam. "
            "Fica até **`sub off`** ou a **próxima tradução** (sem sumir sozinha). "
            "Só pixels — não é CC do Teams. "
            "`sub on` / `sub off` / `sub status`. Padrão OFF. `[u]` = UI compacta."
        ),
        "title_cam_sub": "Legenda vcam via cam",
        "desc_cam_sub": (
            "Igual a `sub`: `cam sub` / `cam sub on` / `cam sub off` — "
            "desenha a última tradução **TARGET** na base do frame da câmera virtual."
        ),
        "title_lo": "Listar só source",
        "desc_lo": "Lista só as linhas **source** (ouvidas) da sessão.",
        "title_lt": "Listar só target",
        "desc_lt": "Lista só as linhas **target** (traduzidas) da sessão.",
        "title_m": "Mostrar menu",
        "desc_m": "Mostra de novo o resumo do menu (status + lista compacta de comandos).",
        "title_pc": "Status do cache de frases",
        "desc_pc": (
            "Mostra stats do TM (hits/misses/taxa/mem) e o último evento HIT/MISS/store. "
            "Memória de **frases completas** em **todas as sessões** (não só a atual). "
            "Ver também: `pc on`, `pc off`, `pc force`, `pc last`, `pc good`, `pc bad`, "
            "`pc undo`, `pc backup`, `pc restore`, `pc import`. Env: `PHRASE_CACHE=true`."
        ),
        "title_pc_on": "Ligar cache de frases",
        "desc_pc_on": (
            "Liga o TM neste processo e aquece a RAM a partir do SQLite. "
            "Frases repetidas podem sair como **Translated [CACHE]** (magenta) sem chamar o LLM. "
            "Compare latência com `pc off`."
        ),
        "title_pc_off": "Desligar cache de frases",
        "desc_pc_off": (
            "Desliga o TM. Toda frase é traduzida ao vivo (Google/LLM) e aparece "
            "**Translated [LIVE]** (ciano). Os pares ficam no banco; só a consulta para."
        ),
        "title_pc_force": "Forçar próxima tradução live",
        "desc_pc_force": (
            "O **próximo** chunk ignora HIT: traduz ao vivo e **sobrescreve** o par. "
            "O target anterior vai para o histórico (`pc undo`). Use após `pc bad`."
        ),
        "title_pc_last": "Revisar último evento do cache",
        "desc_pc_last": (
            "Mostra o último HIT/MISS/store: frase source, target, camada (memory/sqlite), qualidade. "
            "Para julgar se o CACHE ficou no contexto."
        ),
        "title_pc_good": "Marcar último par como bom",
        "desc_pc_good": "Marca o último HIT/store como qualidade **good** no SQLite.",
        "title_pc_bad": "Marcar último par como ruim",
        "desc_pc_bad": (
            "Marca o último HIT/store como **bad** (fora de contexto). Depois `pc force` e fale de novo, "
            "ou `pc undo` se a sobrescrita foi errada."
        ),
        "title_pc_undo": "Desfazer última sobrescrita",
        "desc_pc_undo": (
            "Restaura o **target anterior** do último par a partir de `translation_pairs_history`. "
            "Não apaga o par; só reverte o texto target e a entrada em RAM."
        ),
        "title_pc_backup": "Backup do cache de frases",
        "desc_pc_backup": (
            "Grava todos os `translation_pairs` em JSON em `.cache/phrase_cache_backups/` "
            "(arquivo com data + `phrase_cache_latest.json`). Faça antes de import/experimentos."
        ),
        "title_pc_restore": "Restaurar cache de frases",
        "desc_pc_restore": (
            "`pc restore` carrega o latest; `pc restore caminho.json` um arquivo específico. "
            "Faz backup automático antes. Merge por upsert (sem duplicar chaves)."
        ),
        "title_pc_import": "Importar CSV para o cache",
        "desc_pc_import": (
            "`pc import arquivo.csv` ou `pc import arquivo.csv reverse`. Colunas: SourceText, "
            "TranslatedText, TargetLanguage. Dedup por source normalizado + par de idiomas. "
            "CLI: `python -m livelingo.import_phrase_csv arquivo.csv [--dry-run] [--also-reverse]`. "
            "Exports típicos são EN→PT; use `reverse` para HIT em PT→EN."
        ),
        "title_q": "Sair",
        "desc_q": "Encerra a sessão e sai. O id da sessão é impresso para retomar depois com `livelingo <id>`.",
        "title_u": "UI compacta",
        "desc_u": "Alterna TUI compacta: esconde o menu multilinha; mantém a entrada de comando. Igual a **F4**. Aliases: `ui`, `compact`.",
        "title_v": "Trocar sessão",
        "desc_v": "Sai da sessão atual e volta ao seletor (nova / retomar / apagar).",
    },
    "es": {
        "tab": "Lista de comandos",
        "intro": (
            "Todos los comandos del menú de LiveLingo, **agrupados** por área. "
            "Grupos y comandos en orden **alfabético**. "
            "Escriba el comando abajo y pulse **Enter**."
        ),
        "group_audio": "Audio",
        "group_idiom": "Idioma",
        "group_keys": "Teclado",
        "group_sentence": "Frase",
        "group_session": "Sesión",
        "title_a": "Copiar ruta de audio",
        "desc_a": "Copia la ruta absoluta del audio del **último** chunk al portapapeles.",
        "title_aN": "Copiar ruta de audio N",
        "desc_aN": "Copia la ruta del audio del **chunk N**. Ejemplo: `a12`.",
        "title_b": "Bypass de voz",
        "desc_b": "Alterna: envía el micrófono en crudo a la salida (p. ej. VB-Cable) **sin** STT/traducción. Cabecera `BYPASS`. Pulse de nuevo para traducir. Alias: `bypass`, `hot`.",
        "title_ctts": "Cambiar voz TTS",
        "desc_ctts": "Cambia la voz Edge TTS. Use `ctts <ShortName>` o solo `ctts` para introducir el nombre.",
        "title_lav": "Listar todas las voces TTS",
        "desc_lav": "Ejecuta `edge-tts --list-voices` y muestra el catálogo en el log.",
        "title_ld": "Listar dispositivos de audio",
        "desc_ld": "Ejecuta `list_devices.py` y lista entradas/salidas en el log.",
        "title_lv": "Voces TTS filtradas",
        "desc_lv": "Lista voces Edge filtradas a locales comunes (en/es/fr).",
        "title_n": "Silenciar micrófono",
        "desc_n": "Activa/desactiva el mute del micrófono (Core Audio + gate de la app).",
        "title_p": "Abrir carpeta de audio",
        "desc_p": "Abre la carpeta del audio del **último** chunk en el explorador.",
        "title_pN": "Abrir carpeta de audio N",
        "desc_pN": "Abre la carpeta del **chunk N**. Ejemplo: `p5`.",
        "title_r": "Repetir último",
        "desc_r": "Reproduce el TTS del **último** chunk. Reactiva el sonido si estaba OFF. Muestra texto source y target.",
        "title_rN": "Repetir chunk N",
        "desc_rN": "Reproduce el TTS del **chunk N**. Ejemplo: `r133`. Target en verde bajo el estado.",
        "title_s": "Sonido ON/OFF",
        "desc_s": "Activa/desactiva la reproducción TTS en vivo. Por defecto OFF (solo texto).",
        "title_x": "Detener reproducción",
        "desc_x": "Detiene el TTS actual y vacía la cola de audio.",
        "title_g": "Intercambiar idiomas",
        "desc_g": "Invierte `SOURCE_LANG` ↔ `TARGET_LANG` en caliente (STT, traductor, TTS). No reescribe chunks antiguos.",
        "title_o": "Sinónimos / significado",
        "desc_o": "Consulta sinónimos o significado (IA). Pide el término si se usa solo.",
        "title_t": "Cambiar idioma TARGET",
        "desc_t": "Define solo el idioma **destino**. Ejemplo: `t EN`. Entrada en MAYÚSCULAS en este comando.",
        "title_ctrl_c": "Copiar selección",
        "desc_ctrl_c": "Copia la selección del log activo; sin selección, copia el log completo.",
        "title_ctrl_q": "Salir",
        "desc_ctrl_q": "Cierra la aplicación (como el comando `q`).",
        "title_ctrl_shift_c": "Copiar log completo",
        "desc_ctrl_shift_c": "Copia siempre todo el log de la pestaña activa.",
        "title_f1": "Ayuda (F1)",
        "desc_f1": "Imprime la ayuda de inicio en la pestaña **Sistema**.",
        "title_f2": "Bypass de voz (F2)",
        "desc_f2": "Alterna bypass de voz (igual a `b` / badge blanco). Copiar log: Ctrl+Shift+C.",
        "title_f3": "Ciclar pestañas (F3)",
        "desc_f3": "Cicla: **Tradução → Sistema → Novidades → Lista de comandos → …**.",
        "title_f4": "UI compacta (F4)",
        "desc_f4": "Oculta el menú de comandos; mantiene la línea de entrada. Igual que `u`.",
        "title_f5": "Auto-scroll Tradução (F5)",
        "desc_f5": (
            "Activa/desactiva el **auto-scroll** de ambos paneles de Tradução (LC + VOZ). "
            "ON = nuevas líneas van al final. OFF = la vista se queda fija. **F5** o clic en el chip."
        ),
        "title_up_down": "Historial de comandos",
        "desc_up_down": "↑ / ↓ recorren comandos anteriores en el campo de comando.",
        "title_c": "Exportar Markdown",
        "desc_c": (
            "Exporta la sesión a `.md`. Canales **LC** (LiveCaptions) y **VOZ** (mic); "
            "anexos separados; resumen IA opcional (`SUMMARY_MAX_INPUT_TOKENS`)."
        ),
        "title_cls": "Limpiar log",
        "desc_cls": "Limpia **Tradução** y **Sistema** (no Novidades / Lista de comandos).",
        "title_co": "Comentar último",
        "desc_co": "Añade un comentario al **último** chunk (SQLite; visible en `l` con `#id`).",
        "title_coN": "Comentar chunk N",
        "desc_coN": "Comenta el **chunk N**: `coN` o `coN texto`.",
        "title_codN": "Borrar comentario #N",
        "desc_codN": "Borra el comentario por id. Ejemplo: `cod12`.",
        "title_d": "Borrar último",
        "desc_d": "Borra el **último** chunk (historial, BD y audio).",
        "title_dN": "Borrar chunk N",
        "desc_dN": "Borra el **chunk N**. Ejemplo: `d8`.",
        "title_e": "Editar último",
        "desc_e": "Edita la **última** frase y retraduce (puede regenerar audio).",
        "title_eN": "Editar chunk N",
        "desc_eN": "Edita el **chunk N**. Ejemplo: `e4`.",
        "title_enew": "Texto nuevo (sin mic)",
        "desc_enew": "Traduce solo texto escrito, sin micrófono. Ejemplo: `enew Hola`. TTS según `s`.",
        "title_f": "Favorito último",
        "desc_f": "Marca el **último** chunk como favorito.",
        "title_fN": "Favorito chunk N",
        "desc_fN": "Marca el **chunk N** como favorito.",
        "title_F": "Listar favoritos",
        "desc_F": "Muestra los favoritos de la sesión.",
        "title_gg": "Ir al inicio",
        "desc_gg": "Salta al inicio del log activo y desactiva auto-scroll (`gg` / `gt`).",
        "title_GG": "Ir al final",
        "desc_GG": "Salta al final y reactiva auto-scroll (`GG` / `gf`).",
        "title_l": "Listar mensajes",
        "desc_l": (
            "Lista frases (cronológico). **Dos raíles:** LiveCaptions izquierda (magenta), "
            "VOZ mic derecha (amarillo). Timing, audio y comentarios."
        ),
        "title_lc": "Live Captions pausar/reanudar",
        "desc_lc": (
            "Activa/desactiva Windows LiveCaptions (franja sobre las pestañas). "
            "Por defecto **OFF** al entrar. `lc on`/`off`/`show`/`hide`/`status`. "
            "Requiere Win11 + uiautomation."
        ),
        "title_lc_on": "Iniciar / reanudar LiveCaptions",
        "desc_lc_on": "Inicia el scrape si está OFF, o reanuda tras pausa.",
        "title_lc_off": "Apagar LiveCaptions",
        "desc_lc_off": "Apaga LC (pausa la franja; el mic/VOZ sigue).",
        "title_lc_show": "Mostrar ventana LiveCaptions",
        "desc_lc_show": "Restaura la ventana de LiveCaptions. Alias: `lc restore`.",
        "title_lc_hide": "Ocultar ventana LiveCaptions",
        "desc_lc_hide": "Oculta la ventana de LiveCaptions.",
        "title_lc_status": "Estado LiveCaptions",
        "desc_lc_status": "Muestra status / paused / hidden / error.",
        "title_lo": "Solo source",
        "desc_lo": "Lista solo líneas **source** (oídas).",
        "title_lt": "Solo target",
        "desc_lt": "Lista solo líneas **target** (traducidas).",
        "title_m": "Mostrar menú",
        "desc_m": "Vuelve a mostrar el resumen del menú de comandos.",
        "title_q": "Salir",
        "desc_q": "Termina la sesión. Se imprime el id para reanudar con `livelingo <id>`.",
        "title_u": "UI compacta",
        "desc_u": "Oculta el menú multilínea; mantiene la entrada. Igual que **F4**.",
        "title_v": "Cambiar sesión",
        "desc_v": "Vuelve al selector de sesión (nueva / reanudar / borrar).",
    },
    "fr": {
        "tab": "Liste des commandes",
        "intro": (
            "Toutes les commandes du menu LiveLingo, **groupées** par zone. "
            "Groupes et commandes en ordre **alphabétique**. "
            "Tapez la commande ci-dessous puis **Entrée**."
        ),
        "group_audio": "Audio",
        "group_idiom": "Langue",
        "group_keys": "Clavier",
        "group_sentence": "Phrase",
        "group_session": "Session",
        "title_a": "Copier le chemin audio",
        "desc_a": "Copie le chemin absolu de l'audio du **dernier** chunk dans le presse-papiers.",
        "title_aN": "Copier le chemin audio N",
        "desc_aN": "Copie le chemin de l'audio du **chunk N**. Exemple : `a12`.",
        "title_b": "Bypass vocal",
        "desc_b": "Bascule : micro brut vers la sortie (ex. VB-Cable) **sans** STT/traduction. En-tête `BYPASS`. Raccourcis : `bypass`, `hot`.",
        "title_ctts": "Changer la voix TTS",
        "desc_ctts": "Change la voix Edge TTS. `ctts <ShortName>` ou `ctts` pour saisir le nom.",
        "title_lav": "Lister toutes les voix TTS",
        "desc_lav": "Lance `edge-tts --list-voices` et affiche le catalogue dans le log.",
        "title_ld": "Lister les périphériques audio",
        "desc_ld": "Lance `list_devices.py` et liste entrées/sorties dans le log.",
        "title_lv": "Voix TTS filtrées",
        "desc_lv": "Liste les voix Edge filtrées (en/es/fr).",
        "title_n": "Muet micro",
        "desc_n": "Active/désactive le mute micro (Core Audio + gate de l'app).",
        "title_p": "Ouvrir le dossier audio",
        "desc_p": "Ouvre le dossier de l'audio du **dernier** chunk.",
        "title_pN": "Ouvrir le dossier audio N",
        "desc_pN": "Ouvre le dossier du **chunk N**. Exemple : `p5`.",
        "title_r": "Rejouer le dernier",
        "desc_r": "Rejoue le TTS du **dernier** chunk. Réactive le son si OFF. Affiche source et cible.",
        "title_rN": "Rejouer le chunk N",
        "desc_rN": "Rejoue le TTS du **chunk N**. Exemple : `r133`. Cible en vert sous le statut.",
        "title_s": "Son ON/OFF",
        "desc_s": "Active/désactive la lecture TTS en direct. Par défaut OFF (texte seul).",
        "title_x": "Arrêter la lecture",
        "desc_x": "Arrête le TTS en cours et vide la file audio.",
        "title_g": "Inverser les langues",
        "desc_g": "Inverse `SOURCE_LANG` ↔ `TARGET_LANG` à chaud (STT, traducteur, TTS).",
        "title_o": "Synonymes / sens",
        "desc_o": "Recherche synonymes ou sens (IA). Demande le terme si utilisé seul.",
        "title_t": "Changer la langue TARGET",
        "desc_t": "Définit uniquement la langue **cible**. Exemple : `t EN`. Saisie en MAJUSCULES pour cette commande.",
        "title_ctrl_c": "Copier la sélection",
        "desc_ctrl_c": "Copie la sélection du log actif ; sinon le log entier.",
        "title_ctrl_q": "Quitter",
        "desc_ctrl_q": "Quitte l'application (comme `q`).",
        "title_ctrl_shift_c": "Copier tout le log",
        "desc_ctrl_shift_c": "Copie toujours tout le log de l'onglet actif.",
        "title_f1": "Aide (F1)",
        "desc_f1": "Affiche l'aide de démarrage dans l'onglet **Sistema**.",
        "title_f2": "Bypass vocal (F2)",
        "desc_f2": "Bascule le bypass vocal (comme `b` / badge blanc). Copier le log : Ctrl+Shift+C.",
        "title_f3": "Parcourir les onglets (F3)",
        "desc_f3": "Cycle : **Tradução → Sistema → Novidades → Liste des commandes → …**.",
        "title_f4": "UI compacte (F4)",
        "desc_f4": "Masque le menu ; garde la ligne de commande. Comme `u`.",
        "title_f5": "Auto-scroll Tradução (F5)",
        "desc_f5": (
            "Bascule l'**auto-scroll** des deux panneaux Tradução (LC + VOZ). "
            "ON = nouvelles lignes en bas. OFF = vue figée. **F5** ou clic sur le chip."
        ),
        "title_up_down": "Historique des commandes",
        "desc_up_down": "↑ / ↓ parcourent les commandes précédentes.",
        "title_c": "Exporter Markdown",
        "desc_c": "Exporte la session en `.md`, avec résumé IA optionnel.",
        "title_cls": "Effacer le log",
        "desc_cls": "Efface **Tradução** et **Sistema** (pas Novidades / Liste des commandes).",
        "title_co": "Commenter le dernier",
        "desc_co": "Ajoute un commentaire au **dernier** chunk (SQLite ; `#id` dans `l`).",
        "title_coN": "Commenter le chunk N",
        "desc_coN": "Commente le **chunk N** : `coN` ou `coN texte`.",
        "title_codN": "Supprimer le commentaire #N",
        "desc_codN": "Supprime le commentaire par id. Exemple : `cod12`.",
        "title_d": "Supprimer le dernier",
        "desc_d": "Supprime le **dernier** chunk (historique, BDD, audio).",
        "title_dN": "Supprimer le chunk N",
        "desc_dN": "Supprime le **chunk N**. Exemple : `d8`.",
        "title_e": "Éditer le dernier",
        "desc_e": "Édite la **dernière** phrase et retraduit (peut regénérer l'audio).",
        "title_eN": "Éditer le chunk N",
        "desc_eN": "Édite le **chunk N**. Exemple : `e4`.",
        "title_enew": "Nouveau texte (sans micro)",
        "desc_enew": "Traduit un texte saisi, sans micro. Exemple : `enew Bonjour`. TTS selon `s`.",
        "title_f": "Favori dernier",
        "desc_f": "Marque le **dernier** chunk en favori.",
        "title_fN": "Favori chunk N",
        "desc_fN": "Marque le **chunk N** en favori.",
        "title_F": "Lister les favoris",
        "desc_F": "Affiche les favoris de la session.",
        "title_gg": "Aller en haut",
        "desc_gg": "Va au début du log actif et coupe l'auto-scroll (`gg` / `gt`).",
        "title_GG": "Aller en bas",
        "desc_GG": "Va à la fin et réactive l'auto-scroll (`GG` / `gf`).",
        "title_l": "Lister les messages",
        "desc_l": "Liste les phrases avec timing, horodatage, audio et commentaires.",
        "title_lo": "Source seulement",
        "desc_lo": "Liste uniquement les lignes **source**.",
        "title_lt": "Cible seulement",
        "desc_lt": "Liste uniquement les lignes **target**.",
        "title_m": "Afficher le menu",
        "desc_m": "Réaffiche le résumé du menu de commandes.",
        "title_q": "Quitter",
        "desc_q": "Termine la session. L'id est affiché pour reprendre avec `livelingo <id>`.",
        "title_u": "UI compacte",
        "desc_u": "Masque le menu multi-lignes ; garde la saisie. Comme **F4**.",
        "title_v": "Changer de session",
        "desc_v": "Retourne au sélecteur de session (nouvelle / reprendre / supprimer).",
    },
    "de": {
        "tab": "Befehlsliste",
        "intro": (
            "Alle LiveLingo-Menübefehle, nach Bereich **gruppiert**. "
            "Gruppen und Befehle **alphabetisch**. "
            "Befehl unten eingeben und **Enter**."
        ),
        "group_audio": "Audio",
        "group_idiom": "Sprache",
        "group_keys": "Tastatur",
        "group_sentence": "Satz",
        "group_session": "Sitzung",
        "title_a": "Audiopfad kopieren",
        "desc_a": "Kopiert den absoluten Pfad des **letzten** Chunk-Audios in die Zwischenablage.",
        "title_aN": "Audiopfad N kopieren",
        "desc_aN": "Kopiert den Pfad von **Chunk N**. Beispiel: `a12`.",
        "title_b": "Voice-Bypass",
        "desc_b": "Umschalten: Roh-Mikrofon zur Ausgabe (z. B. VB-Cable) **ohne** STT/Übersetzung. Header `BYPASS`. Alias: `bypass`, `hot`.",
        "title_ctts": "TTS-Stimme ändern",
        "desc_ctts": "Edge-TTS-Stimme ändern: `ctts <ShortName>` oder nur `ctts`.",
        "title_lav": "Alle TTS-Stimmen",
        "desc_lav": "Führt `edge-tts --list-voices` aus und listet den Katalog im Log.",
        "title_ld": "Audiogeräte auflisten",
        "desc_ld": "Führt `list_devices.py` aus und listet Geräte im Log.",
        "title_lv": "Gefilterte TTS-Stimmen",
        "desc_lv": "Listet gefilterte Edge-Stimmen (en/es/fr).",
        "title_n": "Mikro stumm",
        "desc_n": "Mikrofon-Mute umschalten (Core Audio + App-Gate).",
        "title_p": "Audioordner öffnen",
        "desc_p": "Öffnet den Ordner des **letzten** Chunk-Audios.",
        "title_pN": "Audioordner N öffnen",
        "desc_pN": "Öffnet den Ordner von **Chunk N**. Beispiel: `p5`.",
        "title_r": "Letzten wiedergeben",
        "desc_r": "Spielt TTS des **letzten** Chunks. Schaltet Sound wieder ein falls OFF. Zeigt Source und Target.",
        "title_rN": "Chunk N wiedergeben",
        "desc_rN": "Spielt TTS von **Chunk N**. Beispiel: `r133`. Target grün unter der Statuszeile.",
        "title_s": "Sound AN/AUS",
        "desc_s": "Live-TTS umschalten. Standard AUS (nur Text).",
        "title_x": "Wiedergabe stoppen",
        "desc_x": "Stoppt aktuelles TTS und leert die Audio-Warteschlange.",
        "title_g": "Sprachen tauschen",
        "desc_g": "Tauscht `SOURCE_LANG` ↔ `TARGET_LANG` live (STT, Übersetzer, TTS).",
        "title_o": "Synonyme / Bedeutung",
        "desc_o": "Synonyme oder Bedeutung nachschlagen (KI). Fragt den Begriff bei alleiniger Nutzung.",
        "title_t": "TARGET-Sprache ändern",
        "desc_t": "Setzt nur die **Ziel**sprache. Beispiel: `t EN`. Eingabe in GROSSBUCHSTABEN.",
        "title_ctrl_c": "Auswahl kopieren",
        "desc_ctrl_c": "Kopiert die Auswahl im aktiven Log; sonst das ganze Log.",
        "title_ctrl_q": "Beenden",
        "desc_ctrl_q": "Beendet die App (wie `q`).",
        "title_ctrl_shift_c": "Ganzes Log kopieren",
        "desc_ctrl_shift_c": "Kopiert immer das gesamte aktive Log.",
        "title_f1": "Hilfe (F1)",
        "desc_f1": "Start-Hilfe im Tab **Sistema**.",
        "title_f2": "Sprach-Bypass (F2)",
        "desc_f2": "Schaltet Sprach-Bypass um (wie `b` / weißes Badge). Log kopieren: Ctrl+Shift+C.",
        "title_f3": "Tabs wechseln (F3)",
        "desc_f3": "Zyklus: **Tradução → Sistema → Novidades → Befehlsliste → …**.",
        "title_f4": "Kompakte UI (F4)",
        "desc_f4": "Menü ausblenden; Eingabezeile bleibt. Wie `u`.",
        "title_f5": "Auto-Scroll Tradução (F5)",
        "desc_f5": (
            "Schaltet **Auto-Scroll** für beide Tradução-Panels (LC + VOZ). "
            "ON = neue Zeilen ans Ende. OFF = Ansicht bleibt. **F5** oder Klick auf den Chip."
        ),
        "title_up_down": "Befehlshistorie",
        "desc_up_down": "↑ / ↓ blättern frühere Befehle.",
        "title_c": "Markdown exportieren",
        "desc_c": "Exportiert die Sitzung als `.md`, optional mit KI-Zusammenfassung.",
        "title_cls": "Log leeren",
        "desc_cls": "Leert **Tradução** und **Sistema** (nicht Novidades / Befehlsliste).",
        "title_co": "Letzten kommentieren",
        "desc_co": "Kommentar am **letzten** Chunk (SQLite; `#id` in `l`).",
        "title_coN": "Chunk N kommentieren",
        "desc_coN": "Kommentar zu **Chunk N**: `coN` oder `coN text`.",
        "title_codN": "Kommentar #N löschen",
        "desc_codN": "Löscht Kommentar per ID. Beispiel: `cod12`.",
        "title_d": "Letzten löschen",
        "desc_d": "Löscht den **letzten** Chunk (Historie, DB, Audio).",
        "title_dN": "Chunk N löschen",
        "desc_dN": "Löscht **Chunk N**. Beispiel: `d8`.",
        "title_e": "Letzten bearbeiten",
        "desc_e": "Bearbeitet den **letzten** Satz und übersetzt neu.",
        "title_eN": "Chunk N bearbeiten",
        "desc_eN": "Bearbeitet **Chunk N**. Beispiel: `e4`.",
        "title_enew": "Neuer Text (ohne Mikro)",
        "desc_enew": "Übersetzung nur aus getipptem Text. Beispiel: `enew Hallo`. TTS folgt `s`.",
        "title_f": "Favorit letzter",
        "desc_f": "Markiert den **letzten** Chunk als Favorit.",
        "title_fN": "Favorit Chunk N",
        "desc_fN": "Markiert **Chunk N** als Favorit.",
        "title_F": "Favoriten listen",
        "desc_F": "Zeigt Favoriten der Sitzung.",
        "title_gg": "Nach oben",
        "desc_gg": "Zum Anfang des aktiven Logs; Auto-Scroll aus (`gg` / `gt`).",
        "title_GG": "Nach unten",
        "desc_GG": "Zum Ende; Auto-Scroll an (`GG` / `gf`).",
        "title_l": "Nachrichten listen",
        "desc_l": "Listet Phrasen mit Timing, Zeitstempel, Audio und Kommentaren.",
        "title_lo": "Nur Source",
        "desc_lo": "Nur **Source**-Zeilen.",
        "title_lt": "Nur Target",
        "desc_lt": "Nur **Target**-Zeilen.",
        "title_m": "Menü anzeigen",
        "desc_m": "Zeigt die Befehlsmenü-Zusammenfassung erneut.",
        "title_q": "Beenden",
        "desc_q": "Beendet die Sitzung. Session-ID wird ausgegeben für `livelingo <id>`.",
        "title_u": "Kompakte UI",
        "desc_u": "Mehrzeiliges Menü aus; Eingabe bleibt. Wie **F4**.",
        "title_v": "Sitzung wechseln",
        "desc_v": "Zurück zum Sitzungsauswahl (neu / fortsetzen / löschen).",
    },
    "it": {
        "tab": "Elenco comandi",
        "intro": (
            "Tutti i comandi del menu LiveLingo, **raggruppati** per area. "
            "Gruppi e comandi in ordine **alfabetico**. "
            "Digita il comando sotto e premi **Invio**."
        ),
        "group_audio": "Audio",
        "group_idiom": "Lingua",
        "group_keys": "Tastiera",
        "group_sentence": "Frase",
        "group_session": "Sessione",
        "title_a": "Copia percorso audio",
        "desc_a": "Copia il percorso assoluto dell'audio dell'**ultimo** chunk negli appunti.",
        "title_aN": "Copia percorso audio N",
        "desc_aN": "Copia il percorso dell'audio del **chunk N**. Esempio: `a12`.",
        "title_b": "Bypass voce",
        "desc_b": "Attiva/disattiva: microfono grezzo verso l'uscita (es. VB-Cable) **senza** STT/traduzione. Header `BYPASS`. Alias: `bypass`, `hot`.",
        "title_ctts": "Cambia voce TTS",
        "desc_ctts": "Cambia la voce Edge TTS: `ctts <ShortName>` oppure solo `ctts`.",
        "title_lav": "Elenca tutte le voci TTS",
        "desc_lav": "Esegue `edge-tts --list-voices` e stampa il catalogo nel log.",
        "title_ld": "Elenca dispositivi audio",
        "desc_ld": "Esegue `list_devices.py` e elenca dispositivi nel log.",
        "title_lv": "Voci TTS filtrate",
        "desc_lv": "Elenca voci Edge filtrate (en/es/fr).",
        "title_n": "Muto microfono",
        "desc_n": "Attiva/disattiva il mute del microfono (Core Audio + gate app).",
        "title_p": "Apri cartella audio",
        "desc_p": "Apre la cartella dell'audio dell'**ultimo** chunk.",
        "title_pN": "Apri cartella audio N",
        "desc_pN": "Apre la cartella del **chunk N**. Esempio: `p5`.",
        "title_r": "Ripeti ultimo",
        "desc_r": "Riproduce il TTS dell'**ultimo** chunk. Riattiva il suono se OFF. Mostra source e target.",
        "title_rN": "Ripeti chunk N",
        "desc_rN": "Riproduce il TTS del **chunk N**. Esempio: `r133`. Target in verde sotto lo stato.",
        "title_s": "Suono ON/OFF",
        "desc_s": "Attiva/disattiva la riproduzione TTS live. Default OFF (solo testo).",
        "title_x": "Ferma riproduzione",
        "desc_x": "Interrompe il TTS corrente e svuota la coda audio.",
        "title_g": "Scambia lingue",
        "desc_g": "Inverte `SOURCE_LANG` ↔ `TARGET_LANG` a runtime (STT, traduttore, TTS).",
        "title_o": "Sinonimi / significato",
        "desc_o": "Cerca sinonimi o significato (IA). Chiede il termine se usato da solo.",
        "title_t": "Cambia lingua TARGET",
        "desc_t": "Imposta solo la lingua **target**. Esempio: `t EN`. Input in MAIUSCOLO.",
        "title_ctrl_c": "Copia selezione",
        "desc_ctrl_c": "Copia la selezione del log attivo; altrimenti tutto il log.",
        "title_ctrl_q": "Esci",
        "desc_ctrl_q": "Chiude l'app (come `q`).",
        "title_ctrl_shift_c": "Copia log intero",
        "desc_ctrl_shift_c": "Copia sempre tutto il log della scheda attiva.",
        "title_f1": "Aiuto (F1)",
        "desc_f1": "Stampa l'aiuto di avvio nella scheda **Sistema**.",
        "title_f2": "Bypass vocale (F2)",
        "desc_f2": "Attiva/disattiva bypass vocale (come `b` / badge bianco). Copia log: Ctrl+Shift+C.",
        "title_f3": "Cicla schede (F3)",
        "desc_f3": "Ciclo: **Tradução → Sistema → Novidades → Elenco comandi → …**.",
        "title_f4": "UI compatta (F4)",
        "desc_f4": "Nasconde il menu; mantiene la riga di comando. Come `u`.",
        "title_f5": "Auto-scroll Tradução (F5)",
        "desc_f5": (
            "Attiva/disattiva l'**auto-scroll** di entrambi i pannelli Tradução (LC + VOZ). "
            "ON = nuove righe in fondo. OFF = vista bloccata. **F5** o clic sul chip."
        ),
        "title_up_down": "Cronologia comandi",
        "desc_up_down": "↑ / ↓ scorrono i comandi precedenti.",
        "title_c": "Esporta Markdown",
        "desc_c": "Esporta la sessione in `.md`, con riepilogo IA opzionale.",
        "title_cls": "Pulisci log",
        "desc_cls": "Pulisce **Tradução** e **Sistema** (non Novidades / Elenco comandi).",
        "title_co": "Commenta ultimo",
        "desc_co": "Aggiunge un commento all'**ultimo** chunk (SQLite; `#id` in `l`).",
        "title_coN": "Commenta chunk N",
        "desc_coN": "Commenta il **chunk N**: `coN` o `coN testo`.",
        "title_codN": "Elimina commento #N",
        "desc_codN": "Elimina il commento per id. Esempio: `cod12`.",
        "title_d": "Elimina ultimo",
        "desc_d": "Elimina l'**ultimo** chunk (storico, DB, audio).",
        "title_dN": "Elimina chunk N",
        "desc_dN": "Elimina il **chunk N**. Esempio: `d8`.",
        "title_e": "Modifica ultimo",
        "desc_e": "Modifica l'**ultima** frase e ritraduce.",
        "title_eN": "Modifica chunk N",
        "desc_eN": "Modifica il **chunk N**. Esempio: `e4`.",
        "title_enew": "Nuovo testo (senza mic)",
        "desc_enew": "Traduce solo testo digitato. Esempio: `enew Ciao`. TTS segue `s`.",
        "title_f": "Preferito ultimo",
        "desc_f": "Segna l'**ultimo** chunk come preferito.",
        "title_fN": "Preferito chunk N",
        "desc_fN": "Segna il **chunk N** come preferito.",
        "title_F": "Elenca preferiti",
        "desc_F": "Mostra i preferiti della sessione.",
        "title_gg": "Vai in alto",
        "desc_gg": "Inizio del log attivo; auto-scroll off (`gg` / `gt`).",
        "title_GG": "Vai in fondo",
        "desc_GG": "Fine del log; auto-scroll on (`GG` / `gf`).",
        "title_l": "Elenca messaggi",
        "desc_l": "Elenca frasi con timing, timestamp, audio e commenti.",
        "title_lo": "Solo source",
        "desc_lo": "Solo righe **source**.",
        "title_lt": "Solo target",
        "desc_lt": "Solo righe **target**.",
        "title_m": "Mostra menu",
        "desc_m": "Mostra di nuovo il riepilogo del menu comandi.",
        "title_q": "Esci",
        "desc_q": "Termina la sessione. Stampa l'id per riprendere con `livelingo <id>`.",
        "title_u": "UI compatta",
        "desc_u": "Nasconde il menu multi-riga; tiene l'input. Come **F4**.",
        "title_v": "Cambia sessione",
        "desc_v": "Torna al selettore sessione (nuova / riprendi / elimina).",
    },
    "zh": {
        "tab": "命令列表",
        "intro": (
            "LiveLingo 全部菜单命令，按区域**分组**。"
            "组名与命令均按**字母顺序**排列。"
            "在下方输入命令并按 **Enter**。"
        ),
        "group_audio": "音频",
        "group_idiom": "语言",
        "group_keys": "键盘",
        "group_sentence": "句子",
        "group_session": "会话",
        "title_a": "复制音频路径",
        "desc_a": "将**最后**一个 chunk 音频的绝对路径复制到剪贴板。",
        "title_aN": "复制音频路径 N",
        "desc_aN": "复制 **chunk N** 音频路径。示例：`a12`。",
        "title_b": "语音直通",
        "desc_b": "切换：将原始麦克风送至输出（如 VB-Cable），**不经** STT/翻译。标题显示 `BYPASS`。别名：`bypass`、`hot`。",
        "title_ctts": "更换 TTS 音色",
        "desc_ctts": "更改 Edge TTS 音色：`ctts <ShortName>` 或仅 `ctts` 后输入。",
        "title_lav": "列出全部 TTS 音色",
        "desc_lav": "运行 `edge-tts --list-voices` 并输出完整列表。",
        "title_ld": "列出音频设备",
        "desc_ld": "运行 `list_devices.py` 并在日志中列出设备。",
        "title_lv": "筛选 TTS 音色",
        "desc_lv": "列出筛选后的 Edge 音色（en/es/fr）。",
        "title_n": "麦克风静音",
        "desc_n": "切换麦克风静音（Windows Core Audio + 应用门控）。",
        "title_p": "打开音频文件夹",
        "desc_p": "在资源管理器中打开**最后**一个 chunk 音频所在文件夹。",
        "title_pN": "打开音频文件夹 N",
        "desc_pN": "打开 **chunk N** 音频文件夹。示例：`p5`。",
        "title_r": "重播最后一条",
        "desc_r": "重播**最后** chunk 的 TTS。若声音关闭会重新打开。显示 source 与 target。",
        "title_rN": "重播 chunk N",
        "desc_rN": "重播 **chunk N** 的 TTS。示例：`r133`。target 以绿色显示在状态下方。",
        "title_s": "声音 开/关",
        "desc_s": "切换实时 TTS 播放。默认关（仅文本）。",
        "title_x": "停止播放",
        "desc_x": "停止当前 TTS 并清空音频队列。",
        "title_g": "交换语言",
        "desc_g": "运行时交换 `SOURCE_LANG` ↔ `TARGET_LANG`（STT、翻译、TTS）。不改写历史 chunk。",
        "title_o": "同义词 / 释义",
        "desc_o": "查询同义词或释义（AI）。单独使用时会提示输入词语。",
        "title_t": "更改 TARGET 语言",
        "desc_t": "仅设置**目标**语言。示例：`t EN`。此命令输入强制为大写。",
        "title_ctrl_c": "复制选区",
        "desc_ctrl_c": "复制活动日志中的选中文本；无选区则复制整页日志。",
        "title_ctrl_q": "退出",
        "desc_ctrl_q": "退出应用（与 `q` 类似）。",
        "title_ctrl_shift_c": "复制完整日志",
        "desc_ctrl_shift_c": "始终复制活动日志选项卡的全部内容。",
        "title_f1": "帮助 (F1)",
        "desc_f1": "在 **Sistema** 选项卡打印启动帮助。",
        "title_f2": "语音旁路 (F2)",
        "desc_f2": "切换语音旁路（同 `b` / 白色徽章）。复制日志：Ctrl+Shift+C。",
        "title_f3": "切换选项卡 (F3)",
        "desc_f3": "循环：**Tradução → Sistema → Novidades → 命令列表 → …**。",
        "title_f4": "紧凑界面 (F4)",
        "desc_f4": "隐藏命令菜单条，保留输入行。等同 `u`。",
        "title_f5": "自动滚动翻译窗 (F5)",
        "desc_f5": (
            "切换 **Tradução** 左右面板（LC + VOZ）的自动滚到底。 "
            "ON = 新行跟到底；OFF = 视口锁定。**F5** 或点击徽章。"
        ),
        "title_up_down": "命令历史",
        "desc_up_down": "在命令框中用 ↑ / ↓ 浏览历史命令。",
        "title_c": "导出 Markdown",
        "desc_c": "将会话导出为 `.md`，可选 AI 摘要。",
        "title_cls": "清空日志",
        "desc_cls": "清空 **Tradução** 与 **Sistema**（不清 Novidades / 命令列表）。",
        "title_co": "评论最后一条",
        "desc_co": "为**最后** chunk 添加注释（SQLite；列表 `l` 中显示 `#id`）。",
        "title_coN": "评论 chunk N",
        "desc_coN": "评论 **chunk N**：`coN` 或 `coN 文本`。",
        "title_codN": "删除评论 #N",
        "desc_codN": "按主键 id 删除评论。示例：`cod12`。",
        "title_d": "删除最后一条",
        "desc_d": "删除**最后** chunk（历史、数据库与音频）。",
        "title_dN": "删除 chunk N",
        "desc_dN": "删除 **chunk N**。示例：`d8`。",
        "title_e": "编辑最后一条",
        "desc_e": "编辑**最后**一句并重新翻译（可再生成音频）。",
        "title_eN": "编辑 chunk N",
        "desc_eN": "编辑 **chunk N**。示例：`e4`。",
        "title_enew": "新文本（无麦克风）",
        "desc_enew": "仅用键入文本排队翻译。示例：`enew 你好`。TTS 跟随 `s`。",
        "title_f": "收藏最后一条",
        "desc_f": "将**最后** chunk 标为收藏。",
        "title_fN": "收藏 chunk N",
        "desc_fN": "将 **chunk N** 标为收藏。",
        "title_F": "列出收藏",
        "desc_F": "显示本会话收藏短语。",
        "title_gg": "到顶部",
        "desc_gg": "跳到活动日志开头并关闭自动滚动（`gg` / `gt`）。",
        "title_GG": "到底部",
        "desc_GG": "跳到末尾并恢复自动滚动（`GG` / `gf`）。",
        "title_l": "列出消息",
        "desc_l": "列出会话短语及计时、时间戳、音频与评论。",
        "title_lo": "仅 source",
        "desc_lo": "仅列出 **source**（听到的）行。",
        "title_lt": "仅 target",
        "desc_lt": "仅列出 **target**（译文）行。",
        "title_m": "显示菜单",
        "desc_m": "再次显示命令菜单摘要。",
        "title_q": "退出",
        "desc_q": "结束会话并退出。会打印 session id 以便 `livelingo <id>` 恢复。",
        "title_u": "紧凑界面",
        "desc_u": "隐藏多行菜单，保留输入。等同 **F4**。",
        "title_v": "切换会话",
        "desc_v": "返回会话选择（新建 / 恢复 / 删除）。",
    },
    "ja": {
        "tab": "コマンド一覧",
        "intro": (
            "LiveLingo のメニューコマンドを領域ごとに**グループ化**。"
            "グループ名とコマンドは**アルファベット順**。"
            "下の欄にコマンドを入力して **Enter**。"
        ),
        "group_audio": "音声",
        "group_idiom": "言語",
        "group_keys": "キーボード",
        "group_sentence": "文",
        "group_session": "セッション",
        "title_a": "音声パスをコピー",
        "desc_a": "**最後**の chunk 音声の絶対パスをクリップボードへ。",
        "title_aN": "音声パス N をコピー",
        "desc_aN": "**chunk N** の音声パスをコピー。例: `a12`。",
        "title_b": "ボイスバイパス",
        "desc_b": "切替: マイク生音を出力（VB-Cable 等）へ、STT/翻訳**なし**。ヘッダ `BYPASS`。別名: `bypass`, `hot`。",
        "title_ctts": "TTS 音声変更",
        "desc_ctts": "Edge TTS 音声を変更。`ctts <ShortName>` または `ctts` のみ。",
        "title_lav": "全 TTS 音声一覧",
        "desc_lav": "`edge-tts --list-voices` を実行しログへ出力。",
        "title_ld": "音声デバイス一覧",
        "desc_ld": "`list_devices.py` を実行しデバイスをログへ。",
        "title_lv": "絞り込み TTS 音声",
        "desc_lv": "よく使うロケールの Edge 音声を一覧（en/es/fr）。",
        "title_n": "マイクミュート",
        "desc_n": "マイクのミュート切替（Core Audio + アプリゲート）。",
        "title_p": "音声フォルダを開く",
        "desc_p": "**最後**の chunk 音声フォルダを開く。",
        "title_pN": "音声フォルダ N を開く",
        "desc_pN": "**chunk N** のフォルダを開く。例: `p5`。",
        "title_r": "最後を再生",
        "desc_r": "**最後**の chunk の TTS を再生。OFF なら音を再 ON。source / target を表示。",
        "title_rN": "chunk N を再生",
        "desc_rN": "**chunk N** の TTS を再生。例: `r133`。target は緑で表示。",
        "title_s": "サウンド ON/OFF",
        "desc_s": "ライブ TTS の切替。既定 OFF（テキストのみ）。",
        "title_x": "再生停止",
        "desc_x": "現在の TTS を止め、キューを空にする。",
        "title_g": "言語スワップ",
        "desc_g": "実行中に `SOURCE_LANG` ↔ `TARGET_LANG` を入替（STT・翻訳・TTS）。過去 chunk は変更しない。",
        "title_o": "類語 / 意味",
        "desc_o": "類語や意味を調べる（AI）。単体では用語を尋ねる。",
        "title_t": "TARGET 言語変更",
        "desc_t": "**ターゲット**言語のみ設定。例: `t EN`。このコマンドは大文字入力。",
        "title_ctrl_c": "選択をコピー",
        "desc_ctrl_c": "アクティブログの選択範囲をコピー。未選択なら全ログ。",
        "title_ctrl_q": "終了",
        "desc_ctrl_q": "アプリ終了（`q` と同様）。",
        "title_ctrl_shift_c": "ログ全体をコピー",
        "desc_ctrl_shift_c": "アクティブタブのログ全体を常にコピー。",
        "title_f1": "ヘルプ (F1)",
        "desc_f1": "起動ヘルプを **Sistema** タブに表示。",
        "title_f2": "音声バイパス (F2)",
        "desc_f2": "音声バイパス切替（`b` / 白バッジと同じ）。ログコピー: Ctrl+Shift+C。",
        "title_f3": "タブ切替 (F3)",
        "desc_f3": "循環: **Tradução → Sistema → Novidades → コマンド一覧 → …**。",
        "title_f4": "コンパクト UI (F4)",
        "desc_f4": "メニューを隠し入力行は残す。`u` と同じ。",
        "title_f5": "Tradução 自動スクロール (F5)",
        "desc_f5": (
            "Tradução 両ペイン（LC + VOZ）の**自動下端スクロール**を切替。 "
            "ON = 新行で下へ。OFF = 位置固定。**F5** またはチップをクリック。"
        ),
        "title_up_down": "コマンド履歴",
        "desc_up_down": "コマンド欄で ↑ / ↓ で履歴を辿る。",
        "title_c": "Markdown 書き出し",
        "desc_c": "セッションを `.md` に書き出し（任意で AI 要約）。",
        "title_cls": "ログ消去",
        "desc_cls": "**Tradução** と **Sistema** を消去（Novidades / コマンド一覧は対象外）。",
        "title_co": "最後にコメント",
        "desc_co": "**最後**の chunk にコメント（SQLite；`l` で `#id`）。",
        "title_coN": "chunk N にコメント",
        "desc_coN": "**chunk N** にコメント: `coN` または `coN 文`。",
        "title_codN": "コメント #N 削除",
        "desc_codN": "主キー id でコメント削除。例: `cod12`。",
        "title_d": "最後を削除",
        "desc_d": "**最後**の chunk を削除（履歴・DB・音声）。",
        "title_dN": "chunk N を削除",
        "desc_dN": "**chunk N** を削除。例: `d8`。",
        "title_e": "最後を編集",
        "desc_e": "**最後**の文を編集し再翻訳（音声再生成可）。",
        "title_eN": "chunk N を編集",
        "desc_eN": "**chunk N** を編集。例: `e4`。",
        "title_enew": "新規テキスト（マイクなし）",
        "desc_enew": "入力テキストのみ翻訳。例: `enew こんにちは`。TTS は `s` に従う。",
        "title_f": "最後をお気に入り",
        "desc_f": "**最後**の chunk をお気に入りに。",
        "title_fN": "chunk N をお気に入り",
        "desc_fN": "**chunk N** をお気に入りに。",
        "title_F": "お気に入り一覧",
        "desc_F": "セッションのお気に入りを表示。",
        "title_gg": "先頭へ",
        "desc_gg": "アクティブログ先頭へ。自動スクロール OFF（`gg` / `gt`）。",
        "title_GG": "末尾へ",
        "desc_GG": "末尾へ。自動スクロール ON（`GG` / `gf`）。",
        "title_l": "メッセージ一覧",
        "desc_l": "フレーズを timing・時刻・音声・コメント付きで一覧。",
        "title_lo": "source のみ",
        "desc_lo": "**source** 行のみ。",
        "title_lt": "target のみ",
        "desc_lt": "**target** 行のみ。",
        "title_m": "メニュー表示",
        "desc_m": "コマンドメニュー要約を再表示。",
        "title_q": "終了",
        "desc_q": "セッション終了。再開用に session id を表示（`livelingo <id>`）。",
        "title_u": "コンパクト UI",
        "desc_u": "複数行メニューを隠し入力は残す。**F4** と同じ。",
        "title_v": "セッション切替",
        "desc_v": "セッション選択に戻る（新規 / 再開 / 削除）。",
    },
}


def _pack_for(lang: str) -> dict[str, str]:
    code = (lang or "en").lower().strip()
    if "-" in code:
        code = code.split("-", 1)[0]
    if code in ("cn", "zh-cn", "zh-tw", "cmn"):
        code = "zh"
    if code in ("jp",):
        code = "ja"
    if code in ("ger", "deu"):
        code = "de"
    if code in ("ita",):
        code = "it"
    if code in ("por", "pt-br", "pt_br"):
        code = "pt"
    base = dict(_I18N["en"])
    pack = _I18N.get(code) or {}
    base.update(pack)
    return base


def tab_title(lang: str = "en") -> str:
    """Localized tab label for the Command list tab."""
    return _pack_for(lang).get("tab", "Command list")


def build_commands_markdown(lang: str = "en") -> str:
    """
    Build a Markdown document: groups A–Z by localized group title,
    commands A–Z within each group by sort key.
    """
    pack = _pack_for(lang)
    # Bucket commands by group
    by_group: dict[str, list[dict[str, str]]] = {g: [] for g in _GROUP_IDS}
    for cmd in _COMMANDS:
        g = cmd["group"]
        if g in by_group:
            by_group[g].append(cmd)

    # Sort groups by localized title (accent-insensitive A–Z)
    group_order = sorted(
        _GROUP_IDS,
        key=lambda gid: _alpha_key(pack.get(f"group_{gid}", gid) or gid),
    )

    lines: list[str] = [
        f"# {pack.get('tab', 'Command list')}",
        "",
        pack.get("intro", ""),
        "",
        "---",
        "",
    ]

    for gid in group_order:
        gtitle = pack.get(f"group_{gid}", gid)
        lines.append(f"## {gtitle}")
        lines.append("")
        cmds = sorted(
            by_group.get(gid) or [],
            key=lambda c: _alpha_key(c.get("sort") or c["token"]),
        )
        for cmd in cmds:
            cid = cmd["id"]
            token = cmd["token"]
            title = pack.get(f"title_{cid}", token)
            desc = pack.get(f"desc_{cid}", "")
            # Markdown: ### `[token]` — short title
            lines.append(f"### `[{token}]` — {title}")
            lines.append("")
            if desc:
                lines.append(desc)
                lines.append("")
        lines.append("---")
        lines.append("")

    # Drop trailing separator whitespace
    while lines and lines[-1] in ("", "---"):
        lines.pop()
    lines.append("")
    return "\n".join(lines)
