"""The capture engine: runs ``bairdcap.exe`` (or, for demos and tests, an ffmpeg test pattern), reads the transport
stream from its stdout, keeps the recent past in a :class:`TsBuffer` and hands every chunk to the listeners (the
live view and the recorder). It restarts by itself after an unplug or a lost signal and frees the device when
capture is switched off."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from .config import Settings
from .errors import BairdError
from .native import NATIVE_DIR, NO_WINDOW, ensure_helper, list_devices
from .tsbuf import TsBuffer

log = logging.getLogger("baird.capture")

Listener = Callable[[bytes, list[int], int], None]  # aligned bytes, segment starts in them, stream offset after them


class CaptureEngine:
    def __init__(self, settings: Settings, buffer: TsBuffer, *, ffmpeg: Optional[str] = None, fake: bool = False,
                 on_state: Optional[Callable[[dict[str, Any]], None]] = None):
        self.settings = settings
        self.buffer = buffer
        self.ffmpeg = ffmpeg
        self.fake = fake
        self.on_state = on_state
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.state: dict[str, Any] = {"state": "off", "message": "", "device": "", "encoder": "", "input": None,
                                      "kbps": 0, "since": None, "restarts": 0, "audio": False}

    # ------------------------------------------------------------------ listeners

    def add_listener(self, fn: Listener) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="baird-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=8)

    def restart(self) -> None:
        """Apply new settings: stop the helper; the loop starts it again (or stays off)."""
        self._kill()
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self.state)
        out["buffered_seconds"] = round(self.buffer.seconds_kept(), 1)
        out["age_s"] = round(time.time() - self.buffer.last_data, 1) if self.buffer.last_data else None
        return out

    def require_picture(self) -> None:
        st = self.snapshot()
        if st["state"] != "running" or st["age_s"] is None or st["age_s"] > 3:
            hint = {"off": "Turn capture on.", "no_device": "Plug in the capture device (AVerMedia GL310).",
                    "no_signal": "Check that the other computer is on and sends a picture to the HDMI cable."}.get(st["state"], "")
            raise BairdError("no_signal", "There is no live picture right now.", hint, state=st["state"])

    # ------------------------------------------------------------------ internals

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self.state.update(changes)
            snap = dict(self.state)
        if self.on_state:
            try:
                self.on_state(snap)
            except Exception:  # noqa: BLE001 - a listener must not stop capture
                log.debug("state listener failed", exc_info=True)

    def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(b"quit\n")
                proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _command(self) -> list[str]:
        cfg = self.settings.get()
        if self.fake:
            if not self.ffmpeg:
                raise BairdError("unavailable", "ffmpeg is needed for the demo source.")
            return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-re",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "30", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "96k", "-f", "mpegts", "pipe:1"]
        exe = ensure_helper()
        return [str(exe), "--device", cfg["device"], "--audio", cfg["audio_input"], "--out", "-"]

    def _loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            if not self.settings["capture_enabled"]:
                self._set(state="off", message="Capture is off.", input=None, kbps=0, since=None)
                self._wake.wait(30)
                self._wake.clear()
                continue
            try:
                if not self.fake:
                    dev = self.settings["device"]
                    names = list_devices()
                    match = [n for n in names if dev.lower() in n.lower()]
                    if not match:
                        self._set(state="no_device", message=f"No capture device matching '{dev}' is connected.", input=None, kbps=0)
                        self._wake.wait(3)
                        self._wake.clear()
                        continue
                self._run_once()
                backoff = 2.0
            except BairdError as exc:
                self._set(state="error", message=exc.message)
                log.warning("capture: %s", exc.message)
            except Exception as exc:  # noqa: BLE001
                self._set(state="error", message=str(exc)[:300])
                log.exception("capture loop")
            if self._stop.is_set():
                break
            self._wake.wait(backoff)
            self._wake.clear()
            backoff = min(backoff * 1.5, 10.0)
        self._set(state="off", message="Stopped.", kbps=0, since=None)

    def _run_once(self) -> None:
        cmd = self._command()
        self.buffer.reset()
        self._set(state="starting", message="Opening the capture device…", kbps=0)
        proc = subprocess.Popen(cmd, cwd=str(NATIVE_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=0, creationflags=NO_WINDOW)
        self._proc = proc
        err_thread = threading.Thread(target=self._read_events, args=(proc,), name="baird-capture-events", daemon=True)
        err_thread.start()
        if self.fake:
            self._set(state="running", message="Demo source (test pattern).", device="demo", encoder="ffmpeg",
                      input={"width": 1280, "height": 720}, since=time.time(), audio=True)
        got_data = False
        try:
            out = proc.stdout
            assert out is not None
            while True:
                chunk = out.read(65536) if not hasattr(out, "read1") else out.read1(65536)  # type: ignore[union-attr]
                if not chunk:
                    break
                aligned, starts = self.buffer.feed(chunk)
                if not aligned:
                    continue
                end_total = self.buffer.bytes_total
                if not got_data:
                    got_data = True
                    if self.state["state"] != "running":
                        self._set(state="running", message="", since=time.time())
                with self._lock:
                    listeners = list(self._listeners)
                for fn in listeners:
                    try:
                        fn(aligned, starts, end_total)
                    except Exception:  # noqa: BLE001
                        log.exception("capture listener failed")
        finally:
            code = proc.wait()
            err_thread.join(timeout=2)
            self._proc = None
            with self._lock:
                self.state["restarts"] = int(self.state.get("restarts", 0)) + 1
            if self._stop.is_set():
                return
            if code in (4, 5) or (code == 0 and not got_data):
                self._set(state="no_signal", message="No picture: the other computer is off, asleep or not sending to HDMI.",
                          kbps=0, since=None)
            elif code == 2:
                self._set(state="no_device", message="The capture device disappeared.", kbps=0, since=None)
            elif code not in (0, None) and self.state.get("state") != "error":
                self._set(state="error", message=f"The capture helper stopped (code {code}).", kbps=0, since=None)
            log.info("capture helper exited with %s", code)

    def _read_events(self, proc: subprocess.Popen) -> None:
        err = proc.stderr
        if err is None:
            return
        for raw in iter(err.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                log.debug("helper: %s", line)
                continue
            kind = evt.get("event")
            if kind == "ready":
                self._set(device=evt.get("device", ""), encoder=evt.get("encoder", ""), input=evt.get("input"),
                          audio=self.buffer.audio_pid is not None or True)
                log.info("capture ready: %s", line[:400])
            elif kind == "stats":
                self._set(kbps=int(evt.get("kbps") or 0), audio=self.buffer.audio_pid is not None)
            elif kind == "signal":
                self._set(input=evt.get("input"))
            elif kind == "error":
                code = evt.get("code", "")
                state = "no_signal" if code == "no_data" else "no_device" if code == "device_not_found" else "error"
                self._set(state=state, message=str(evt.get("message", ""))[:300])
                log.warning("capture helper: %s", line[:400])

    def send(self, command: str) -> bool:
        """Send a line to the helper (``iframe``); False when it is not running."""
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None or self.fake:
            return False
        try:
            proc.stdin.write((command.strip() + "\n").encode())
            proc.stdin.flush()
            return True
        except OSError:
            return False


def ffmpeg_path() -> Optional[str]:
    """ffmpeg from the shared finder (BAIRD_FFMPEG / FFMPEG env, PATH, WinGet), else None."""
    try:
        from .hoard_link.media import bins
        tool = bins.find("ffmpeg", legacy_env=("BAIRD_FFMPEG",))
        if tool:
            return str(getattr(tool, "path", "") or getattr(tool, "argv", [""])[0]) or None
    except Exception:  # noqa: BLE001
        log.debug("bins.find failed", exc_info=True)
    import shutil
    return shutil.which("ffmpeg")


def ffprobe_path(ffmpeg: Optional[str]) -> Optional[str]:
    import shutil
    if ffmpeg:
        cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffprobe")
