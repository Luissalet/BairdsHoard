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
        try:
            os.replace(out, EXE)
        except PermissionError:
            # A helper that never finished exiting still has the old file open; Windows lets it be renamed.
            old = EXE.with_suffix(".old.exe")
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
            os.replace(EXE, old)
            os.replace(out, EXE)
        log.info("built %s", EXE)
        return EXE


def list_devices() -> list[str]:
    """DirectShow video input names (``bairdcap --list``). Raises :class:`BairdError` ``stuck`` when the listing does
    not finish (a driver that stopped responding)."""
    exe = ensure_helper()
    proc = subprocess.Popen([str(exe), "--list"], cwd=str(NATIVE_DIR), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    bind_to_app(proc)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise BairdError("stuck", "Listing the capture devices did not finish.", STUCK_HINT)
    return [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]


STUCK_HINT = "Unplug the capture device's USB cable, wait five seconds and plug it back in."

# ---------------------------------------------------------------- helper processes

_job = None


def bind_to_app(proc: subprocess.Popen) -> None:
    """Put a helper in a job that Windows closes when this app exits, so it never outlives Baird."""
    global _job
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if _job is None:
            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = k32.CreateJobObjectW(None, None)
            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "job setup failed")
            _job = job
        if not k32.AssignProcessToJobObject(_job, int(proc._handle)):  # type: ignore[attr-defined]
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    except Exception:  # noqa: BLE001 - only a safety net
        log.debug("could not bind the helper to the app", exc_info=True)


def helper_pids() -> set[int]:
    """Process ids of every running ``bairdcap.exe``, including ones that are stuck exiting inside the driver."""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq bairdcap.exe", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, creationflags=NO_WINDOW, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("bairdcap") and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def kill_pids(pids: set[int]) -> None:
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
