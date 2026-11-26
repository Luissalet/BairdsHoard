"""Demand-driven capture from the native helper or a synthetic ffmpeg source."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from .capture_source import CaptureSource, SyntheticSource
from .config import Settings
from .errors import BairdError
from .native import NATIVE_DIR, NO_WINDOW, STUCK_HINT, bind_to_app, ensure_helper, helper_pids
from .tsbuf import TsBuffer

log = logging.getLogger("baird.capture")

Listener = Callable[[bytes, list[int], int], None]  # aligned bytes, segment starts in them, stream offset after them

STUCK_MESSAGE = "The capture device stopped responding."
NO_SIGNAL_MESSAGE = "No picture is arriving from the capture device."
# The device delivers nothing in both cases; from this side they look the same.
NO_SIGNAL_HINT = ("The other computer is off or asleep, or its HDMI output is content-protected (HDCP): "
                  "a monitor on the pass-through still shows it, the device may not capture it.")
START_LIMIT_S = 30.0     # a helper that has produced nothing after this long is killed
SILENCE_LIMIT_S = 20.0   # the helper's own watchdog fires at 8 s; this is the backstop
EXIT_GRACE_S = 4.0       # time a helper gets to leave after reporting an error
KILL_GRACE_S = 6.0       # time a killed helper gets to disappear before the device counts as stuck
IDLE_GRACE_S = 10.0
RETRY_DELAYS_S = (15.0, 60.0, 300.0, 900.0)
OPEN_LIMIT = 12
OPEN_WINDOW_S = 3600.0
SOURCE_STUCK_MESSAGE = "The capture source did not stop."


class _HdmiSource:
    def command(self, settings: Settings) -> list[str]:
        cfg = settings.get()
        exe = ensure_helper()
        return [str(exe), "--device", cfg["device"], "--audio", cfg["audio_input"], "--out", "-"]

    def initial_state(self) -> dict[str, Any]:
        return {}  # Metadata arrives in the helper's ready event.


class CaptureEngine:
    def __init__(self, settings: Settings, buffer: TsBuffer, *, ffmpeg: Optional[str] = None, fake: bool = False,
                 on_state: Optional[Callable[[dict[str, Any]], None]] = None, fake_source: str = "",
                 source: CaptureSource | None = None, demand_only: bool = False, graceful_stop: bool = False):
        self.settings = settings
        self.buffer = buffer
        self._alternate_source = source is not None
        self._uses_hdmi_helper = source is None and not fake
        self._demand_only = demand_only
        self._graceful_stop = self._uses_hdmi_helper or graceful_stop
        self._needs_selection = False
        self._source: CaptureSource = source if source is not None else (
            SyntheticSource(ffmpeg, video=fake_source, marked=False) if fake else _HdmiSource()
        )
        self.on_state = on_state
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._changed = threading.Condition(self._lock)
        self._demands: set[object] = set()
        self._idle_until = 0.0
        self._retry_at = 0.0
        self._manual_open = False
        self._retry_step = 0
        self._limit_until = 0.0
        self._opening = False
        self._restart = threading.Event()
        self._stop = threading.Event()
        self._budget_path = Path(settings.path).with_name("capture-opens.sqlite3")
        self._wake = threading.Event()
        self._fatal: Optional[tuple[str, str, float]] = None  # code, message, time of the helper's last error
        self._fails = 0
        self._stuck_pids: set[int] = set()
        self._stuck_source: Optional[subprocess.Popen] = None
        self.state: dict[str, Any] = {"state": "idle", "message": "", "device": "", "encoder": "", "input": None,
                                      "kbps": 0, "since": None, "restarts": 0, "audio": False, "hint": ""}
        if self._demand_only:
            self.state.update(self._source.initial_state())

    # ------------------------------------------------------------------ listeners

    def add_listener(self, fn: Listener) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    # ------------------------------------------------------------------ lifecycle

    def _start(self) -> None:
        if not self._stop.is_set() and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, name="baird-capture", daemon=True)
            self._thread.start()

    def _wanted(self) -> bool:
        return (self.settings["capture_enabled"] and not self._needs_selection
                and bool(self._demands or (not self._demand_only and self.settings["capture_always_on"])))

    def set_demand(self, source: object, active: bool) -> None:
        with self._lock:
            if active:
                self._demands.add(source)
            else:
                if source in self._demands and len(self._demands) == 1:
                    self._idle_until = time.monotonic() + IDLE_GRACE_S
                self._demands.discard(source)
            if self._wanted():
                self._start()
            else:
                self._manual_open = False
            self._wake.set()

    @contextmanager
    def request(self):
        source = object()
        self.set_demand(source, True)
        try:
            yield
        finally:
            self.set_demand(source, False)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._changed:
            self._changed.notify_all()
        if self._thread:
            self._thread.join(timeout=8)

    def settings_changed(self, reopen: bool = False) -> None:
        with self._lock:
            if reopen:
                if self._opening or self._proc is not None:
                    self._restart.set()
                if self._demand_only:
                    self._needs_selection = False
                    self._retry_at = 0.0
                    self._retry_step = 0
            if self._wanted():
                self._start()
            else:
                self._manual_open = False
            self._wake.set()
            self._changed.notify_all()

    def retry_now(self, *, manual: bool = False) -> dict[str, Any]:
        with self._lock:
            if (self._wanted() and not self._opening and self._proc is None
                    and (manual or time.monotonic() >= self._limit_until)):
                if manual:
                    self._manual_open = True
                    self._limit_until = 0.0
                self._retry_at = 0.0
                self._wake.set()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self.state)
            out["retry_in_s"] = round(max(0.0, max(self._retry_at, self._limit_until) - time.monotonic()), 1)
            if not self.settings["capture_enabled"]:
                out.update(state="off", message="Capture is off.")
        out["buffered_seconds"] = round(self.buffer.seconds_kept(), 1)
        out["age_s"] = round(time.time() - self.buffer.last_data, 1) if self.buffer.last_data else None
        return out

    def wait_for_picture(self) -> None:
        """Wait for a pending opening, for up to twelve seconds."""
        deadline = time.monotonic() + 12
        with self._changed:
            while self._wanted() and not self._stop.is_set():
                try:
                    self.require_picture()
                    return
                except BairdError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or (not self._opening and self.snapshot()["retry_in_s"] > 0):
                        raise
                    self._changed.wait(remaining)
        self.require_picture()

    def require_picture(self) -> None:
        st = self.snapshot()
        if st["state"] == "reselect":
            raise BairdError("monitor_reselect", st["message"], status=409)
        if st["state"] != "running" or st["age_s"] is None or st["age_s"] > 3:
            hint = {"off": "Turn capture on.", "no_device": "Plug in the capture device (AVerMedia GL310).",
                    "stuck": STUCK_HINT,
                    "rate_limited": "Automatic retries are paused for this hour; the viewer's button opens once more.",
                    "no_signal": NO_SIGNAL_HINT}.get(st["state"], "")
            raise BairdError("no_signal", "There is no live picture right now.", hint, state=st["state"])

    # ------------------------------------------------------------------ internals

    def _set(self, **changes: Any) -> None:
        with self._changed:
            self.state.update(changes)
            snap = dict(self.state)
            self._changed.notify_all()
        if self.on_state:
            try:
                self.on_state(snap)
            except Exception:  # noqa: BLE001 - a listener must not stop capture
                log.debug("state listener failed", exc_info=True)

    def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if not self._graceful_stop:
            proc.terminate()
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
        return self._source.command(self.settings)

    def _block_for_selection(self, message: str) -> None:
        with self._lock:
            if self._restart.is_set():
                return
            self._needs_selection = True
        self.buffer.reset()
        self._set(state="reselect", message=message, input=None, kbps=0, audio=False, since=None,
                  audio_level=0.0, microphone_level=None)

    def _go_stuck(self, why: str) -> None:
        if self.state.get("state") != "stuck":
            log.error("capture device stuck: %s", why)
        self._set(state="stuck", message=STUCK_MESSAGE, hint=STUCK_HINT, input=None, kbps=0, since=None)

    def _reserve_opening(self, *, manual: bool = False) -> float:
        """Count each attempt before spawning, including across restarts sharing this data directory."""
        db = sqlite3.connect(self._budget_path)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS openings (started REAL NOT NULL)")
                now = time.time()
                db.execute("DELETE FROM openings WHERE started <= ?", (now - OPEN_WINDOW_S,))
                count, first = db.execute("SELECT COUNT(*), MIN(started) FROM openings").fetchone()
                if count >= OPEN_LIMIT and not manual:
                    return first + OPEN_WINDOW_S - now
                db.execute("INSERT INTO openings VALUES (?)", (now,))
                return 0.0
        finally:
            db.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                wanted = self._wanted()
                delay = max(self._retry_at, self._limit_until) - time.monotonic()
            if not wanted:
                self._wake.wait()
                continue
            if delay > 0:
                self._wake.wait(delay)
                continue

            with self._lock:
                self._opening = True
            attempted = True
            try:
                if self._stuck_source is not None and self._stuck_source.poll() is not None:
                    self._stuck_source = None
                if self._stuck_pids and self._uses_hdmi_helper:
                    self._stuck_pids &= helper_pids()
                if self._stuck_source is not None:
                    self._set(state="error", message=SOURCE_STUCK_MESSAGE, kbps=0, since=None)
                elif self._stuck_pids:
                    self._go_stuck("previous helper is still running")
                else:
                    attempted = self._run_once()
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, BairdError) and exc.code == "monitor_reselect":
                    self._block_for_selection(exc.message)
                else:
                    self._set(state="error", message=str(exc)[:300], kbps=0, since=None)
                    log.exception("capture loop")
            with self._changed:
                self._manual_open = False
                if attempted and not self._restart.is_set() and self.state["state"] in ("no_signal", "no_device", "stuck", "error"):
                    self._retry_at = time.monotonic() + RETRY_DELAYS_S[self._retry_step]
                    self._retry_step = min(self._retry_step + 1, len(RETRY_DELAYS_S) - 1)
                self._opening = False
                self._restart.clear()
                self._changed.notify_all()
        self._set(state="off", message="Stopped.", kbps=0, since=None)

    def _run_once(self) -> bool:
        with self._lock:
            if not self._wanted() or self._stop.is_set():
                return False
        cmd = self._command()
        source_state = self._source.initial_state()
        with self._lock:
            if not self._wanted() or self._stop.is_set() or self._restart.is_set():
                return False
            manual, self._manual_open = self._manual_open, False
            limit = 0.0 if self._alternate_source else self._reserve_opening(manual=manual)
            if limit:
                self._limit_until = time.monotonic() + limit
            else:
                self.buffer.reset()
                self._fatal = None
                proc = subprocess.Popen(cmd, cwd=str(NATIVE_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0, creationflags=NO_WINDOW)
                self._proc = proc
        if limit:
            self._set(state="rate_limited", message="Hourly capture opening limit reached.", hint="", kbps=0, since=None)
            return True
        message = "Opening the capture source…" if self._alternate_source else "Opening the capture device…"
        self._set(state="starting", message=message, hint="", input=None, kbps=0, since=None)
        bind_to_app(proc)
        if source_state:
            self._set(**source_state)
        err_thread = threading.Thread(target=self._read_events, args=(proc,), name="baird-capture-events", daemon=True)
        err_thread.start()
        box = {"got_data": False}
        pump = threading.Thread(target=self._pump, args=(proc, box), name="baird-capture-pump", daemon=True)
        pump.start()

        started = time.time()
        killed_at: Optional[float] = None
        why = ""
        closing = False
        while pump.is_alive():
            pump.join(0.25)
            if not pump.is_alive():
                break
            now = time.time()
            if killed_at is None:
                with self._lock:
                    closing = (self._stop.is_set() or self._restart.is_set() or not self.settings["capture_enabled"]
                               or (not self._wanted() and time.monotonic() >= self._idle_until))
                if closing:
                    self._kill()
                    killed_at = now
                    continue
                fatal = self._fatal
                if fatal and now - fatal[2] > EXIT_GRACE_S:
                    why = f"did not leave after '{fatal[0]}'"
                elif not box["got_data"] and now - started > START_LIMIT_S:
                    why = f"sent nothing for {START_LIMIT_S:.0f} s"
                elif box["got_data"] and self.buffer.last_data and now - self.buffer.last_data > SILENCE_LIMIT_S:
                    why = f"went silent for {SILENCE_LIMIT_S:.0f} s"
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
            self._proc = None
            if self._alternate_source:
                self._stuck_source = proc
                if not self._stop.is_set():
                    self._set(state="error", message=SOURCE_STUCK_MESSAGE, input=None, kbps=0, since=None)
                return True
            # The helper is stuck inside the driver and holds the device. Leave its threads behind.
            self._stuck_pids.add(proc.pid)
            self._fails += 1
            if not self._stop.is_set():
                self._go_stuck(f"helper {proc.pid} {why or 'did not exit'} and cannot be ended")
            return True
        err_thread.join(timeout=2)
        self._proc = None
        if closing or self._restart.is_set() or self._stop.is_set():
            self.buffer.reset()
            self._set(state="idle", message="", hint="", input=None, kbps=0, since=None)
            return True
        got_data = box["got_data"]
        with self._lock:
            self.state["restarts"] = int(self.state.get("restarts", 0)) + 1
        if self._alternate_source:
            fatal = self._fatal
            if fatal and fatal[0] == "monitor_reselect":
                self._block_for_selection(fatal[1])
            else:
                message = fatal[1] if fatal else f"The capture source stopped (code {code})."
                self._set(state="error", message=message, kbps=0, since=None)
            return True
        log.info("capture helper exited with %s", code)
        fatal = self._fatal
        if got_data:
            self._fails = 0
        if code in (0, 4, 5) or (fatal and fatal[0] == "no_data") or (why and got_data and not fatal):
            self._fails = 0
            self._set(state="no_signal", message=NO_SIGNAL_MESSAGE, hint=NO_SIGNAL_HINT, kbps=0, since=None)
        elif code == 2:
            self._fails = 0
            self._set(state="no_device", message="The capture device disappeared.", hint="", kbps=0, since=None)
        else:
            self._fails += 1
            hung = fatal is not None and fatal[0] == "run_failed" and "8007001F" in fatal[1].upper()
            if hung or self._fails >= 3 or self.state.get("state") == "stuck":
                self._go_stuck(f"{fatal[1] if fatal else f'exit {code}'} ({self._fails} failures in a row)")
            elif self.state.get("state") != "error":
                self._set(state="error", message=f"The capture helper stopped (code {code}).", kbps=0, since=None)
        return True

    def _pump(self, proc: subprocess.Popen, box: dict[str, bool]) -> None:
        """Read the source's stdout until it closes, feeding the buffer and the listeners."""
        out = proc.stdout
        if out is None:
            return
        ready = False
        try:
            while True:
                chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)  # type: ignore[union-attr]
                if not chunk or self._proc is not proc:
                    break
                aligned, starts = self.buffer.feed(chunk)
                if not aligned:
                    continue
                end_total = self.buffer.bytes_total
                box["got_data"] = True
                if not ready and self.buffer.latest()[1]:
                    if self._demand_only:
                        # A monitor emits its encoder before publishing TS. The two pipe readers can see them out of order.
                        with self._changed:
                            self._changed.wait_for(lambda: self.state["encoder"] or self._proc is not proc, timeout=1.0)
                            if self._proc is not proc or not self.state["encoder"]:
                                continue
                    ready = True
                    self._fails = 0
                    with self._lock:
                        self._retry_step = 0
                        self._retry_at = 0.0
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
                self._set(kbps=int(evt.get("kbps") or 0), audio=self.buffer.audio_pid is not None,
                          **{key: evt[key] for key in ("audio_level", "microphone_level") if key in evt})
            elif kind == "signal":
                self._set(input=evt.get("input"))
            elif kind == "error":
                code = str(evt.get("code", ""))
                message = str(evt.get("message", ""))[:300]
                if self._proc is proc:
                    self._fatal = (code, message, time.time())
                log.warning("capture helper: %s", line[:400])
            elif kind == "warning":
                log.warning("capture helper: %s", line[:400])

    def send(self, command: str) -> bool:
        """Send a line to the helper (``iframe``); False when it is not running."""
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None or not self._uses_hdmi_helper:
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
