"""
devices.py
==========
Audio-device discovery and resolution helpers built on top of sounddevice
(PortAudio). Used by both `list_devices.py` and the main app.

A "device spec" coming from config can be:
  * "" or None  -> default device
  * an int / a numeric string -> a device index
  * any other string -> a case-insensitive substring of the device name
"""

import sounddevice as sd

# Common VB-Audio Virtual Cable playback-side names. The first that matches a
# real device wins. ("CABLE Input" is the playback side that apps feed; the
# matching "CABLE Output" is what Teams/Zoom pick as a microphone.)
VBCABLE_PLAYBACK_HINTS = ("CABLE Input", "VB-Audio Virtual Cable", "VB-Audio Point")


def query_devices():
    """Return the full list of devices (list of dicts)."""
    return sd.query_devices()


def query_hostapis():
    return sd.query_hostapis()


def default_input_index():
    """Index of the default input device, or None."""
    try:
        idx = sd.default.device[0]
        return idx if idx is not None and idx >= 0 else None
    except Exception:
        return None


def default_output_index():
    """Index of the default output device, or None."""
    try:
        idx = sd.default.device[1]
        return idx if idx is not None and idx >= 0 else None
    except Exception:
        return None


def summary_rows():
    """
    Return display rows for every device:
        (index, name, max_input_channels, max_output_channels, hostapi_name)
    """
    hostapis = query_hostapis()
    rows = []
    for idx, dev in enumerate(query_devices()):
        rows.append(
            (
                idx,
                dev["name"],
                dev["max_input_channels"],
                dev["max_output_channels"],
                hostapis[dev["hostapi"]]["name"],
            )
        )
    return rows


def device_name(index):
    """Human-readable name for a device index, or 'default' for None."""
    if index is None:
        return "system default"
    try:
        return sd.query_devices(index)["name"]
    except Exception:
        return f"#{index}"


def is_cable_like_output(index) -> bool:
    """True if device looks like VB-Cable *playback* (would leak into Teams mic)."""
    if index is None:
        return False
    try:
        name = (device_name(index) or "").lower()
    except Exception:
        return False
    # Playback side apps write to; Teams then hears via "CABLE Output"
    hints = (
        "cable input",
        "vb-audio virtual cable",
        "vb-audio point",
        "cable-a input",
        "cable-b input",
        "voicemeeter input",  # also a virtual path into calls
    )
    return any(h in name for h in hints)


def resolve_monitor_output(spec="", *, exclude_index=None):
    """
    Resolve headphones for monitor/cue.

    If ``spec`` is set (e.g. ``\"13\"``), use that device only — never silently
    fall back to another index (was sending cue to device 1 instead of 13).
    """
    from .monitor_cue import resolve_headphones

    idx, _name = resolve_headphones(spec, cable_index=exclude_index)
    return idx


def _matches_kind(dev, kind):
    if kind == "input":
        return dev["max_input_channels"] >= 1
    if kind == "output":
        return dev["max_output_channels"] >= 1
    return True


def _hostapi_name(dev) -> str:
    try:
        return str(query_hostapis()[dev["hostapi"]]["name"] or "")
    except Exception:
        return ""


def _prefer_match(matches, prefer_hostapi: str | None):
    """
    Pick among (idx, dev) matches.

    prefer_hostapi:
      - \"wasapi\" → Windows WASAPI (needed for exclusive capture)
      - \"mme\" / None → MME first (most compatible shared mode)
    """
    if not matches:
        return None
    hostapis = query_hostapis()
    pref = (prefer_hostapi or "").strip().lower()
    if pref in ("wasapi", "windows wasapi"):
        wasapi = next(
            (
                (idx, dev)
                for idx, dev in matches
                if "wasapi" in hostapis[dev["hostapi"]]["name"].lower()
            ),
            None,
        )
        if wasapi is not None:
            return wasapi
    # Default / mme: prefer MME when several APIs expose the same name
    mme = next(
        (
            (idx, dev)
            for idx, dev in matches
            if "mme" in hostapis[dev["hostapi"]]["name"].lower()
        ),
        None,
    )
    return mme if mme is not None else matches[0]


def find_wasapi_twin(index):
    """
    Given a device index, return a WASAPI device with the same name (same kind),
    or (None, None) if none exists.
    """
    if index is None:
        return None, None
    try:
        base = sd.query_devices(index)
    except Exception:
        return None, None
    name = str(base.get("name") or "")
    if not name:
        return None, None
    kind = "input" if int(base.get("max_input_channels") or 0) >= 1 else "output"
    name_low = name.lower()
    matches = [
        (i, d)
        for i, d in enumerate(query_devices())
        if _matches_kind(d, kind) and d["name"].lower() == name_low
    ]
    chosen = _prefer_match(matches, "wasapi")
    if chosen is None:
        return None, None
    idx, dev = chosen
    if "wasapi" not in _hostapi_name(dev).lower():
        return None, None
    return idx, dev["name"]


