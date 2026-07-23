"""
Webcam → lip-sync → virtual camera service (3-thread low-latency pipeline).

Threads
-------
1. **Capture (producer)** — ``cv2.VideoCapture``; always keeps the latest frame.
2. **Infer (worker)** — Face ROI + audio ring + lip engine; queue maxsize=2 drop-old.
3. **Emit (consumer)** — soft blend + ``pyvirtualcam`` send (target 30 FPS).

Design choices vs naive loop
----------------------------
* Drop-latest queues: if inference stalls, we skip intermediate frames instead of
  growing latency (lip-sync prefers *current* face + *recent* audio).
* TTS audio is injected from LiveLingo's playback path (not Cable loopback) →
  lower latency and no extra WASAPI/PipeWire graph.
* Engines are pluggable (passthrough / amplitude / ONNX FP16 CUDA|TRT).
* Optional deps are lazy; service stays ``None`` when packages missing.
* Virtual cam opens even if the physical camera is slow/failed (placeholder)
  so Teams can list the device; frames resize to the vcam size.

Commands: ``[cam]`` toggle · ``[cam on|off|status]``.

Resource lifecycle
------------------
* ``[cam on]`` / ``enable()`` — open physical capture + virtual cam (exclusive devices).
* ``[cam off]`` / ``disable()`` — **release** physical cam and virtual cam so other apps
  (Teams, OBS, Zoom) can use them; worker threads stay alive for a fast re-enable.
* ``stop()`` — full teardown (app exit / WEBCAM off). Does not touch audio/STT pipeline.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

import numpy as np

from .audio_ring import AudioRingBuffer
from .engines import LipSyncEngine, build_engine
from .face_roi import FaceMouthROI
from .mouth_template import (
    ClosedMouthTemplateStore,
    align_and_blend,
    compute_freeze_plate_geom,
    cover_frame_with_closed_image,
    load_template,
    open_from_closed_template,
    save_template_from_frame,
)

def check_webcam_deps() -> dict:
    """
    Return availability of optional packages.

    Uses ``importlib.util.find_spec`` for heavy deps (esp. mediapipe) to avoid
    native crashes during probe on some Windows/Python builds; full import still
    happens when the webcam path actually starts.
    """
    import importlib.util

    out = {
        "cv2": False,
        "mediapipe": False,
        "pyvirtualcam": False,
        "onnxruntime": False,
        "errors": {},
    }

    def _probe(name: str, key: str) -> None:
        try:
            if importlib.util.find_spec(name) is not None:
                out[key] = True
            else:
                out["errors"][key] = f"{name} not installed"
        except Exception as exc:
            out["errors"][key] = str(exc)

    _probe("cv2", "cv2")
    _probe("mediapipe", "mediapipe")
    _probe("pyvirtualcam", "pyvirtualcam")
    _probe("onnxruntime", "onnxruntime")
    return out


def teams_setup_hint() -> str:
    """Short checklist for Teams A/V (video ≠ audio path)."""
    return (
        "Teams: câmera = OBS Virtual Camera | "
        "mic = CABLE Output | "
        "LiveLingo [s] ON + OUTPUT_DEVICE=CABLE Input"
    )


def _windows_process_running(image_name: str) -> bool:
    """True if a Windows process with this image name is running (best-effort)."""
    if sys.platform != "win32":
        return False
    name = (image_name or "").strip().lower()
    if not name:
        return False
    try:
        import subprocess

        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            text=True,
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return name in (out or "").lower()
    except Exception:
        return False


def obs_virtual_cam_conflict_hint() -> str:
    """
    Hint when pyvirtualcam cannot start OBS Virtual Camera.

    OBS and pyvirtualcam are exclusive *producers* of the same device.
    OBS may stay open for scenes, but Virtual Camera must be STOPPED while
    LiveLingo owns the feed.
    """
    parts = [
        "OBS Virtual Camera is exclusive: only ONE app can *output* to it.",
        "In OBS: click 'Stop Virtual Camera' (Tools / controls) - do NOT leave it started.",
        "OBS app may stay open; only the virtual-cam button must be OFF.",
        "First-time setup: Start Virtual Camera once (as Admin) to register the driver, "
        "then Stop it, then run LiveLingo.",
        "Close other producers (Snap Camera, ManyCam, old LiveLingo).",
        "Windows Privacy -> Camera -> allow desktop apps.",
        'Test: python -c "import pyvirtualcam;c=pyvirtualcam.Camera(640,480,30);print(c.device);c.close()"',
    ]
    if _windows_process_running("obs64.exe") or _windows_process_running("obs32.exe"):
        parts.insert(
            0,
            "OBS is running now - most common fail: Virtual Camera still STARTED in OBS.",
        )
    return " ".join(parts)


@dataclass
class _FramePacket:
    frame_bgr: np.ndarray
    t_capture: float


@dataclass
class WebcamStatus:
    running: bool = False
    enabled: bool = False
    fps_out: float = 0.0
    fps_cap: float = 0.0
    face_ok: bool = False
    engine: str = ""
    error: Optional[str] = None
    width: int = 0
    height: int = 0
    backend: str = ""
    audio_rms: float = 0.0
    frames_sent: int = 0
    capture_ok: bool = False
    # emit pipeline phase for diagnostics (idle|wait_enable|opening_vcam|live|failed)
    emit_phase: str = "idle"


class WebcamLipSyncService:
    """
    Lifecycle-managed webcam lip-sync → virtual camera.

    Thread-safe ``push_tts_audio`` from the LiveLingo playback thread.
    """

    def __init__(
        self,
        config,
        log: Callable[..., None] = print,
        on_status: Optional[Callable[[dict], None]] = None,
    ):
        self.cfg = config
        self._log = log
        self._on_status = on_status

        self._stop = threading.Event()
        self._enabled = threading.Event()
        self._capture_failed = threading.Event()
        self._vcam_ready = threading.Event()
        if bool(getattr(config, "WEBCAM_START_ENABLED", False)):
            self._enabled.set()

        sr = int(getattr(config, "WEBCAM_AUDIO_SR", 24000) or 24000)
        self.audio = AudioRingBuffer(
            sample_rate=sr,
            max_seconds=float(getattr(config, "WEBCAM_AUDIO_RING_S", 2.0) or 2.0),
        )
        # Small delay so morph tracks Cable Out (device open / first buffer lag).
        try:
            self.audio.set_play_delay(
                float(getattr(config, "WEBCAM_AUDIO_PLAY_DELAY_S", 0.08) or 0.0)
            )
        except Exception:
            pass
        self.engine: LipSyncEngine = build_engine(config, log=log)
        self.roi = FaceMouthROI(
            pad_ratio=float(getattr(config, "WEBCAM_ROI_PAD", 0.35) or 0.35),
            feather_px=int(getattr(config, "WEBCAM_FEATHER_PX", 9) or 9),
        )
        self._sync_marker = bool(getattr(config, "WEBCAM_SYNC_MARKER", False))
        # Closed mouth when idle; open/simulate only while TTS → Teams.
        self._force_closed_idle = bool(
            getattr(config, "WEBCAM_FORCE_CLOSED_IDLE", True)
        )
        self._amp_sensitivity = float(
            getattr(config, "WEBCAM_AMP_SENSITIVITY", 28.0) or 28.0
        )
        self._template_store = ClosedMouthTemplateStore()
        if self._template_store.load_from_config(config):
            log(
                f"Closed-mouth template loaded: {self._template_store.path()} "
                "(only while mic listening, off during TTS)"
            )
        # Mirror template vs live (Teams often looks flipped vs OpenCV)
        self._template_flip_h = bool(
            getattr(config, "WEBCAM_TEMPLATE_FLIP_H", False)
        )
        # VAD: True only while mic is *hearing speech* (not just unmuted)
        self._vad_speech = threading.Event()
        self._vad_lock = threading.Lock()
        self._vad_hangover_s = float(
            getattr(config, "WEBCAM_SPEECH_HANGOVER_S", 1.5) or 0.0
        )
        self._vad_end_mono = 0.0
        self._listening_probe: Optional[Callable[[], bool]] = None  # legacy unused
        # F10 manual closed-mouth: when _closed_manual_mode, VAD auto is ignored
        self._closed_manual_mode = False
        self._closed_manual_on = False
        self._closed_gen = 0  # increments every F10 so status/debug can see toggles
        # F11: full-frame freeze with closed photo (hides all live video)
        self._closed_full_frame_on = False
        self._closed_full_gen = 0
        # VAD auto closed-plate removed — F10/F11 only (config kept for status/help)
        self._closed_auto = False
        self._closed_apply_ok = 0  # frames successfully blended (debug)
        self._closed_apply_fail = 0
        self._last_frame_lock = threading.Lock()
        self._last_frame_bgr = None  # for cam snap closed
        # Burn-in TARGET (translated) text on vcam frames — toggle [sub] / [cam sub]
        self._subtitle_lock = threading.Lock()
        self._subtitle_enabled = bool(getattr(config, "WEBCAM_SUBTITLE", False))
        self._subtitle_text = ""
        self._subtitle_set_mono = 0.0
        self._subtitle_gen = 0  # increments on each replace (debug / status)
        # 0 = stay until [sub off] or next push_subtitle_text (no auto-hide)
        self._subtitle_hold_s = float(
            getattr(config, "WEBCAM_SUBTITLE_HOLD_S", 0.0) or 0.0
        )
        # 2 lines: one caption at a time (wrap only); never stack old+new
        self._subtitle_max_lines = int(
            getattr(config, "WEBCAM_SUBTITLE_MAX_LINES", 2) or 2
        )
        self._subtitle_font_scale = float(
            getattr(config, "WEBCAM_SUBTITLE_FONT_SCALE", 0.0) or 0.0
        )
        self._subtitle_margin_bottom = int(
            getattr(config, "WEBCAM_SUBTITLE_MARGIN_BOTTOM", 22) or 22
        )
        # Dark veil over frosted video (see-through footer, not solid black)
        self._subtitle_bar_alpha = float(
            getattr(config, "WEBCAM_SUBTITLE_BAR_ALPHA", 0.48) or 0.48
        )
        self._subtitle_blur_px = int(
            getattr(config, "WEBCAM_SUBTITLE_BLUR_PX", 21) or 0
        )
        # Default True: Teams/OBS often mirror vcam → bare putText reads R→L
        self._subtitle_mirror_h = bool(
            getattr(config, "WEBCAM_SUBTITLE_MIRROR", True)
        )

        qsize = max(1, int(getattr(config, "WEBCAM_QUEUE_SIZE", 2) or 2))
        self._q_cap: queue.Queue = queue.Queue(maxsize=qsize)
        self._q_out: queue.Queue = queue.Queue(maxsize=qsize)

        self._threads: list[threading.Thread] = []
        self._status = WebcamStatus(engine=getattr(self.engine, "name", "?"))
        self._status_lock = threading.Lock()
        self._started = False
        self._frames_sent = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """Spawn capture / infer / emit threads (idempotent)."""
        if self._started:
            return True
        deps = check_webcam_deps()
        missing = []
        if not deps["cv2"]:
            missing.append("opencv-python")
        if not deps["pyvirtualcam"]:
            missing.append("pyvirtualcam")
        if missing:
            self._set_error(
                "Deps missing: "
                + ", ".join(missing)
                + " — pip install opencv-python mediapipe pyvirtualcam "
                "(+ start OBS Virtual Camera once on Windows)"
            )
            return False
        if not deps["mediapipe"]:
            self._log(
                "mediapipe missing — face ROI disabled; passthrough full frame. "
                "pip install mediapipe"
            )

        self._stop.clear()
        self._capture_failed.clear()
        self._vcam_ready.clear()
        self._threads = [
            threading.Thread(target=self._capture_loop, name="cam-capture", daemon=True),
            threading.Thread(target=self._infer_loop, name="cam-infer", daemon=True),
            threading.Thread(target=self._emit_loop, name="cam-emit", daemon=True),
        ]
        for t in self._threads:
            t.start()
        self._started = True
        with self._status_lock:
            self._status.running = True
            self._status.enabled = self._enabled.is_set()
            self._status.engine = getattr(self.engine, "name", "?")
            self._status.error = None
        self._log(
            f"Webcam lip-sync started (engine={self.engine.name}, "
            f"enabled={self._enabled.is_set()}). Toggle: [cam]"
        )
        self._log(teams_setup_hint())
        # Background status file for diagnosis when TUI log is hard to copy
        try:
            threading.Thread(
                target=self._status_file_loop, name="cam-status-file", daemon=True
            ).start()
        except Exception:
            pass
        return True

    def stop(self) -> None:
        was_started = self._started
        self._stop.set()
        self._enabled.clear()
        # Unblock queues
        for q in (self._q_cap, self._q_out):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        for t in self._threads:
            try:
                t.join(timeout=1.5)
            except Exception:
                pass
        self._threads.clear()
        try:
            self.engine.close()
        except Exception:
            pass
        try:
            self.roi.close()
        except Exception:
            pass
        self._started = False
        self._vcam_ready.clear()
        with self._status_lock:
            self._status.running = False
            self._status.enabled = False
            self._status.capture_ok = False
        # Only log if threads actually ran — avoid "stopped" on idle exit (looks like failure).
        if was_started:
            self._log("Webcam lip-sync stopped.")

    def enable(self) -> None:
        """Turn stream ON: capture + virtual cam re-open if threads already running."""
        self._capture_failed.clear()
        self._enabled.set()
        with self._status_lock:
            self._status.enabled = True
            self._status.error = None
        self._log(
            "Webcam lip-sync ENABLED — opening physical cam + virtual cam "
            "([cam off] libera ambos)."
        )
        self._log(teams_setup_hint())

    def disable(self) -> None:
        """
        Turn stream OFF and release exclusive camera devices.

        Capture and emit loops close VideoCapture / pyvirtualcam promptly so
        Teams/OBS can reclaim them. Threads keep running (idle wait) so
        ``[cam on]`` is fast — no full ``stop()`` / MediaPipe re-init.
        Does not affect LiveLingo audio, STT, or TTS paths.
        """
        was_on = self._enabled.is_set()
        self._enabled.clear()
        with self._status_lock:
            self._status.enabled = False
            self._status.fps_cap = 0.0
            self._status.fps_out = 0.0
        if not was_on:
            self._log("Webcam already OFF (physical + virtual cam free).")
            return
        # Drop stale frames so re-enable does not flash an old picture.
        self._drain_queues()
        self._log(
            "Webcam lip-sync DISABLED — releasing physical cam + virtual cam "
            "([cam on] reabre)."
        )

    def toggle(self) -> bool:
        """Return new enabled state."""
        if self._enabled.is_set():
            self.disable()
            return False
        self.enable()
        return True

    def is_enabled(self) -> bool:
        return self._enabled.is_set()

    def push_tts_audio(self, audio, sample_rate: int) -> None:
        """Called from pipeline when TTS is enqueued for Cable/Teams."""
        if not self._started:
            return
        # Accept audio even if briefly disabled so lips catch up on re-enable.
        try:
            self.audio.push(audio, sample_rate)
        except Exception:
            pass

    def clear_tts_audio(self) -> None:
        """Drop scheduled TTS (stop/[x]) → mouth closes immediately."""
        try:
            self.audio.clear()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Burn-in subtitles (TARGET text on virtual-cam pixels)
    # ------------------------------------------------------------------ #
    def push_subtitle_text(self, text: str) -> None:
        """
        Replace TARGET burn-in with this string (never append).

        Always overwrites the previous caption so the vcam shows a single
        current translation. Empty text is ignored (keeps last until
        ``clear_subtitle_text`` / ``sub off``).
        """
        t = " ".join((text or "").split())
        if not t:
            return
        with self._subtitle_lock:
            # Explicit replace — never concatenate with previous caption
            self._subtitle_text = t
            self._subtitle_set_mono = time.monotonic()
            self._subtitle_gen = int(getattr(self, "_subtitle_gen", 0) or 0) + 1

    def clear_subtitle_text(self) -> None:
        """Clear stored TARGET text (overlay shows nothing until next push)."""
        with self._subtitle_lock:
            self._subtitle_text = ""
            self._subtitle_set_mono = 0.0

    def is_subtitle_enabled(self) -> bool:
        with self._subtitle_lock:
            return bool(self._subtitle_enabled)

    def set_subtitle_enabled(self, on: bool) -> Tuple[bool, str]:
        """Enable/disable burn-in. Returns (enabled, status message)."""
        on = bool(on)
        with self._subtitle_lock:
            self._subtitle_enabled = on
            preview = (self._subtitle_text or "")[:60]
        # Persist for this process + config (so status/help stay in sync)
        try:
            self.cfg.WEBCAM_SUBTITLE = on
        except Exception:
            pass
        if on:
            extra = f' · last="{preview}…"' if len(preview) >= 60 else (
                f' · last="{preview}"' if preview else " · (aguarde próxima tradução)"
            )
            return True, (
                f"Legenda vcam ON (TARGET burn-in){extra}. "
                f"Fica na tela até [sub off] ou nova tradução. "
                f"Só pixels na OBS Virtual Cam — não é CC do Teams."
            )
        return False, (
            "Legenda vcam OFF — texto sumiu do frame. "
            "[sub on] liga de novo (último TARGET, se houver)."
        )

    def toggle_subtitle(self) -> Tuple[bool, str]:
        """Toggle burn-in ON/OFF."""
        with self._subtitle_lock:
            nxt = not bool(self._subtitle_enabled)
        return self.set_subtitle_enabled(nxt)

    def _active_subtitle_text(self) -> str:
        """
        TARGET text while overlay is ON.

        Stays visible until ``[sub off]`` (or clear) or the next
        ``push_subtitle_text`` replaces it. Optional hold>0 auto-hides only
        if WEBCAM_SUBTITLE_HOLD_S is set deliberately.
        """
        with self._subtitle_lock:
            if not self._subtitle_enabled:
                return ""
            text = self._subtitle_text or ""
            if not text:
                return ""
            hold = float(self._subtitle_hold_s or 0.0)
            # Default 0: never expire. Only expire if hold explicitly > 0.
            if hold > 0 and self._subtitle_set_mono > 0:
                if (time.monotonic() - self._subtitle_set_mono) > hold:
                    return ""
            return text

    def _apply_subtitle_burnin(self, frame_bgr: np.ndarray) -> np.ndarray:
        """Draw TARGET burn-in when enabled; otherwise return frame unchanged."""
        text = self._active_subtitle_text()
        if not text or frame_bgr is None:
            return frame_bgr
        try:
            from livelingo.webcam.subtitle import draw_subtitle_burnin

            return draw_subtitle_burnin(
                frame_bgr,
                text,
                max_lines=self._subtitle_max_lines,
                font_scale=self._subtitle_font_scale,
                margin_bottom=self._subtitle_margin_bottom,
                bar_alpha=self._subtitle_bar_alpha,
                mirror_h=bool(self._subtitle_mirror_h),
                blur_px=int(self._subtitle_blur_px or 0),
            )
        except Exception:
            return frame_bgr

    def snap_closed_mouth(
        self,
        *,
        preview: bool = True,
        timeout_s: float = 45.0,
    ) -> Tuple[bool, str]:
        """
        Capture closed-mouth photo template with optional live preview.

        Opens an OpenCV window (if display available):
          - Look at camera with mouth closed
          - Press **SPACE** or **ENTER** to save
          - **ESC** cancels
          - Auto-saves after ~3s of stable face if you do nothing

        Falls back to last pipeline frame if preview cannot open.
        """
        img_p = (getattr(self.cfg, "WEBCAM_CLOSED_MOUTH_IMAGE", "") or "").strip()
        lm_p = (getattr(self.cfg, "WEBCAM_CLOSED_MOUTH_LANDMARKS", "") or "").strip()

        frame = None
        roi = None
        used_preview = False

        if preview:
            try:
                frame, roi, used_preview = self._snap_with_preview(timeout_s=timeout_s)
            except Exception as exc:
                self._log(f"snap preview failed: {exc}")
                frame, roi, used_preview = None, None, False

        if frame is None:
            with self._last_frame_lock:
                frame = (
                    None if self._last_frame_bgr is None else self._last_frame_bgr.copy()
                )
            if frame is None:
                return (
                    False,
                    "nenhum frame — [cam on] e aguarde vídeo, ou rode de novo "
                    "com display (preview OpenCV). No WSL sem GUI o preview falha.",
                )
            roi = self.roi.process(frame)

        # Prefer MediaPipe landmarks; allow heuristic synthetic landmarks
        mp_ok = bool(
            roi is not None
            and roi.landmarks_xy is not None
            and roi.landmarks_xy.shape[0] >= 100
        )
        if not self.roi.available:
            self._log(
                "MediaPipe indisponível — template usará caixa da boca. "
                "pip install mediapipe  (mesmo Python do LiveLingo)"
            )
        elif not mp_ok:
            self._log(
                "Face Mesh não travou landmarks — salvando com caixa heurística. "
                "Melhor: luz frontal, rosto de frente, refazer snap."
            )

        ok, msg = save_template_from_frame(
            frame,
            roi,
            image_path=img_p or None,
            landmarks_path=lm_p or None,
            allow_heuristic=True,
        )
        if ok:
            tpl = load_template(img_p or None, lm_p or None)
            self._template_store.set(tpl)
            if used_preview:
                msg = msg + " [preview]"
            self._log(msg)
        return ok, msg

    def _snap_with_preview(
        self, timeout_s: float = 45.0
    ) -> Tuple[Optional[np.ndarray], Optional[Any], bool]:
        """
        Interactive OpenCV window for snap. Returns (frame, roi, used_preview).
        """
        import cv2

        idx = int(getattr(self.cfg, "WEBCAM_DEVICE_INDEX", 0) or 0)
        # Prefer sharing last frame if capture thread already holds the device
        # (Windows exclusive cam). Still open a second handle when possible.
        cap = self._open_capture(idx)
        own_cap = cap is not None
        if not own_cap:
            # Device busy by cam-capture — drive preview from last frames
            self._log(
                "Câmera física ocupada pelo pipeline — preview usa frames do stream. "
                "Garanta [cam on] e que o LED da cam esteja aceso."
            )

        win = "LiveLingo — boca FECHADA (SPACE=salvar  ESC=cancelar)"
        try:
            cv2.namedWindow(win, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win, 640, 480)
        except Exception as exc:
            if own_cap and cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            raise RuntimeError(f"sem display GUI para preview: {exc}") from exc

        deadline = time.monotonic() + max(5.0, float(timeout_s))
        stable_face_t = 0.0
        best = None  # (frame, roi)
        saved = None

        try:
            while time.monotonic() < deadline:
                frame = None
                if own_cap and cap is not None:
                    ok, frame = cap.read()
                    if not ok:
                        frame = None
                if frame is None:
                    with self._last_frame_lock:
                        frame = (
                            None
                            if self._last_frame_bgr is None
                            else self._last_frame_bgr.copy()
                        )
                if frame is None:
                    time.sleep(0.05)
                    # blank wait screen
                    blank = np.zeros((360, 480, 3), dtype=np.uint8)
                    cv2.putText(
                        blank,
                        "Aguardando camera...",
                        (40, 180),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (200, 200, 200),
                        2,
                    )
                    cv2.imshow(win, blank)
                    if (cv2.waitKey(30) & 0xFF) in (27,):
                        return None, None, True
                    continue

                roi = self.roi.process(frame)
                vis = frame.copy()
                h, w = vis.shape[:2]
                mp_ok = (
                    roi.landmarks_xy is not None and roi.landmarks_xy.shape[0] >= 100
                )
                # Full-face freeze region (same geom as F10 align_and_blend)
                color = (0, 220, 0) if mp_ok else (0, 180, 255)
                scale = float(
                    getattr(self.cfg, "WEBCAM_TEMPLATE_REGION_SCALE", 1.15) or 1.15
                )
                geom = compute_freeze_plate_geom(
                    h,
                    w,
                    roi.landmarks_xy if mp_ok else None,
                    int(roi.mouth_cx),
                    int(roi.mouth_cy),
                    int(roi.mouth_w or 40),
                    int(roi.mouth_h or 12),
                    region_scale=scale,
                )
                fcx, fcy = geom.center
                ax, ay = geom.axes
                if geom.contour is not None and len(geom.contour) >= 6:
                    try:
                        hull = cv2.convexHull(geom.contour)
                        cv2.polylines(
                            vis, [hull], True, color, 2, cv2.LINE_AA
                        )
                    except Exception:
                        cv2.ellipse(
                            vis,
                            (fcx, fcy),
                            (ax, ay),
                            0,
                            0,
                            360,
                            color,
                            2,
                            cv2.LINE_AA,
                        )
                else:
                    cv2.ellipse(
                        vis,
                        (fcx, fcy),
                        (ax, ay),
                        0,
                        0,
                        360,
                        color,
                        2,
                        cv2.LINE_AA,
                    )
                cv2.circle(vis, (roi.mouth_cx, roi.mouth_cy), 4, color, -1)
                cv2.circle(vis, (fcx, fcy), 3, (0, 255, 255), -1)
                lines = [
                    "Feche a boca | SPACE/ENTER=salvar | ESC=cancelar",
                    f"Area CONGELADA = ROSTO INTEIRO (F10) | MP={'OK' if mp_ok else 'nao'}",
                    "Auto-save ~3s com face estavel...",
                ]
                if not self.roi.available:
                    lines.append(
                        f"mediapipe missing: {self.roi.error or 'install mediapipe'}"
                    )
                y = 28
                for line in lines:
                    cv2.putText(
                        vis,
                        line,
                        (12, y),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (40, 40, 40),
                        3,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        vis,
                        line,
                        (12, y),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (0, 255, 200),
                        1,
                        cv2.LINE_AA,
                    )
                    y += 26

                cv2.imshow(win, vis)
                key = cv2.waitKey(30) & 0xFF
                if key in (27,):  # ESC
                    return None, None, True
                if key in (13, 32):  # Enter / Space
                    saved = (frame.copy(), roi)
                    break

                if roi.face_ok:
                    if best is None or mp_ok:
                        best = (frame.copy(), roi)
                    if mp_ok:
                        if stable_face_t <= 0:
                            stable_face_t = time.monotonic()
                        elif time.monotonic() - stable_face_t >= 3.0:
                            saved = (frame.copy(), roi)
                            break
                    else:
                        # heuristic face: need longer stable + user can still SPACE
                        if stable_face_t <= 0:
                            stable_face_t = time.monotonic()
                        elif time.monotonic() - stable_face_t >= 5.0 and best:
                            saved = best
                            break
                else:
                    stable_face_t = 0.0

            if saved is None and best is not None:
                saved = best
            if saved is None:
                return None, None, True
            return saved[0], saved[1], True
        finally:
            try:
                cv2.destroyWindow(win)
            except Exception:
                pass
            if own_cap and cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    def has_closed_template(self) -> bool:
        return self._template_store.loaded

    def notify_vad_speech(self, is_speaking: bool) -> None:
        """
        Called from capture VAD (on_listening): mic is hearing speech now.

        Used only when not in F10 manual mode.
        """
        with self._vad_lock:
            if is_speaking:
                self._vad_speech.set()
                self._vad_end_mono = 0.0
            else:
                self._vad_speech.clear()
                self._vad_end_mono = time.monotonic()

    def is_mic_hearing_speech(self) -> bool:
        """True while VAD says speech, plus short hangover (anti-flicker)."""
        with self._vad_lock:
            if self._vad_speech.is_set():
                return True
            hang = self._vad_hangover_s
            if hang > 0 and self._vad_end_mono > 0:
                return (time.monotonic() - self._vad_end_mono) < hang
            return False

    def toggle_closed_mouth_manual(self) -> Tuple[bool, str]:
        """
        F10: toggle closed-mouth face plate ON/OFF (manual).

        Enters manual mode (disables VAD auto for this session until
        ``set_closed_mouth_auto()``). Returns (is_on, status_message).
        """
        # Always reload if empty; clear stuck TTS so playing=False doesn't block show
        try:
            if not self._template_store.loaded:
                self._template_store.load_from_config(self.cfg)
        except Exception:
            pass
        try:
            self.clear_tts_audio()
        except Exception:
            pass
        with self._vad_lock:
            self._closed_manual_mode = True
            self._closed_manual_on = not self._closed_manual_on
            on = self._closed_manual_on
            self._closed_gen = int(self._closed_gen) + 1
            gen = self._closed_gen
            self._closed_apply_ok = 0
            self._closed_apply_fail = 0
        if on:
            if not self._template_store.loaded:
                return True, (
                    f"Boca calada ON gen={gen} — sem foto: [cam snap closed]"
                )
            if not self.is_enabled() or not self._started:
                return True, (
                    f"Boca calada ON gen={gen} — ative [cam on] + Teams=OBS Virtual Cam"
                )
            snap = self.snapshot()
            if not snap.get("vcam_ready"):
                return True, (
                    f"Boca calada ON gen={gen} — vcam=false; [cam status]"
                )
            return True, (
                f"Boca calada ON gen={gen} — foto no rosto | "
                f"tpl={self._template_store.path() or 'ok'} | F10 tira"
            )
        return False, (
            f"Boca calada OFF gen={gen} — vídeo ao vivo | F10 mostra de novo"
        )

    def set_closed_mouth_manual(self, on: bool) -> Tuple[bool, str]:
        """Force manual closed plate on/off (commands)."""
        with self._vad_lock:
            self._closed_manual_mode = True
            self._closed_manual_on = bool(on)
            on = self._closed_manual_on
        if on:
            return True, "Boca calada ON (manual)"
        return False, "Boca calada OFF (manual)"

    def set_closed_mouth_auto(self) -> str:
        """Exit F10/F11 manual freeze → live video (no VAD auto plate anymore)."""
        with self._vad_lock:
            self._closed_manual_mode = False
            self._closed_manual_on = False
            self._closed_full_frame_on = False
            self._closed_gen = int(self._closed_gen) + 1
        return (
            "Vídeo ao vivo — closed só com F10 (rosto) / F11 (tela inteira). "
            "Auto no mic desativado."
        )

    def toggle_closed_full_frame(self) -> Tuple[bool, str]:
        """
        F11: toggle full-frame closed photo (covers entire virtual cam).

        Unlike F10 (face plate on live video), this hides 100% of the live
        feed and shows only the closed-mouth template scaled to fill the frame.
        """
        try:
            if not self._template_store.loaded:
                self._template_store.load_from_config(self.cfg)
        except Exception:
            pass
        try:
            self.clear_tts_audio()
        except Exception:
            pass
        with self._vad_lock:
            self._closed_full_frame_on = not self._closed_full_frame_on
            on = bool(self._closed_full_frame_on)
            self._closed_full_gen = int(self._closed_full_gen) + 1
            gen = self._closed_full_gen
            # Full-frame freeze supersedes face plate while ON
            if on:
                self._closed_manual_mode = True
                self._closed_manual_on = False
        if on:
            if not self._template_store.loaded:
                return True, (
                    f"Tela closed ON gen={gen} — sem foto: [cam snap closed]"
                )
            if not self.is_enabled() or not self._started:
                return True, (
                    f"Tela closed ON gen={gen} — ative [cam on] + Teams=OBS Virtual Cam"
                )
            snap = self.snapshot()
            if not snap.get("vcam_ready"):
                return True, (
                    f"Tela closed ON gen={gen} — vcam=false; [cam status]"
                )
            return True, (
                f"Tela closed ON gen={gen} — foto INTEIRA no vídeo "
                f"(sem live) | tpl={self._template_store.path() or 'ok'} | F11 tira"
            )
        return False, (
            f"Tela closed OFF gen={gen} — vídeo ao vivo | F11 congela de novo"
        )

    def set_closed_full_frame(self, on: bool) -> Tuple[bool, str]:
        """Force full-frame closed freeze on/off."""
        with self._vad_lock:
            self._closed_full_frame_on = bool(on)
            on = bool(self._closed_full_frame_on)
            if on:
                self._closed_manual_mode = True
                self._closed_manual_on = False
        if on:
            return True, "Tela closed ON (manual full-frame)"
        return False, "Tela closed OFF (manual full-frame)"

    def closed_mouth_state(self) -> dict:
        with self._vad_lock:
            return {
                "manual_mode": self._closed_manual_mode,
                "manual_on": self._closed_manual_on,
                "full_frame_on": self._closed_full_frame_on,
                "auto": self._closed_auto,
                "vad_speech": self.is_mic_hearing_speech(),
            }

    def should_show_closed_template(self, *, playing: bool) -> bool:
        """
        Auto closed-photo on mic speech is **disabled**.

        Closed image only via F10 (face plate) / F11 (full-frame) — see infer loop
        ``manual_on`` / ``full_frame_on``. Always returns False (no VAD auto).
        """
        del playing  # no longer used for auto plate
        return False

    # Back-compat alias used by older wiring
    def set_listening_probe(self, fn: Optional[Callable[[], bool]]) -> None:
        """Deprecated: use notify_vad_speech from on_listening instead."""
        self._listening_probe = fn  # type: ignore[attr-defined]

    def snapshot(self) -> dict:
        with self._status_lock:
            s = self._status
            try:
                rms = float(self.audio.rms(0.12))
            except Exception:
                rms = 0.0
            try:
                playing = bool(self.audio.is_playing())
            except Exception:
                playing = False
            return {
                "running": s.running,
                "enabled": s.enabled,
                "fps_out": round(s.fps_out, 1),
                "fps_cap": round(s.fps_cap, 1),
                "face_ok": s.face_ok,
                "engine": s.engine,
                "error": s.error,
                "width": s.width,
                "height": s.height,
                "backend": s.backend,
                "roi_ok": self.roi.available,
                "audio_rms": round(rms, 4),
                "audio_playing": playing,
                "frames_sent": s.frames_sent,
                "capture_ok": s.capture_ok,
                "vcam_ready": self._vcam_ready.is_set(),
                "emit_phase": s.emit_phase,
                "sync_marker": self._sync_marker,
                "force_closed_idle": self._force_closed_idle,
                "template_ok": self._template_store.loaded,
                "template_path": self._template_store.path() or "",
                "template_flip_h": self._template_flip_h,
                "vad_speech": self.is_mic_hearing_speech(),
                "closed_manual": self._closed_manual_mode,
                "closed_manual_on": self._closed_manual_on,
                "closed_full_frame_on": self._closed_full_frame_on,
                "closed_full_gen": self._closed_full_gen,
                "closed_auto": self._closed_auto,
                "closed_gen": self._closed_gen,
                "closed_apply_ok": self._closed_apply_ok,
                "closed_apply_fail": self._closed_apply_fail,
                "subtitle": self.is_subtitle_enabled(),
                "subtitle_text": (self._active_subtitle_text() or "")[:80],
            }

    def _set_emit_phase(self, phase: str) -> None:
        with self._status_lock:
            self._status.emit_phase = str(phase or "")

    def _status_file_loop(self) -> None:
        """Write .cache/webcam_status.txt every 1s while running (easy to type/open)."""
        import json
        from pathlib import Path

        path = Path(".cache") / "webcam_status.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        while not self._stop.is_set() and self._started:
            try:
                snap = self.snapshot()
                lines = [
                    "LiveLingo webcam status (auto-refresh while cam running)",
                    f"enabled={snap.get('enabled')} running={snap.get('running')}",
                    f"vcam_ready={snap.get('vcam_ready')} capture_ok={snap.get('capture_ok')}",
                    f"fps_out={snap.get('fps_out')} fps_cap={snap.get('fps_cap')} "
                    f"sent={snap.get('frames_sent')} face={snap.get('face_ok')}",
                    f"tts_playing={snap.get('audio_playing')} rms={snap.get('audio_rms')} "
                    f"marker={snap.get('sync_marker')}",
                    f"closed_manual={snap.get('closed_manual')} "
                    f"closed_on={snap.get('closed_manual_on')} "
                    f"gen={snap.get('closed_gen')} "
                    f"apply_ok={snap.get('closed_apply_ok')} "
                    f"apply_fail={snap.get('closed_apply_fail')} "
                    f"tpl={snap.get('template_ok')}",
                    f"size={snap.get('width')}x{snap.get('height')} "
                    f"backend={snap.get('backend')}",
                    f"error={snap.get('error') or '—'}",
                    "",
                    "Teams camera MUST be: OBS Virtual Camera (not the laptop webcam).",
                    "Teams mic MUST be: CABLE Output. LiveLingo [s] ON for speech.",
                    "Mouth morph + SYNC marker only while TTS audio is scheduled.",
                    "",
                    json.dumps(snap, ensure_ascii=False),
                ]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except Exception:
                pass
            self._stop.wait(1.0)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _set_error(self, msg: str) -> None:
        self._log(msg)
        with self._status_lock:
            self._status.error = msg

    def _clear_error_if(self, prefix: str) -> None:
        with self._status_lock:
            err = self._status.error or ""
            if err.startswith(prefix) or prefix in err:
                self._status.error = None

    def _put_drop_old(self, q: queue.Queue, item: Any) -> None:
        """Non-blocking put; if full, drop oldest then insert (latency bound)."""
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass

    def _drain_queues(self) -> None:
        """Empty frame queues (best-effort; safe while threads idle on disable)."""
        for q in (self._q_cap, self._q_out):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break

    def _open_capture(self, idx: int):
        """Open physical camera; on Windows prefer DirectShow then Media Foundation."""
        import cv2

        attempts: List[Tuple[str, Any]] = []
        if sys.platform == "win32":
            # CAP_DSHOW is far more reliable on Windows than the default backend.
            attempts.append(("dshow", getattr(cv2, "CAP_DSHOW", 700)))
            attempts.append(("msmf", getattr(cv2, "CAP_MSMF", 1400)))
        attempts.append(("any", getattr(cv2, "CAP_ANY", 0)))

        last_err = ""
        for name, backend in attempts:
            try:
                cap = cv2.VideoCapture(idx, backend)
            except Exception as exc:
                last_err = f"{name}: {exc}"
                continue
            if cap is not None and cap.isOpened():
                self._log(
                    f"Webcam física ABERTA index={idx} backend={name} "
                    f"(LED/indicador do SO deve acender agora)"
                )
                return cap
            try:
                cap.release()
            except Exception:
                pass
            last_err = f"{name}: not opened"
        self._set_error(
            f"Cannot open webcam index {idx} ({last_err}). "
            "Close Teams/Zoom camera, try WEBCAM_DEVICE_INDEX=1, or free the device."
        )
        return None

    @staticmethod
    def _even(n: int, default: int) -> int:
        n = int(n or 0)
        if n <= 0:
            n = default
        if n % 2:
            n += 1
        return max(2, n)

    def _vcam_size(self) -> Tuple[int, int]:
        """Resolution for pyvirtualcam (even dims; OBS-friendly defaults)."""
        w = self._even(getattr(self.cfg, "WEBCAM_VCAM_WIDTH", 1280), 1280)
        h = self._even(getattr(self.cfg, "WEBCAM_VCAM_HEIGHT", 720), 720)
        return w, h

    def _try_open_one_vcam(self, kwargs: dict, timeout_s: float = 6.0):
        """
        Open ``pyvirtualcam.Camera`` on the *current* thread when possible.

        Nested open-threads are dangerous on Windows: a timed-out worker can
        still finish later and hold OBS Virtual Camera exclusively, so every
        later attempt fails with ``virtual camera output could not be started``.

        Timeout (``WEBCAM_VCAM_OPEN_TIMEOUT_S`` > 0) still uses a worker, but a
        hang aborts the whole open sequence (no more size/backend retries while
        a zombie may own the device).
        """
        import pyvirtualcam

        timeout_s = float(timeout_s or 0.0)
        # Direct open is reliable when the driver is registered (normal case).
        if timeout_s <= 0:
            return pyvirtualcam.Camera(**kwargs)

        box: dict = {"cam": None, "exc": None}

        def _worker():
            try:
                box["cam"] = pyvirtualcam.Camera(**kwargs)
            except Exception as exc:
                box["exc"] = exc

        t = threading.Thread(target=_worker, name="vcam-open", daemon=True)
        t.start()
        t.join(timeout=max(1.0, timeout_s))
        if t.is_alive():
            # Cannot kill native code. Do NOT start more open attempts.
            raise TimeoutError(
                f"pyvirtualcam.Camera hung >{timeout_s:.0f}s "
                f"(backend={kwargs.get('backend') or 'auto'} "
                f"{kwargs.get('width')}x{kwargs.get('height')}). "
                "Install OBS Studio, Start Virtual Camera once (register driver), "
                "then Stop it. Restart LiveLingo if a previous open hung."
            )
        if box["exc"] is not None:
            raise box["exc"]
        if box["cam"] is None:
            raise RuntimeError("pyvirtualcam.Camera returned None")
        return box["cam"]

    def _open_vcam(self, w: int, h: int, fps: float):
        """
        Try a *small* set of size/backend/format combos.

        OBS Virtual Camera allows only one producer. Rapid multi-format storms
        + abandoned timeout threads used to leave the device locked.
        """
        import pyvirtualcam  # noqa: F401
        from pyvirtualcam import PixelFormat

        device = (getattr(self.cfg, "WEBCAM_VCAM_DEVICE", "") or "").strip() or None
        configured = (getattr(self.cfg, "WEBCAM_VCAM_BACKEND", "") or "").strip() or None
        # Default 0 = open on emit thread (no nested worker). Set >0 only if
        # opens hang without the OBS driver registered.
        timeout_s = float(getattr(self.cfg, "WEBCAM_VCAM_OPEN_TIMEOUT_S", 0.0) or 0.0)

        backends: List[Optional[str]] = []
        if configured:
            if configured == "obs" and sys.platform not in ("win32", "darwin"):
                self._log(
                    f"Ignoring WEBCAM_VCAM_BACKEND=obs on {sys.platform} "
                    "(use v4l2loopback on Linux, or run LiveLingo on Windows for Teams)."
                )
            else:
                backends.append(configured)
        if sys.platform == "win32":
            for b in ("obs", "unitycapture"):
                if b not in backends:
                    backends.append(b)
        elif sys.platform.startswith("linux"):
            if "v4l2loopback" not in backends:
                backends.append("v4l2loopback")
        if None not in backends:
            backends.append(None)  # auto last

        # Prefer native capture size / configured vcam size, then 640x480 fallback.
        sizes: List[Tuple[int, int]] = []
        primary = (self._even(w, 1280), self._even(h, 720))
        for pair in (primary, (640, 480), (1280, 720)):
            if pair not in sizes:
                sizes.append(pair)

        # RGB is pyvirtualcam default; try BGR second (OpenCV native).
        formats = [PixelFormat.RGB, PixelFormat.BGR]
        errors: List[str] = []
        exclusive_hits = 0

        for be in backends:
            for sw, sh in sizes:
                for fmt in formats:
                    if self._stop.is_set():
                        raise RuntimeError("stopped during vcam open")
                    fmt_name = getattr(fmt, "name", str(fmt))
                    label = f"{be or 'auto'}/{fmt_name}/{sw}x{sh}"
                    self._set_emit_phase(f"opening_vcam:{label}")
                    self._log(f"vcam try {label}…")
                    kwargs: dict = dict(
                        width=int(sw),
                        height=int(sh),
                        fps=float(fps),
                        fmt=fmt,
                    )
                    if be:
                        kwargs["backend"] = be
                    if device:
                        kwargs["device"] = device

                    # Brief re-try on exclusive-lock failures (OBS just stopped, etc.)
                    last_exc: Optional[BaseException] = None
                    for attempt in range(3):
                        if self._stop.is_set():
                            raise RuntimeError("stopped during vcam open")
                        try:
                            cam = self._try_open_one_vcam(
                                kwargs, timeout_s=timeout_s
                            )
                            be_name = str(
                                getattr(cam, "backend", None) or be or "auto"
                            )
                            dev_name = getattr(cam, "device", device)
                            self._log(
                                f"Virtual cam OPEN: {sw}x{sh} @ {fps:g} FPS "
                                f"backend={be_name} fmt={fmt_name} "
                                f"device={dev_name!r}"
                            )
                            self._log(
                                ">>> No Teams: escolha a câmera "
                                f"{dev_name!r} (OBS Virtual Camera) — "
                                "NÃO a webcam física."
                            )
                            return cam, be_name, fmt, sw, sh
                        except TimeoutError:
                            # Hang: do not try more combos (zombie may hold device).
                            raise
                        except Exception as exc:
                            last_exc = exc
                            msg = str(exc).lower()
                            if "could not be started" in msg or "already" in msg:
                                exclusive_hits += 1
                                if attempt < 2:
                                    time.sleep(0.4 * (attempt + 1))
                                    continue
                            break

                    errors.append(f"{label}: {last_exc}")
                    self._log(f"vcam try fail {label}: {last_exc}")

        detail = " | ".join(errors[:8]) + (" …" if len(errors) > 8 else "")
        if exclusive_hits:
            detail = f"{obs_virtual_cam_conflict_hint()} Detail: {detail}"
        raise RuntimeError(detail)

    def _placeholder_frame(self, h: int, w: int) -> np.ndarray:
        """Dark frame so Teams shows *something* while waiting for capture."""
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:] = (32, 24, 16)  # dark BGR
        try:
            import cv2

            msg = "LiveLingo cam"
            cv2.putText(
                frame,
                msg,
                (max(8, w // 10), h // 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                max(0.5, min(w, h) / 600.0),
                (200, 200, 200),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                "waiting physical camera...",
                (max(8, w // 10), h // 2 + 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                max(0.4, min(w, h) / 700.0),
                (160, 160, 160),
                1,
                cv2.LINE_AA,
            )
        except Exception:
            pass
        return frame

    # ------------------------------------------------------------------ #
    # Thread 1 — capture
    # ------------------------------------------------------------------ #
    def _capture_loop(self) -> None:
        """
        Open physical cam only while enabled; release on ``[cam off]``.

        Mirrors ``_emit_loop``: exclusive device is not held idle forever.
        Outer loop re-opens after disable→enable (and after transient open fails).
        """
        import cv2

        idx = int(getattr(self.cfg, "WEBCAM_DEVICE_INDEX", 0) or 0)
        width = int(getattr(self.cfg, "WEBCAM_WIDTH", 0) or 0)
        height = int(getattr(self.cfg, "WEBCAM_HEIGHT", 0) or 0)
        target_fps = float(getattr(self.cfg, "WEBCAM_FPS", 30) or 30)
        frame_interval = 1.0 / max(1.0, target_fps)

        while not self._stop.is_set():
            # Wait until enabled so we don't grab exclusive camera while idle.
            while not self._stop.is_set() and not self._enabled.is_set():
                time.sleep(0.05)
            if self._stop.is_set():
                break

            self._capture_failed.clear()
            cap = self._open_capture(idx)
            if cap is None:
                self._capture_failed.set()
                with self._status_lock:
                    self._status.capture_ok = False
                    self._status.fps_cap = 0.0
                # Retry while still ON (device may free up); bail if user turns off.
                for _ in range(25):
                    if self._stop.is_set() or not self._enabled.is_set():
                        break
                    time.sleep(0.1)
                continue

            if width > 0:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            if height > 0:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, target_fps)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
            with self._status_lock:
                self._status.width = actual_w
                self._status.height = actual_h
                self._status.capture_ok = True
                self._status.fps_cap = 0.0
            self._clear_error_if("Cannot open webcam")
            self._clear_error_if("Webcam read failed")
            self._log(
                f"Capture LIVE index={idx} {actual_w}x{actual_h} "
                f"([cam off] libera a webcam física)."
            )

            n = 0
            t0 = time.perf_counter()
            fail_reads = 0
            try:
                while not self._stop.is_set() and self._enabled.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        fail_reads += 1
                        if fail_reads == 30:
                            self._set_error(
                                f"Webcam read failed (index={idx}). "
                                "Device busy or disconnected?"
                            )
                        time.sleep(0.02)
                        continue
                    fail_reads = 0
                    self._put_drop_old(
                        self._q_cap,
                        _FramePacket(
                            frame_bgr=frame, t_capture=time.perf_counter()
                        ),
                    )
                    n += 1
                    if n % 30 == 0:
                        dt = time.perf_counter() - t0
                        if dt > 0:
                            with self._status_lock:
                                self._status.fps_cap = n / dt
                        n = 0
                        t0 = time.perf_counter()
                    time.sleep(max(0.0, frame_interval * 0.15))
            finally:
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                with self._status_lock:
                    self._status.capture_ok = False
                    self._status.fps_cap = 0.0
                if not self._stop.is_set():
                    self._log(
                        "Capture released — webcam física livre "
                        "([cam on] reabre)."
                    )
                # Brief settle so Windows can hand the device to another app.
                time.sleep(0.15)

    # ------------------------------------------------------------------ #
    # Thread 2 — ROI + inference
    # ------------------------------------------------------------------ #
    def _infer_loop(self) -> None:
        audio_win = float(getattr(self.cfg, "WEBCAM_AUDIO_WINDOW_S", 0.35) or 0.35)
        while not self._stop.is_set():
            if not self._enabled.is_set():
                time.sleep(0.05)
                continue
            try:
                pkt = self._q_cap.get(timeout=0.1)
            except queue.Empty:
                continue
            if pkt is None:
                break
            frame = pkt.frame_bgr
            try:
                with self._last_frame_lock:
                    self._last_frame_bgr = frame
                roi = self.roi.process(frame)
                # TTS schedule → Teams only (not mic capture).
                playing = bool(self.audio.is_playing())
                open_amt = 0.0
                if playing:
                    try:
                        open_amt = float(
                            self.audio.open_amount(
                                min(0.12, audio_win), self._amp_sensitivity
                            )
                        )
                    except Exception:
                        open_amt = 0.0

                tpl = self._template_store.get()
                # F10 / F11 manual flags
                with self._vad_lock:
                    full_frame_on = bool(self._closed_full_frame_on)
                    manual_on = bool(
                        self._closed_manual_mode and self._closed_manual_on
                    )

                # F11: entire virtual-cam frame = closed photo (hides live video)
                if full_frame_on and tpl is not None and tpl.ok:
                    try:
                        out = cover_frame_with_closed_image(
                            frame,
                            tpl,
                            flip_h=self._template_flip_h,
                        )
                        self._closed_apply_ok += 1
                    except Exception as exc:
                        self._closed_apply_fail += 1
                        self._set_error(f"closed-full-frame: {exc}")
                        out = frame
                elif manual_on and tpl is not None and tpl.ok:
                    use_tpl = True  # F10 ON always paints (even if TTS ring stale)
                    roi_use = roi
                    if not roi.face_ok:
                        try:
                            roi_use = self.roi._heuristic_mouth_roi(frame)
                        except Exception:
                            roi_use = roi
                    try:
                        out = align_and_blend(
                            frame,
                            roi_use,
                            tpl,
                            feather_px=int(
                                getattr(self.cfg, "WEBCAM_TEMPLATE_FEATHER_PX", 24)
                                or 24
                            ),
                            region_scale=float(
                                getattr(self.cfg, "WEBCAM_TEMPLATE_REGION_SCALE", 1.15)
                                or 1.15
                            ),
                            flip_h=self._template_flip_h,
                        )
                        self._closed_apply_ok += 1
                    except Exception as exc:
                        self._closed_apply_fail += 1
                        self._set_error(f"closed-template blend: {exc}")
                        out = frame
                # VAD auto closed-plate removed — only F10 (above) / F11 (above).
                elif playing:
                    if roi.face_ok and open_amt > 0.05:
                        amt = max(open_amt, 0.2)
                        out = open_from_closed_template(frame, roi, amt)
                    else:
                        out = frame
                else:
                    # F10/F11 OFF / silence: 100% live
                    out = frame

                frozen = bool(full_frame_on or manual_on)
                if (
                    self._sync_marker
                    and roi.face_ok
                    and (frozen or playing)
                    and not full_frame_on
                ):
                    out = FaceMouthROI.draw_sync_marker(
                        out, roi, open_amt=open_amt, active=playing
                    )
                with self._status_lock:
                    self._status.face_ok = bool(roi.face_ok)
                    self._status.audio_rms = float(open_amt) if playing else 0.0
                self._put_drop_old(
                    self._q_out,
                    _FramePacket(frame_bgr=out, t_capture=pkt.t_capture),
                )
            except Exception as exc:
                self._set_error(f"infer: {exc}")
                # Fail open: emit raw frame
                self._put_drop_old(
                    self._q_out,
                    _FramePacket(frame_bgr=frame, t_capture=pkt.t_capture),
                )

    # ------------------------------------------------------------------ #
    # Thread 3 — virtual camera emit
    # ------------------------------------------------------------------ #
    def _emit_loop(self) -> None:
        """
        Open virtual cam when enabled, push frames, **retry** on open failure.

        Waiting for physical capture first hid failures under TUI and left
        Teams with no device. A permanent ``return`` after one failed open also
        left ``vcam=False`` forever until full app restart — now we re-open
        while enabled (e.g. after user stops OBS Virtual Camera).
        """
        self._set_emit_phase("emit_start")
        try:
            import pyvirtualcam  # noqa: F401
            from pyvirtualcam import PixelFormat
        except Exception as exc:
            self._set_emit_phase("failed")
            self._set_error(
                f"pyvirtualcam not importable: {exc}. "
                "Run: pip install pyvirtualcam  (same venv as LiveLingo)"
            )
            return

        target_fps = float(getattr(self.cfg, "WEBCAM_FPS", 30) or 30)
        debug_preview = bool(getattr(self.cfg, "WEBCAM_DEBUG_PREVIEW", False))
        want_w, want_h = self._vcam_size()

        while not self._stop.is_set():
            # Wait until enabled so we don't hold the virtual cam while idle.
            self._set_emit_phase("wait_enable")
            self._vcam_ready.clear()
            while not self._stop.is_set() and not self._enabled.is_set():
                time.sleep(0.05)
            if self._stop.is_set():
                break

            self._log(
                f"Opening virtual cam (target {want_w}x{want_h} @ "
                f"{target_fps:g} FPS)…"
            )
            self._set_emit_phase("opening_vcam")
            cam = None
            try:
                cam, be_name, pixel_fmt, w, h = self._open_vcam(
                    want_w, want_h, target_fps
                )
            except Exception as exc:
                self._set_emit_phase("failed")
                msg = str(exc)
                hint = (
                    "Virtual cam open failed. "
                    "cap_ok=true means physical cam is fine — only the "
                    "*virtual* device failed. "
                )
                low = msg.lower()
                if (
                    "could not be started" in low
                    or "obs" in low
                    or "exclusive" in low
                ):
                    hint += obs_virtual_cam_conflict_hint() + " "
                if "unitycapture" in low or "no camera registered" in low:
                    hint += "Unity Capture not installed — use OBS instead. "
                # Avoid duplicating a long Detail: already in RuntimeError body.
                if "Detail:" not in msg:
                    hint += f"Detail: {msg[:400]} "
                else:
                    hint += msg[:700] + " "
                hint += "| docs/webcam-lipsync.md"
                self._set_error(hint)
                self._log(
                    "vcam open failed — will retry in ~2.5s while [cam] is ON. "
                    "Stop Virtual Camera inside OBS if it is running."
                )
                # Retry while still enabled (user may fix OBS mid-session).
                for _ in range(25):
                    if self._stop.is_set() or not self._enabled.is_set():
                        break
                    time.sleep(0.1)
                continue

            need_rgb = pixel_fmt == PixelFormat.RGB
            with self._status_lock:
                self._status.backend = be_name
                self._status.width = w
                self._status.height = h
                self._status.error = None
            self._vcam_ready.set()
            self._set_emit_phase("live")
            self._log(teams_setup_hint())

            last = self._placeholder_frame(h, w)
            n = 0
            t0 = time.perf_counter()
            logged_live = False
            reopen = False

            def _to_vcam(frame_bgr: np.ndarray) -> np.ndarray:
                import cv2

                if frame_bgr is None or frame_bgr.size == 0:
                    return last
                out = frame_bgr
                if out.shape[0] != h or out.shape[1] != w:
                    out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
                if need_rgb:
                    out = cv2.cvtColor(out, cv2.COLOR_BGR2RGB)
                return out

            try:
                cam.send(_to_vcam(last) if need_rgb else last)
                cam.sleep_until_next_frame()
                self._frames_sent = 1
                with self._status_lock:
                    self._status.frames_sent = self._frames_sent

                while not self._stop.is_set():
                    if not self._enabled.is_set():
                        # Release vcam when disabled so OBS/other apps can use it.
                        # (Frozen last-frame hold kept exclusive lock forever.)
                        self._log(
                            "Webcam disabled — closing virtual cam "
                            "(re-opens on [cam on])."
                        )
                        break
                    try:
                        pkt = self._q_out.get(timeout=1.0 / max(1.0, target_fps))
                        if pkt is None:
                            break
                        last = pkt.frame_bgr
                        if not logged_live:
                            logged_live = True
                            self._log(
                                f"Virtual cam LIVE — frames flowing "
                                f"({last.shape[1]}x{last.shape[0]} → {w}x{h}). "
                                "Teams camera = OBS Virtual Camera."
                            )
                    except queue.Empty:
                        pass  # re-send last (placeholder or freeze)

                    try:
                        # Burn-in TARGET on BGR before RGB convert / send
                        frame_out = self._apply_subtitle_burnin(last)
                        cam.send(_to_vcam(frame_out))
                        cam.sleep_until_next_frame()
                        if debug_preview:
                            try:
                                import cv2

                                prev = frame_out if frame_out is not None else last
                                if prev.shape[0] != h or prev.shape[1] != w:
                                    prev = cv2.resize(prev, (w, h))
                                cv2.imshow("LiveLingo webcam (debug)", prev)
                                cv2.waitKey(1)
                            except Exception:
                                pass
                    except Exception as exc:
                        self._set_error(f"vcam send: {exc}")
                        self._log(f"vcam send failed — will re-open: {exc}")
                        reopen = True
                        break
                    self._frames_sent += 1
                    n += 1
                    if n % 30 == 0:
                        dt = time.perf_counter() - t0
                        if dt > 0:
                            with self._status_lock:
                                self._status.fps_out = n / dt
                                self._status.frames_sent = self._frames_sent
                        n = 0
                        t0 = time.perf_counter()
            finally:
                self._vcam_ready.clear()
                with self._status_lock:
                    self._status.backend = ""
                    self._status.fps_out = 0.0
                if debug_preview:
                    try:
                        import cv2

                        cv2.destroyWindow("LiveLingo webcam (debug)")
                    except Exception:
                        pass
                if cam is not None:
                    try:
                        cam.close()
                    except Exception:
                        pass
                    cam = None
                # Give Windows/OBS a beat to release shared memory.
                time.sleep(0.25)

            if self._stop.is_set():
                break
            if reopen:
                continue
            # Disabled: loop back to wait_enable.

        self._set_emit_phase("stopped")


def build_webcam_service(config, log=print) -> Optional[WebcamLipSyncService]:
    """
    Factory: return service if WEBCAM_ENABLED else None.

    Does not start threads until ``start()`` (caller decides after pipeline ready).
    """
    if not bool(getattr(config, "WEBCAM_ENABLED", False)):
        return None
    deps = check_webcam_deps()
    if not deps["cv2"] or not deps["pyvirtualcam"]:
        miss = []
        if not deps["cv2"]:
            miss.append("opencv-python")
        if not deps["pyvirtualcam"]:
            miss.append("pyvirtualcam")
        log(
            "WEBCAM_ENABLED=true but missing: "
            + ", ".join(miss)
            + " — run: pip install opencv-python mediapipe pyvirtualcam"
        )
        # Still build so [cam status] can report; start() will fail clearly.
    try:
        return WebcamLipSyncService(config, log=log)
    except Exception as exc:
        log(f"Webcam service build failed: {exc}")
        return None
