"""The native capture helper (``native/bairdcap.exe``): find it, and build it from ``native/bairdcap.cs`` when it is
missing or older than its source. Windows always ships the .NET Framework 4 C# compiler, so no SDK is needed."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from .config import REPO_ROOT
from .errors import BairdError

log = logging.getLogger("baird.native")

NATIVE_DIR = REPO_ROOT / "native"
SOURCE = NATIVE_DIR / "bairdcap.cs"
LIBRARY = NATIVE_DIR / "DirectShowLib.dll"
EXE = NATIVE_DIR / "bairdcap.exe"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_lock = threading.Lock()


def csc_path() -> Path | None:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for sub in ("Framework64", "Framework"):
        candidates = sorted((windir / "Microsoft.NET" / sub).glob("v4.*/csc.exe"), reverse=True)
        if candidates:
            return candidates[0]
    return None


def ensure_helper() -> Path:
    """Path of a current ``bairdcap.exe``; builds it when needed. Raises :class:`BairdError` when it cannot."""
    with _lock:
        if EXE.is_file() and (not SOURCE.is_file() or EXE.stat().st_mtime >= SOURCE.stat().st_mtime):
            return EXE
        if os.name != "nt":
            raise BairdError("unavailable", "The capture helper only runs on Windows.")
        csc = csc_path()
        if csc is None:
            raise BairdError("unavailable", "The .NET Framework C# compiler (csc.exe) was not found.",
                             "It ships with Windows; repair the .NET Framework 4.8 feature.")
        out = EXE.with_suffix(".new.exe")
        cmd = [str(csc), "/nologo", "/optimize+", "/platform:x64", f"/out:{out}", f"/reference:{LIBRARY}", str(SOURCE)]
        proc = subprocess.run(cmd, cwd=str(NATIVE_DIR), capture_output=True, text=True, creationflags=NO_WINDOW, timeout=120)
        if proc.returncode != 0 or not out.is_file():
            raise BairdError("failed", "Building the capture helper failed.", (proc.stdout + proc.stderr)[-800:])
        os.replace(out, EXE)
        log.info("built %s", EXE)
        return EXE


def list_devices() -> list[str]:
    """DirectShow video input names (``bairdcap --list``)."""
    exe = ensure_helper()
    proc = subprocess.run([str(exe), "--list"], cwd=str(NATIVE_DIR), capture_output=True, text=True, creationflags=NO_WINDOW,
                          timeout=20)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