def resolve_device(spec, kind, *, prefer_hostapi: str | None = None):
    """
    Resolve a device spec into a concrete device index (or None for default).

    Returns (index_or_None, name_string). Raises ValueError if a non-empty
    spec cannot be matched to a device of the requested kind.

    prefer_hostapi: \"wasapi\" | \"mme\" | None — which PortAudio host API to
    prefer when the same name appears under several APIs.
    """
    prefer = (prefer_hostapi or "").strip().lower() or None

    # Empty / None -> system default (optionally retarget to WASAPI twin).
    if spec is None or str(spec).strip() == "":
        idx = default_input_index() if kind == "input" else None
        if prefer in ("wasapi", "windows wasapi") and idx is not None:
            twin_idx, twin_name = find_wasapi_twin(idx)
            if twin_idx is not None:
                return twin_idx, twin_name
        return idx, device_name(idx)

    spec = str(spec).strip()

    # Numeric -> treat as an index and validate it.
    if spec.lstrip("-").isdigit():
        idx = int(spec)
        try:
            dev = sd.query_devices(idx)
        except Exception as exc:
            raise ValueError(f"No audio device with index {idx}") from exc
        if not _matches_kind(dev, kind):
            raise ValueError(f"Device #{idx} '{dev['name']}' has no {kind} channels.")
        # If caller wants WASAPI exclusive but picked an MME index, retarget twin
        if prefer in ("wasapi", "windows wasapi"):
            if "wasapi" not in _hostapi_name(dev).lower():
                twin_idx, twin_name = find_wasapi_twin(idx)
                if twin_idx is not None:
                    return twin_idx, twin_name
        return idx, dev["name"]

    # Otherwise: case-insensitive substring match among devices of this kind.
    spec_low = spec.lower()
    matches = [
        (i, d)
        for i, d in enumerate(query_devices())
        if _matches_kind(d, kind) and spec_low in d["name"].lower()
    ]
    if not matches:
        raise ValueError(
            f"No {kind} device whose name contains '{spec}'. "
            f"Run `python list_devices.py` to see available devices."
        )
    chosen = _prefer_match(matches, prefer)
    chosen_idx, chosen_dev = chosen
    return chosen_idx, chosen_dev["name"]


def wasapi_exclusive_settings(cfg=None):
    """
    Return ``sounddevice.WasapiSettings(exclusive=True)`` when exclusive capture
    is enabled and the backend supports it; otherwise None.
    """
    enabled = True
    if cfg is not None:
        enabled = bool(getattr(cfg, "CAPTURE_WASAPI_EXCLUSIVE", False))
    if not enabled:
        return None
    try:
        import sounddevice as _sd

        settings_cls = getattr(_sd, "WasapiSettings", None)
        if settings_cls is None:
            return None
        return settings_cls(exclusive=True)
    except Exception:
        return None


def input_stream_extra_settings(cfg=None, *, force_shared: bool = False):
    """Kwarg dict for InputStream: may include ``extra_settings`` for WASAPI."""
    if force_shared:
        return {}
    settings = wasapi_exclusive_settings(cfg)
    if settings is None:
        return {}
    return {"extra_settings": settings}


def find_vbcable_output():
    """
    Return (index, name) of the VB-Cable playback device, or (None, None) if it
    is not installed.
    """
    devices = query_devices()
    for hint in VBCABLE_PLAYBACK_HINTS:
        hint_low = hint.lower()
        for idx, dev in enumerate(devices):
            if dev["max_output_channels"] >= 1 and hint_low in dev["name"].lower():
                return idx, dev["name"]
    return None, None


VBCABLE_INSTALL_MESSAGE = """\
VB-Cable (the virtual audio cable) was not found on this system.

The tool needs it to expose the translated speech as a microphone.

  1. Download VB-CABLE (free) from:
         https://vb-audio.com/Cable/
  2. Unzip it, right-click "VBCABLE_Setup_x64.exe" -> "Run as administrator".
  3. Click "Install Driver", then REBOOT Windows.
  4. After reboot, run `python list_devices.py` — you should now see
     "CABLE Input (VB-Audio Virtual Cable)" in the OUTPUT devices.

Then start this tool again.
"""
