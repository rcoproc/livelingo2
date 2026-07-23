"""
mic_control.py
==============
Windows microphone mute via Core Audio (pycaw).

- get_mute / set_mute / toggle_mute on the capture endpoint matching
  LiveLingo's INPUT device (or the Windows default recording device).
- Graceful no-op stubs on non-Windows or when pycaw/COM is unavailable.

Also exposes a lightweight RMS "dead signal" check for soft warnings when
the OS mute flag is not set but the stream is silent.
"""

from __future__ import annotations

import platform
import re
import threading
from typing import Optional, Tuple

_IS_WINDOWS = platform.system() == "Windows"

# Optional Windows deps — imported lazily so Linux installs stay clean.
_pycaw_ok: Optional[bool] = None
_import_error: Optional[str] = None


def _try_import_pycaw():
    global _pycaw_ok, _import_error
    if _pycaw_ok is not None:
        return _pycaw_ok
    if not _IS_WINDOWS:
        _pycaw_ok = False
        _import_error = "not Windows"
        return False
    try:
        from ctypes import POINTER, cast  # noqa: F401

        from comtypes import CLSCTX_ALL  # noqa: F401
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # noqa: F401

        _pycaw_ok = True
        _import_error = None
    except Exception as exc:  # pragma: no cover
        _pycaw_ok = False
        _import_error = str(exc)
    return _pycaw_ok


def available() -> bool:
    """True if OS-level mic mute control is usable on this machine."""
    return bool(_try_import_pycaw())


def availability_note() -> str:
    if available():
        return "Windows Core Audio (pycaw)"
    if not _IS_WINDOWS:
        return "OS mic mute only on Windows"
    return f"pycaw unavailable ({_import_error or 'unknown'})"


