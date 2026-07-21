#!/usr/bin/env python3
"""
dev_reload.py
=============
Development auto-restart for LiveLingo.

Python CLIs do not hot-reload modules. This watcher runs `main.py` as a child
and restarts it whenever project ``*.py`` content changes.

Important:
  * Start the app with this script, NOT ``python main.py`` alone:
        python dev_reload.py
        python dev_reload.py -v
  * Full process restart — session / mic state is lost (expected).
  * Uses **content hashes** (not only mtime) so changes are detected on
    WSL ``/mnt/c/...`` mounts where Windows mtime is often unreliable.

Stop with Ctrl+C in this terminal (stops child + watcher).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLL_S = 0.75
DEBOUNCE_S = 0.5
IGNORE_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    ".idea",
    ".vscode",
}


def _log(msg: str) -> None:
    print(f"[dev_reload] {msg}", flush=True)


def _iter_py_files(root: Path):
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored / hidden dirs in-place
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORE_DIR_NAMES and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _file_fingerprint(path: Path) -> str | None:
    """
    Content-based fingerprint (mtime alone fails on WSL→Windows drives).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    h = hashlib.md5(data).hexdigest()
    try:
        st = path.stat()
        # Include size as cheap extra signal
        return f"{h}:{st.st_size}"
    except OSError:
        return h


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in _iter_py_files(root):
        # Do not restart the watcher when only this file is edited mid-run
        # in a way that would loop; still watch it so fingerprint set is stable.
        fp = _file_fingerprint(path)
        if fp is not None:
            out[str(path.resolve())] = fp
    return out


def _changed(prev: dict[str, str], cur: dict[str, str]) -> list[str]:
    keys = set(prev) | set(cur)
    return sorted(k for k in keys if prev.get(k) != cur.get(k))


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    pid = proc.pid
    _log(f"stopping child pid={pid}…")

    if os.name == "nt":
        # Force-kill process tree (CTRL_BREAK is unreliable for Python CLIs).
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except Exception as exc:
            _log(f"taskkill failed ({exc}); trying terminate()")
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        return

    # POSIX / WSL
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
    try:
        proc.wait(timeout=4)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _start_child(py: str, main_py: Path, child_args: list[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env["LIVELINGO_DEV_RELOAD"] = "1"
    cmd = [py, str(main_py), *child_args]

    if os.name == "nt":
        # New process group so taskkill /T can target the tree.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            creationflags=flags,
        )
    else:
        # New session/process group for clean SIGTERM to the whole tree.
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            start_new_session=True,
        )
    _log(f"started child pid={proc.pid}: {' '.join(cmd)}")
    return proc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-restart LiveLingo when .py sources change."
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=DEBOUNCE_S,
        help=f"Seconds to wait after first change (default {DEBOUNCE_S})",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=POLL_S,
        help=f"Poll interval seconds (default {POLL_S})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log which files triggered a reload",
    )
    args, child_args = parser.parse_known_args()
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]

    py = sys.executable
    main_py = ROOT / "main.py"
    if not main_py.is_file():
        print(f"[dev_reload] main.py not found at {main_py}", file=sys.stderr)
        sys.exit(1)

    n_files = sum(1 for _ in _iter_py_files(ROOT))
    _log(f"watching {ROOT}")
    _log(f"tracking {n_files} .py file(s) via content hash (WSL-safe)")
    _log("run THIS script for auto-reload — not `python main.py` alone")
    _log("Ctrl+C here stops watcher + app")

    snap = _snapshot(ROOT)
    if args.verbose:
        _log(f"initial snapshot keys={len(snap)}")

    proc: subprocess.Popen | None = None

    def start() -> None:
        nonlocal proc
        proc = _start_child(py, main_py, child_args)

    def stop() -> None:
        nonlocal proc
        if proc is None:
            return
        if proc.poll() is None:
            _kill_process_tree(proc)
        proc = None

    try:
        start()
        while True:
            time.sleep(max(0.2, args.poll))

            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                _log(
                    f"child exited code={code}. "
                    f"Edit a .py file to restart, or Ctrl+C to quit."
                )
                proc = None

            cur = _snapshot(ROOT)
            hits = _changed(snap, cur)
            if not hits:
                continue

            # Debounce editor multi-write (save → temp → rename).
            time.sleep(max(0.1, args.debounce))
            cur = _snapshot(ROOT)
            hits = _changed(snap, cur)
            snap = cur
            if not hits:
                continue

            rels = []
            for h in hits:
                try:
                    rels.append(str(Path(h).resolve().relative_to(ROOT)))
                except Exception:
                    rels.append(h)

            if args.verbose:
                _log("changed: " + ", ".join(rels))
            else:
                _log(f"{len(rels)} file(s) changed — restarting…")
                if len(rels) <= 5:
                    _log("  " + ", ".join(rels))

            stop()
            time.sleep(0.2)
            start()
    except KeyboardInterrupt:
        _log("Ctrl+C — shutting down.")
    finally:
        stop()


if __name__ == "__main__":
    main()
