"""Global shortcuts for screenshot and clip, usable while another window has the focus.

Baird runs from the Hub as a web app, so the shortcuts live in a small helper process, started and stopped with the
app (``python -m baird_hoard.hotkeys``). It registers the combinations with the Windows ``RegisterHotKey`` call (no
keyboard hook, nothing to install, no permission to grant), and when one is pressed it asks the running app to do
what the button does (``POST /api/snapshot`` or ``/api/clip``). The helper writes ``hotkeys-status.json`` so the
settings can say which combinations are registered and what the last press did.

This module only imports the standard library, so the settings can validate a combination without Windows."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN,
         "super": MOD_WIN}
_NAMED = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
          "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25,
          "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "scrolllock": 0x91,
          "numlock": 0x90}


def parse_hotkey(text: str) -> tuple[int, int]:
    """``"ctrl+alt+c"`` -> ``(modifiers, virtual-key code)``. Raises :class:`ValueError` with a message for the settings.
    A combination needs at least one of Ctrl, Alt, Win (a bare letter would steal typing); function keys and Print
    Screen may stand alone or with Shift."""
    parts = [p.strip().lower() for p in str(text or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("Write a combination such as ctrl+alt+c.")
    key = parts[-1]
    mods = 0
    for name in parts[:-1]:
        if name not in _MODS:
            raise ValueError(f"Unknown modifier {name!r}; use ctrl, alt, shift or win.")
        mods |= _MODS[name]
    if key in _MODS:
        raise ValueError("The combination needs a key after the modifiers.")
    if len(key) == 1 and key.isalnum() and key.isascii():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    elif key in _NAMED:
        vk = _NAMED[key]
    else:
        raise ValueError(f"Unknown key {key!r}.")
    standalone = key in ("printscreen", "prtsc", "pause", "scrolllock") or (key.startswith("f") and key[1:].isdigit())
    if not (mods & (MOD_CONTROL | MOD_ALT | MOD_WIN)) and not standalone:
        raise ValueError("Add Ctrl, Alt or Win to the combination.")
    return mods, vk


def normalize_hotkey(text: str) -> str:
    """The canonical spelling (``Ctrl+Alt+C`` becomes ``ctrl+alt+c``); raises like :func:`parse_hotkey`."""
    parse_hotkey(text)
    parts = [p.strip().lower() for p in str(text).replace(" ", "").split("+") if p.strip()]
    mods = [m for m in ("ctrl", "alt", "shift", "win") if any(_MODS.get(p) == _MODS[m] for p in parts[:-1])]
    return "+".join(mods + [parts[-1]])


# ---------------------------------------------------------------- the helper process

ACTIONS = {"shot": ("/api/snapshot", "Captura"), "clip": ("/api/clip", "Clip")}


def write_status(path: Optional[Path], data: dict[str, Any]) -> None:
    if not path:
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_status(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def call_app(port: int, route: str, timeout: float = 90.0) -> dict[str, Any]:
    """POST ``route`` to the running app; ``{ok, name?, error?}``."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": True, "name": body.get("name", "")}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        return {"ok": False, "error": str(body.get("error") or f"HTTP {exc.code}")[:200]}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def run(port: int, combos: dict[str, str], status_path: Optional[Path] = None) -> int:
    """Register ``combos`` (``{"shot": "ctrl+alt+c", "clip": "ctrl+alt+x"}``) and serve them until the process ends."""
    if os.name != "nt":
        write_status(status_path, {"pid": os.getpid(), "error": "Global shortcuts only work on Windows.", "registered": {}})
        return 1
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    registered: dict[str, dict[str, Any]] = {}
    ids: dict[int, str] = {}
    for n, (action, combo) in enumerate(combos.items(), start=1):
        try:
            mods, vk = parse_hotkey(combo)
        except ValueError as exc:
            registered[action] = {"combo": combo, "ok": False, "error": str(exc)}
            continue
        if user32.RegisterHotKey(None, n, mods | MOD_NOREPEAT, vk):
            registered[action] = {"combo": combo, "ok": True}
            ids[n] = action
        else:
            err = ctypes.get_last_error()
            registered[action] = {"combo": combo, "ok": False,
                                  "error": "Another program already uses this combination." if err == 1409 else f"Windows error {err}."}
    state: dict[str, Any] = {"pid": os.getpid(), "started": time.time(), "registered": registered, "last": None}
    write_status(status_path, state)
    msg = wintypes.MSG()
    WM_HOTKEY = 0x0312
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message != WM_HOTKEY or msg.wParam not in ids:
            continue
        action = ids[msg.wParam]
        route, label = ACTIONS[action]
        result = call_app(port, route)
        state["last"] = {"action": action, "label": label, "at": time.time(), **result}
        write_status(status_path, state)
        try:
            ctypes.windll.user32.MessageBeep(0x40 if result["ok"] else 0x10)
        except Exception:  # noqa: BLE001
            pass
    return 0


class HotkeyHelper:
    """Starts and stops the helper process for the app; ``restart`` applies new settings."""

    def __init__(self, status_path: Path, port: int):
        self.status_path = status_path
        self.port = port
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self, enabled: bool, combos: dict[str, str]) -> None:
        with self._lock:
            self._stop_locked()
            try:
                self.status_path.unlink(missing_ok=True)
            except OSError:
                pass
            if not enabled or os.name != "nt":
                return
            from .native import NO_WINDOW, bind_to_app
            cmd = [sys.executable, "-m", "baird_hoard.hotkeys", "--port", str(self.port), "--status", str(self.status_path)]
            for action, combo in combos.items():
                cmd += [f"--{action}", combo]
            root = Path(__file__).resolve().parent.parent
            self._proc = subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            bind_to_app(self._proc)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self, enabled: bool) -> dict[str, Any]:
        alive = self._proc is not None and self._proc.poll() is None
        data = read_status(self.status_path)
        return {"enabled": bool(enabled), "running": alive, "registered": data.get("registered", {}), "last": data.get("last"),
                "error": data.get("error", "")}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="baird_hoard.hotkeys")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--status", default="")
    ap.add_argument("--shot", default="")
    ap.add_argument("--clip", default="")
    args = ap.parse_args(argv)
    combos = {k: v for k, v in (("shot", args.shot), ("clip", args.clip)) if v}
    return run(args.port, combos, Path(args.status) if args.status else None)


if __name__ == "__main__":
    sys.exit(main())