def _normalize_name(name: str) -> str:
    text = (name or "").lower()
    # Drop common PortAudio host-API suffixes / parenthetical noise for matching.
    text = re.sub(
        r"\s*\((mme|wasapi|directsound|wdm-ks|windows\s*wasapi)\)\s*$", "", text
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _match_score(target: str, candidate: str) -> float:
    t = _normalize_name(target)
    c = _normalize_name(candidate)
    if not t or not c:
        return 0.0
    if t == c:
        return 100.0
    if t in c or c in t:
        return 85.0
    tt, ct = set(t.split()), set(c.split())
    # Drop ultra-generic tokens that match every mic.
    noise = {"audio", "microphone", "microfone", "mic", "input", "device", "realtek"}
    tt2 = tt - noise
    ct2 = ct - noise
    if tt2 and ct2:
        return 60.0 * len(tt2 & ct2) / max(1, len(tt2))
    if tt and ct:
        return 40.0 * len(tt & ct) / max(1, len(tt))
    return 0.0


def _endpoint_volume(imm_device):
    """Activate IAudioEndpointVolume on a raw IMMDevice."""
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    iface = imm_device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _list_capture_endpoints():
    """
    Yield (friendly_name, imm_device, is_default) for active capture endpoints.
    """
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    enum = AudioUtilities.GetDeviceEnumerator()
    col = enum.EnumAudioEndpoints(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
    default_id = None
    try:
        default_mic = AudioUtilities.GetMicrophone()
        default_id = default_mic.GetId()
    except Exception:
        default_id = None

    for i in range(col.GetCount()):
        imm = col.Item(i)
        try:
            ad = AudioUtilities.CreateDevice(imm)
            name = getattr(ad, "FriendlyName", None) or ""
        except Exception:
            name = ""
        is_default = False
        try:
            is_default = bool(default_id and imm.GetId() == default_id)
        except Exception:
            is_default = False
        yield name, imm, is_default


def resolve_capture_endpoint(device_name: Optional[str] = None):
    """
    Find the best matching capture IMMDevice for a PortAudio/device name.

    Returns (friendly_name, imm_device) or (None, None).
    """
    if not _try_import_pycaw():
        return None, None

    endpoints = list(_list_capture_endpoints())
    if not endpoints:
        return None, None

    if not device_name or not str(device_name).strip():
        for name, imm, is_default in endpoints:
            if is_default:
                return name or "default microphone", imm
        name, imm, _ = endpoints[0]
        return name or "microphone", imm

    best = None  # (score, name, imm)
    for name, imm, is_default in endpoints:
        score = _match_score(device_name, name)
        if is_default:
            score += 5.0  # slight bias when names are ambiguous
        if best is None or score > best[0]:
            best = (score, name, imm)

    if best is None or best[0] < 25.0:
        # Fall back to Windows default recording device.
        for name, imm, is_default in endpoints:
            if is_default:
                return name or "default microphone", imm
        name, imm, _ = endpoints[0]
        return name or "microphone", imm

    return best[1] or device_name, best[2]


class MicController:
    """
    Thread-safe controller for one capture device's OS mute + app-level gate.

    App gate: even if COM fails, LiveLingo can stop emitting speech chunks.
    OS mute: Windows tray / other apps see the same mute (best Windows mode).
    """

    def __init__(self, device_name: Optional[str] = None):
        self.device_name = device_name or ""
        self._lock = threading.RLock()
        self._app_muted = False
        self._resolved_name: Optional[str] = None
        self._endpoint = None  # raw IMMDevice cache
        self._vol = None  # IAudioEndpointVolume cache

    def _ensure_endpoint(self):
        if not _try_import_pycaw():
            return False
        if self._vol is not None:
            return True
        name, imm = resolve_capture_endpoint(self.device_name)
        if imm is None:
            return False
        try:
            self._vol = _endpoint_volume(imm)
            self._endpoint = imm
            self._resolved_name = name
            return True
        except Exception:
            self._vol = None
            self._endpoint = None
            return False

    def resolved_name(self) -> str:
        with self._lock:
            self._ensure_endpoint()
            return self._resolved_name or self.device_name or "default microphone"

    def os_control_available(self) -> bool:
        with self._lock:
            return self._ensure_endpoint()

    def get_os_mute(self) -> Optional[bool]:
        """Return OS mute flag, or None if unavailable."""
        with self._lock:
            if not self._ensure_endpoint():
                return None
            try:
                return bool(self._vol.GetMute())
            except Exception:
                # Stale COM pointer — drop cache and retry once.
                self._vol = None
                self._endpoint = None
                if not self._ensure_endpoint():
                    return None
                try:
                    return bool(self._vol.GetMute())
                except Exception:
                    return None

    def get_os_volume_scalar(self) -> Optional[float]:
        with self._lock:
            if not self._ensure_endpoint():
                return None
            try:
                return float(self._vol.GetMasterVolumeLevelScalar())
            except Exception:
                return None

    def set_os_mute(self, muted: bool) -> bool:
        """Set Windows mute. Returns True if the COM call succeeded."""
        with self._lock:
            if not self._ensure_endpoint():
                return False
            try:
                self._vol.SetMute(1 if muted else 0, None)
                return True
            except Exception:
                self._vol = None
                self._endpoint = None
                if not self._ensure_endpoint():
                    return False
                try:
                    self._vol.SetMute(1 if muted else 0, None)
                    return True
                except Exception:
                    return False

    def is_app_muted(self) -> bool:
        with self._lock:
            return self._app_muted

    def set_app_muted(self, muted: bool):
        with self._lock:
            self._app_muted = bool(muted)

    def is_muted(self) -> bool:
        """
        LiveLingo mute = **app gate only** (comando [n]).

        OS tray mute is reported separately (diagnose / warn_if_muted) and must
        NOT pause VOZ escuta — only [n] intentionally stops listening. Mixing
        OS mute into this flag made "escuta morrer" after TTS when Windows
        reported mute on a mismatched endpoint; [n] seemed to "fix" it.
        """
        with self._lock:
            return bool(self._app_muted)

    def is_os_muted(self) -> Optional[bool]:
        """Windows Core Audio mute flag, or None if unavailable."""
        return self.get_os_mute()

    def toggle(self) -> Tuple[bool, bool, str]:
        """
        Toggle **app** mute ([n]) and mirror to OS tray when possible.

        Returns (now_muted, os_ok, endpoint_name).
        """
        with self._lock:
            target = not bool(self._app_muted)
            os_ok = self.set_os_mute(target)
            self._app_muted = target
            name = self.resolved_name()
            return target, os_ok, name

    def set_muted(self, muted: bool) -> Tuple[bool, bool, str]:
        with self._lock:
            os_ok = self.set_os_mute(bool(muted))
            self._app_muted = bool(muted)
            return self._app_muted, os_ok, self.resolved_name()

    def diagnose(self) -> dict:
        """Snapshot for startup warnings."""
        with self._lock:
            os_mute = self.get_os_mute()
            vol = self.get_os_volume_scalar()
            return {
                "available": self.os_control_available(),
                "backend": availability_note(),
                "endpoint": self.resolved_name(),
                "os_mute": os_mute,
                "app_mute": self._app_muted,
                "volume": vol,
                # LiveLingo paused only via [n]; OS mute/vol still warned at start
                "effectively_muted": bool(self._app_muted)
                or (os_mute is True)
                or (vol is not None and vol <= 0.001),
            }


def warn_if_muted(controller: MicController, log_warn, log_info=None) -> bool:
    """
    Startup / pre-listen check. Returns True if a warning was emitted.
    """
    info = controller.diagnose()
    if not info["available"]:
        if log_info and _IS_WINDOWS:
            log_info(
                f"Mic mute control unavailable ({info['backend']}). "
                f"[n] will use app-level gate only."
            )
        return False

    name = info["endpoint"]
    if info["os_mute"]:
        log_warn(
            f"Microfone mutado no Windows: '{name}'. Desmute no tray ou pressione [n]."
        )
        return True
    vol = info["volume"]
    if vol is not None and vol <= 0.001:
        log_warn(
            f"Microfone com volume ~0%: '{name}'. "
            f"Aumente o nível em Configurações de Som ou use [n] após ajustar."
        )
        return True
    return False
