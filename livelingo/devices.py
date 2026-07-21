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


def _matches_kind(dev, kind):
    if kind == "input":
        return dev["max_input_channels"] >= 1
    if kind == "output":
        return dev["max_output_channels"] >= 1
    return True


def resolve_device(spec, kind):
    """
    Resolve a device spec into a concrete device index (or None for default).

    Returns (index_or_None, name_string). Raises ValueError if a non-empty
    spec cannot be matched to a device of the requested kind.
    """
    # Empty / None -> system default.
    if spec is None or str(spec).strip() == "":
        idx = default_input_index() if kind == "input" else None
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
    # Prefer an MME device when several host APIs expose the same name; MME is
    # the most compatible host API for simple playback on Windows.
    hostapis = query_hostapis()
    mme = next(
        (
            (idx, dev)
            for idx, dev in matches
            if "mme" in hostapis[dev["hostapi"]]["name"].lower()
        ),
        None,
    )
    chosen_idx, chosen_dev = mme if mme is not None else matches[0]
    return chosen_idx, chosen_dev["name"]


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
