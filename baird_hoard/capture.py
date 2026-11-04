"""The capture engine: runs ``bairdcap.exe`` (or, for demos and tests, an ffmpeg test pattern), reads the transport
stream from its stdout, keeps the recent past in a :class:`TsBuffer` and hands every chunk to the listeners (the
live view and the recorder). It restarts by itself after an unplug or a lost signal and frees the device when
capture is switched off.

A helper that does not leave after an error, or cannot be killed because it is stuck inside the driver, puts the
engine in the ``stuck`` state: the device needs to be unplugged and plugged back in. The engine notices the replug
(or the stuck helper going away) and starts again by itself; it never waits on a helper without a time limit."""

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
from .native import NATIVE_DIR, NO_WINDOW, STUCK_HINT, bind_to_app, ensure_helper, helper_pids, kill_pids, list_devices
from .tsbuf import TsBuffer

log = logging.getLogger("baird.capture")

Listener = Callable[[bytes, list[int], int], None]  # aligned bytes, segment starts in them, stream offset after them

STUCK_MESSAGE = "The capture device stopped responding."
START_LIMIT_S = 30.0     # a helper that has produced nothing after this long is killed
SILENCE_LIMIT_S = 20.0   # the helper's own watchdog fires at 8 s; this is the backstop
EXIT_GRACE_S = 4.0       # time a helper gets to leave after reporting an error
KILL_GRACE_S = 6.0       # time a killed helper gets to disappear before the device counts as stuck
STUCK_RETRY_S = 60.0     # while stuck, try the device again this often even without a replug


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
        self._fatal: Optional[tuple[str, str, float]] = None  # code, message, time of the helper's last error
        self._fails = 0
        self._stuck_pids: set[int] = set()
        self._stuck_since = 0.0
        self._saw_unplug = False
        self.state: dict[str, Any] = {"state": "off", "message": "", "device": "", "encoder": "", "input": None,
                                      "kbps": 0, "since": None, "restarts": 0, "audio": False, "hint": ""}

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
                    "stuck": STUCK_HINT,
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
            proc.kill()  # the run loop decides whether it actually went away

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

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _go_stuck(self, why: str) -> None:
        if self.state.get("state") != "stuck":
            log.error("capture device stuck: %s", why)
            self._stuck_since = time.time()
            self._saw_unplug = False
        self._set(state="stuck", message=STUCK_MESSAGE, hint=STUCK_HINT, input=None, kbps=0, since=None)

    def _stuck_cleared(self, names: list[str]) -> bool:
        """While stuck: True once the device was unplugged and is back, the stuck helpers are gone, or it is time for
        another try anyway."""
        dev = self.settings["device"].lower()
        present = any(dev in n.lower() for n in names)
        if not present:
            self._saw_unplug = True
            return False
        if self._saw_unplug:
            log.info("capture device plugged back in")
            return True
        if self._stuck_pids and not (self._stuck_pids & helper_pids()):
            log.info("stuck capture helper went away")
            return True
        return time.time() - self._stuck_since >= STUCK_RETRY_S

    def _clear_leftovers(self) -> None:
        """Helpers left by an earlier run of the app hold the device; end them, and remember the ones that will not."""
        pids = helper_pids()
        if not pids:
            return
        log.warning("ending leftover capture helpers %s", sorted(pids))
        kill_pids(pids)
        time.sleep(2)
        left = pids & helper_pids()
        if left:
            self._stuck_pids |= left
            self._go_stuck(f"leftover helpers {sorted(left)} cannot be ended")

    def _loop(self) -> None:
        backoff = 2.0
        if not self.fake:
            try:
                self._clear_leftovers()
            except Exception:  # noqa: BLE001
                log.debug("leftover check failed", exc_info=True)
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
                    if self.state.get("state") == "stuck":
                        if not self._stuck_cleared(names):
                            self._wait(3)
                            continue
                        self._stuck_since = time.time()
                        self._saw_unplug = False
                        self._stuck_pids &= helper_pids()
                    match = [n for n in names if dev.lower() in n.lower()]
                    if not match:
                        self._set(state="no_device", message=f"No capture device matching '{dev}' is connected.",
                                  hint="", input=None, kbps=0)
                        self._wait(3)
                        continue
                self._run_once()
                backoff = 2.0
            except BairdError as exc:
                if exc.code == "stuck":
                    self._go_stuck(exc.message)
                else:
                    self._set(state="error", message=exc.message, hint=exc.hint or "")
                log.warning("capture: %s", exc.message)
            except Exception as exc:  # noqa: BLE001
                self._set(state="error", message=str(exc)[:300])
                log.exception("capture loop")
            if self._stop.is_set():
                break
            if self.state.get("state") == "stuck" and not self.fake:
                continue  # the stuck branch above paces itself
            self._wait(backoff)
            backoff = min(backoff * 1.5, 10.0)
        self._set(state="off", message="Stopped.", kbps=0, since=None)

    def _run_once(self) -> None:
        cmd = self._command()
        self.buffer.reset()
        if self.state.get("state") != "stuck":
            self._set(state="starting", message="Opening the capture device…", hint="", kbps=0)
        self._fatal = None
        proc = subprocess.Popen(cmd, cwd=str(NATIVE_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=0, creationflags=NO_WINDOW)
        bind_to_app(proc)
        self._proc = proc
        err_thread = threading.Thread(target=self._read_events, args=(proc,), name="baird-capture-events", daemon=True)
        err_thread.start()
        if self.fake:
            self._set(state="running", message="Demo source (test pattern).", device="demo", encoder="ffmpeg",
                      input={"width": 1280, "height": 720}, since=time.time(), audio=True)
        box = {"got_data": False}
        pump = threading.Thread(target=self._pump, args=(proc, box), name="baird-capture-pump", daemon=True)
        pump.start()

        started = time.time()
        killed_at: Optional[float] = None
        why = ""
        while pump.is_alive():
            pump.join(1.0)
            if not pump.is_alive():
                break
            now = time.time()
            if killed_at is None:
                fatal = self._fatal
                if fatal and now - fatal[2] > EXIT_GRACE_S:
                    why = f"did not leave after '{fatal[0]}'"
                elif not box["got_data"] and now - started > START_LIMIT_S:
                    why = f"sent nothing for {START_LIMIT_S:.0f} s"
                elif box["got_data"] and self.buffer.last_data and now - self.buffer.last_data > SILENCE_LIMIT_S:
                    why = f"went silent for {SILENCE_LIMIT_S:.0f} s"
                elif self._stop.is_set() and proc.poll() is None:
                    why = "app is stopping"
                if why:
                    log.warning("capture helper %s %s; killing it", proc.pid, why)
                    proc.kill()
                    killed_at = now
            elif now - killed_at > KILL_GRACE_S:
                break
        try:
            code: Optional[int] = proc.wait(timeout=KILL_GRACE_S if killed_at else 2)
        except subprocess.TimeoutExpired:
            code = None
        if code is None:
            # Still there after a kill: it is stuck inside the driver and holds the device. Leave its threads behind.
            self._proc = None
            self._stuck_pids.add(proc.pid)
            self._fails += 1
            if not self._stop.is_set():
                self._go_stuck(f"helper {proc.pid} {why or 'did not exit'} and cannot be ended")
            return
        err_thread.join(timeout=2)
        self._proc = None
        got_data = box["got_data"]
        with self._lock:
            self.state["restarts"] = int(self.state.get("restarts", 0)) + 1
        log.info("capture helper exited with %s", code)
        if self._stop.is_set():
            return
        fatal = self._fatal
        if got_data:
            self._fails = 0
        if code in (4, 5) or (code == 0 and not got_data):
            self._fails = 0
            self._set(state="no_signal", message="No picture: the other computer is off, asleep or not sending to HDMI.",
                      hint="", kbps=0, since=None)
        elif code == 2:
            self._fails = 0
            self._set(state="no_device", message="The capture device disappeared.", hint="", kbps=0, since=None)
        elif code not in (0, None):
            self._fails += 1
            hung = fatal is not None and fatal[0] == "run_failed" and "8007001F" in fatal[1].upper()
            if hung or self._fails >= 3 or self.state.get("state") == "stuck":
                self._go_stuck(f"{fatal[1] if fatal else f'exit {code}'} ({self._fails} failures in a row)")
            elif self.state.get("state") != "error":
                self._set(state="error", message=f"The capture helper stopped (code {code}).", kbps=0, since=None)

    def _pump(self, proc: subprocess.Popen, box: dict[str, bool]) -> None:
        """Read the helper's stdout until it closes, feeding the buffer and the listeners."""
        out = proc.stdout
        if out is None:
            return
        try:
            while True:
                chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)  # type: ignore[union-attr]
                if not chunk:
                    break
                aligned, starts = self.buffer.feed(chunk)
                if not aligned:
                    continue
                end_total = self.buffer.bytes_total
                if not box["got_data"]:
                    box["got_data"] = True
                    self._fails = 0
                    self._stuck_pids.clear()
                    if self.state["state"] != "running":
                        self._set(state="running", message="", hint="", since=time.time())
                with self._lock:
                    listeners = list(self._listeners)
                for fn in listeners:
                    try:
                        fn(aligned, starts, end_total)
                    except Exception:  # noqa: BLE001
                        log.exception("capture listener failed")
        except (OSError, ValueError):
            log.debug("capture pipe closed", exc_info=True)

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
                code = str(evt.get("code", ""))
                message = str(evt.get("message", ""))[:300]
                self._fatal = (code, message, time.time())
                if self.state.get("state") != "stuck":
                    state = "no_signal" if code == "no_data" else "no_device" if code == "device_not_found" else "error"
                    self._set(state=state, message=message, hint="")
                log.warning("capture helper: %s", line[:400])
            elif kind == "warning":
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
