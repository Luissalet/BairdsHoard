"""Copilot mode: an assistant guides the person live by drawing on top of the other computer's picture.

(The Faustus chat panel every Hoard page carries lives under ``/api/copilot``; this is Baird's own thing, under
``/copilot``: marks over the live view.)

The person keeps working on the captured computer and looks at it through Baird. An assistant that was told to take
over sends *steps*: a short instruction plus *marks* - a red dot where to click, a box around something, an arrow for
a drag, the text to type next to its field, the keys to press. Baird draws them over the live picture and moves on
to the next step **by itself**: it watches the picture (a small grey copy, a few times a second) and the text on it
(the OCR that ships with Windows, ``textmap.py``), so the assistant is not asked anything between steps. Marks can
name the text they point at (``"click": "Guardar"``) instead of coordinates; Baird finds it, follows it when the
page scrolls and shows the mark as soon as the text is on screen, which lets an assistant send a whole plan for
screens it has not seen yet.

Everything is built to cost the assistant few calls: one call shows a plan and waits until it is finished or the
person needs something, and its answer already carries what happened and, when eyes are needed, the picture.

Nothing here is stored: sessions, events and the pictures an assistant looked at live in memory and go when the
session ends. A session nobody touches ends by itself (``copilot.idle_minutes``), and with it the capture demand."""

from __future__ import annotations

import base64
import logging
import math
import queue
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Optional

from .copilot_steps import (ACTIONS, MAX_SAY, MAX_STEPS, MAX_TALK, STEP_KEYS, Space, _bad, _clamp, _numbers, clean_step, public_mark,
                            repair)
from .errors import BairdError
from .overlay import CopilotOverlay
from .textmap import count_text, find_text, scaled, squash

log = logging.getLogger("baird.copilot")

# The grey copy of the picture the runner compares (FrameTap) and what counts as a change. Ratios are parts of it.
TAP_W, TAP_H, TAP_FPS = 160, 90, 5
FEED_MAX = 200             # chunks of the stream the tap may lag behind before it is dropped and restarted
PIXEL_STEP = 16             # a grey level that moved by more than this is a changed cell: under it the device's encoder still flickers
                            # on a still screen (measured on its recordings), and a dark dialog over a dark page moves little more
MOTION_QUIET = 0.003        # less than this between two frames: the screen is still
ARM_S = 0.5                 # a step starts watching this long after it is shown...
QUIET_ARM_S = 0.3           # ...once the screen has been still for this long
AMBIENT_S = 4.0             # a screen that never goes still (a video) cannot be watched for changes: the person confirms
CHANGE_MIN = 0.004          # the least the whole picture must change for a change around a mark to count
CHANGE_LOCAL = 0.03         # how much of the area around a mark must change
CHANGE_FAR = 0.015          # a change this large counts wherever it is (a dialog opened elsewhere, the page went on)...
CORNER = 0.70               # ...unless all of it sits in a right-hand corner, where notifications pop up
CHANGE_PLAIN = 0.015        # steps without a place (keys): the whole picture
REGION_CHANGE = 0.02        # until={"change": [x, y, w, h]}: that rectangle
SETTLE_S = 0.45             # after a change, wait for the screen to go still before moving on
MAX_SETTLE_S = 3.0
MOVE_MIN = 0.02             # an anchored text that moved this far (part of the picture) means the page scrolled
READ_EVERY_S = 0.8
SUB_KEEP = 32               # states kept for a page that is slow to read them
SHIFT_TOL = 0.01            # two lines of text moved the same way when their shifts differ by less than this
MISSING_S = 6.0             # a text that is still not on screen after this long is reported to the assistant
LINGER_S = 12.0             # an ended session stays this long so the page shows how it ended
LOOK_KEEP = 6
LOG_KEEP = 80               # lines of the conversation the page shows
LOOK_SPACE_S = 900.0        # how long the last look stays the space coordinates are read in
MAX_WAIT_S = 150.0
TICK_S = 0.15
ATTENTION = ("done", "help", "say", "stopped", "stalled", "anchor_missing")
NEEDS_EYES = ("done", "help", "say", "stalled", "anchor_missing")


def changed(a: Optional[bytes], b: Optional[bytes], box: Optional[list[float]] = None, size: tuple[int, int] = (TAP_W, TAP_H)) -> float:
    """Part (0..1) of the grey frame, or of ``box`` (fractions of it), whose cells moved by more than :data:`PIXEL_STEP`."""
    if not a or not b or len(a) != len(b):
        return 0.0
    if box is None:
        return sum(1 for x, y in zip(a, b) if abs(x - y) > PIXEL_STEP) / len(a)
    w, h = size
    x0 = max(0, min(w - 1, int(box[0] * w)))
    y0 = max(0, min(h - 1, int(box[1] * h)))
    x1 = max(x0 + 1, min(w, math.ceil((box[0] + box[2]) * w)))
    y1 = max(y0 + 1, min(h, math.ceil((box[1] + box[3]) * h)))
    moved = 0
    for row in range(y0, y1):
        start = row * w
        moved += sum(1 for x, y in zip(a[start + x0:start + x1], b[start + x0:start + x1]) if abs(x - y) > PIXEL_STEP)
    return moved / ((x1 - x0) * (y1 - y0))


def spread(a: Optional[bytes], b: Optional[bytes], size: tuple[int, int] = (TAP_W, TAP_H)) -> tuple[float, Optional[list[float]]]:
    """Part of the grey frame that changed and the rectangle that holds every changed cell (fractions), if any."""
    if not a or not b or len(a) != len(b):
        return 0.0, None
    w, h = size
    cells = [i for i, (x, y) in enumerate(zip(a, b)) if abs(x - y) > PIXEL_STEP]
    if not cells:
        return 0.0, None
    cols = [i % w for i in cells]
    x0, x1, y0, y1 = min(cols), max(cols) + 1, cells[0] // w, cells[-1] // w + 1
    return len(cells) / len(a), [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h]


# ---------------------------------------------------------------------------------------------- watching the picture

def line_places(text_map: dict[str, Any]) -> dict[str, Optional[tuple[float, float]]]:
    """Where each line of text starts (fractions of the picture), by its text; ``None`` for a text that is there twice."""
    w, h = max(1, text_map["w"]), max(1, text_map["h"])
    out: dict[str, Optional[tuple[float, float]]] = {}
    for line in text_map["lines"]:
        key = squash(str(line[4])) if len(line) > 4 else ""
        if len(key) >= 3:
            out[key] = None if key in out else (line[0] / w, line[1] / h)
    return out


def carried(before: dict[str, Optional[tuple[float, float]]], now: dict[str, Optional[tuple[float, float]]], text: str,
            dx: float, dy: float) -> bool:
    """Whether the page itself moved by (dx, dy) between two readings (it scrolled, a window was dragged), judging by
    the other lines of text that are in both: they moved the same way. One such line is enough for a straight move up,
    down or sideways (a long scroll leaves few lines in sight), two for any other; with no line to ask, a straight
    move is taken for a scroll."""
    skip = squash(text)
    same = total = 0
    for key, was in before.items():
        at = now.get(key)
        if was is None or at is None or (skip and skip in key):
            continue
        total += 1
        if abs(at[0] - was[0] - dx) <= SHIFT_TOL and abs(at[1] - was[1] - dy) <= SHIFT_TOL:
            same += 1
    straight = abs(dx) <= SHIFT_TOL or abs(dy) <= SHIFT_TOL
    if total == 0:
        return straight
    return same >= (1 if straight else 2) or (same == 1 and total <= 2)


class FrameTap:
    """Small grey frames of the live picture, a few per second: one ffmpeg fed with the device's stream for as long as
    a session runs. Feeding never holds the capture back: when ffmpeg cannot keep up the tap is dropped and restarted."""

    def __init__(self, engine: Any, ffmpeg: Optional[str], *, clock: Callable[[], float] = time.monotonic):
        self.engine = engine
        self.ffmpeg = ffmpeg
        self.clock = clock
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._feed: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=FEED_MAX)
        self._frame: Optional[tuple[int, bytes, float]] = None
        self._seq = 0
        self._primed = False
        self._primed_at = 0
        self._listening = False
        self._started_at = -10.0
        self._overrun = False

    def latest(self) -> Optional[tuple[int, bytes, float]]:
        """``(sequence, grey bytes, when)`` of the newest frame, or ``None``."""
        return self._frame

    def ensure(self) -> None:
        """Start (or restart) the tap when there is a live picture; called from the runner every tick."""
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
            if alive and not self._overrun:
                return
            if alive or self._proc is not None:
                self._stop_locked()
            if not self.ffmpeg or self.clock() - self._started_at < 2.0 or self.engine.snapshot().get("state") != "running":
                return
            self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            self._frame = None

    def restart(self) -> None:
        """The source changed (a new capture session): the old decoder is of no use."""
        with self._lock:
            self._stop_locked()
            self._started_at = -10.0

    def _start_locked(self) -> None:
        from .native import NO_WINDOW, bind_to_app
        # No "nobuffer" here: it throws away what was read while probing, and the frames up to the next key frame
        # then come out as copies of the first one - a second of a screen that looks still and is not.
        cmd = [str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt", "-flags", "low_delay",
               "-probesize", "200000", "-analyzeduration", "500000", "-threads", "2", "-f", "mpegts", "-i", "pipe:0", "-an",
               "-vf", f"fps={TAP_FPS},scale={TAP_W}:{TAP_H}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
        below_normal = 0x00004000 if NO_WINDOW else 0
        self._proc = proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                             creationflags=NO_WINDOW | below_normal)
        bind_to_app(proc)
        self._started_at = self.clock()
        self._overrun = False
        self._feed = feed = queue.Queue(maxsize=FEED_MAX)
        self._primed = False
        head, self._primed_at = self.engine.buffer.tail()
        if head:
            feed.put(head)
            self._primed = True
        threading.Thread(target=self._write, args=(proc, feed), name="baird-copilot-tap-in", daemon=True).start()
        threading.Thread(target=self._read, args=(proc,), name="baird-copilot-tap-out", daemon=True).start()
        if not self._listening:
            self.engine.add_listener(self._on_data)
            self._listening = True

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if self._listening:
            self.engine.remove_listener(self._on_data)
            self._listening = False
        if proc is None:
            return
        try:
            self._feed.put_nowait(None)
        except queue.Full:
            pass
        if proc.poll() is None:
            proc.kill()

    def _on_data(self, aligned: bytes, starts: list[int], end_total: int) -> None:
        # Runs in the capture thread: never block here.
        if self._proc is None or self._overrun or end_total <= self._primed_at:
            return
        data = aligned
        if not self._primed:
            if not starts:
                return
            data = self.engine.buffer.header + aligned[starts[0]:]
            self._primed = True
        try:
            self._feed.put_nowait(data)
        except queue.Full:
            self._overrun = True

    @staticmethod
    def _write(proc: subprocess.Popen, feed: "queue.Queue[Optional[bytes]]") -> None:
        try:
            while proc.poll() is None:
                try:
                    chunk = feed.get(timeout=1.0)
                except queue.Empty:
                    continue
                if chunk is None or proc.stdin is None:
                    break
                proc.stdin.write(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass

    def _read(self, proc: subprocess.Popen) -> None:
        out, want = proc.stdout, TAP_W * TAP_H
        if out is None:
            return
        buf = b""
        try:
            while True:
                chunk = out.read(want - len(buf))
                if not chunk:
                    break
                buf += chunk
                if len(buf) == want:
                    if self._proc is proc:
                        self._seq += 1
                        self._frame = (self._seq, buf, self.clock())
                    buf = b""
        except (OSError, ValueError):
            pass


class _Run:
    """What the runner knows about the step that is on screen."""

    def __init__(self, now: float, settled: Optional[bytes] = None):
        self.shown_at = now
        self.armed = settled is not None    # watching started (the screen went still after the step appeared)
        self.ambient = False                # the screen never goes still: changes cannot tell anything
        self.blind = False                  # no frames at all: only the person, a text or the clock end the step
        self.baseline: Optional[bytes] = settled
        self.prev: Optional[bytes] = settled
        self.frame_seq = -1
        self.quiet_since: Optional[float] = now if settled is not None else None
        self.hit = False                    # the newest frame differs from the baseline the way doing the step would
        self.triggered_at: Optional[float] = None
        self.verify = False                 # a change is waiting for the text to say whether the page only scrolled
        self.activity = True                # something, however small, moved since the text was last read
        self.dirty = True                   # the screen moved for real since the text was last read
        self.read_at = -10.0
        self.read_armed = False             # the text was read once since watching started
        self.base_count: Optional[int] = None
        self.places: Optional[dict[str, Optional[tuple[float, float]]]] = None   # where each line was at the last reading
        self.gone_misses = 0
        self.stalled = False
        self.missing_told = False


class _Session:
    def __init__(self, agent: str, goal: str, now: float):
        self.id = uuid.uuid4().hex[:8]
        self.agent = agent
        self.goal = goal
        self.steps: list[dict[str, Any]] = []
        self.index = 0
        self.status = "active"       # active | done (plan finished, waiting for the assistant) | stopped
        self.run: Optional[_Run] = None
        self.started = now
        self.touched = now
        self.agent_at = now
        self.delivered = 0
        self.waiters = 0
        self.help_at: Optional[float] = None
        self.message = ""
        self.ended_by = ""
        self.ended_at = 0.0
        self.log: list[dict[str, Any]] = []     # what was said, in order: the steps as they come up, the assistant, the person
        self.lines = 0
        self.unanswered: list[dict[str, Any]] = []      # what the person said or asked since the assistant last showed anything

    def ask(self, item: dict[str, Any]) -> None:
        """The person wants something from the assistant. It rides in every answer until the assistant shows or says
        something, so a call that was cut short does not lose it."""
        self.unanswered.append(item)
        del self.unanswered[:-6]

    def say(self, who: str, text: str, **more: Any) -> None:
        """Add a line to the conversation: ``who`` is step, agent or you."""
        self.lines += 1
        self.log.append({"id": self.lines, "who": who, "text": text, **more})
        del self.log[:-LOG_KEEP]

    def step_shown(self) -> None:
        """The step on screen joins the conversation the first time it comes up."""
        if self.status == "active" and self.index < len(self.steps):
            step = self.steps[self.index]
            if not step.get("_line"):
                self.say("step", step["say"], n=self.index + 1, hint=step["hint"], done=False)
                step["_line"] = self.lines

    def step_done(self, step: dict[str, Any]) -> None:
        for entry in self.log:
            if entry["id"] == step.get("_line"):
                entry["done"] = True


class _Subscriber:
    """A page that gets every new state (the copilot WebSocket)."""

    def __init__(self, loop: Any):
        import asyncio
        self.loop = loop
        self.queue: "asyncio.Queue[Any]" = asyncio.Queue()

    def _put(self, item: Any) -> None:
        while self.queue.qsize() >= SUB_KEEP:       # a page that stopped reading: every state is whole, the old ones can go
            self.queue.get_nowait()
        self.queue.put_nowait(item)

    def push(self, item: Any) -> None:
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:          # the loop is closed
            pass


class Copilot:
    def __init__(self, *, engine: Any, library: Any, settings: Any, reader: Any, ffmpeg: Optional[str], work_dir: Path,
                 frames: Any = None, clock: Callable[[], float] = time.monotonic, background: bool = True):
        self.engine = engine
        self.library = library
        self.settings = settings
        self.reader = reader
        self.work_dir = work_dir
        self.clock = clock
        self.background = background            # False: nobody ticks (tests drive _tick themselves)
        self.frames = frames if frames is not None else FrameTap(engine, ffmpeg, clock=clock)
        self._cond = threading.Condition(threading.RLock())
        self.capture_publication = nullcontext
        self._session: Optional[_Session] = None
        self._events: deque[dict[str, Any]] = deque(maxlen=300)
        self._seq = 0
        self._rev = 0
        self._subs: list[_Subscriber] = []
        self._looks: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._last_look: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._res_lock = threading.Lock()
        self._demand = False
        self._demand_key = object()
        self._tap_restart = False
        self.overlay: CopilotOverlay | None = None
        self.text_error = ""
        self._sweep()

    def _sweep(self) -> None:
        """A reading that was cut short (the app was killed) leaves a picture of the screen behind: remove it."""
        try:
            for stray in self.work_dir.glob("copilot-*.bmp"):
                stray.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ helpers

    def _config(self) -> dict[str, Any]:
        return self.settings["copilot"]

    def _require_enabled(self) -> dict[str, Any]:
        cfg = self._config()
        if not cfg["enabled"]:
            raise BairdError("unavailable", "Copilot mode is turned off in Baird.",
                             "Ask the person to turn it on: the Copilot button in Baird's top bar.", status=409)
        return cfg

    def _space(self, spec: dict[str, Any]) -> Space:
        raw = spec.get("space")
        if raw is not None:
            pair = _numbers(raw, 2)
            if not pair or pair[0] <= 0 or pair[1] <= 0:
                raise _bad("space must be [width, height] of the picture you measured your coordinates on.")
            return Space((pair[0], pair[1]))
        with self._cond:
            named = spec.get("look")
            if not isinstance(named, str) or named in ("", "auto", "always", "never"):
                named = None
            look_id = named or self._last_look
            look = self._looks.get(str(look_id)) if look_id else None
            if named and look is None:
                raise _bad(f"There is no look {named[:40]!r} any more.", "Look again and use the new one.")
            if look is not None and (named or self.clock() - look["at"] <= LOOK_SPACE_S):
                return Space(look["size"], look["view"])
        return Space(self.library.input_size())

    def _emit(self, event: str, **data: Any) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, "event": event, **{k: v for k, v in data.items() if v not in (None, "")}})
        self._cond.notify_all()

    def _publish(self) -> None:
        self._rev += 1
        if self.overlay is not None or self._subs:
            state = self.state(publish_overlay=True)
            for sub in list(self._subs):
                sub.push(state)
        self._cond.notify_all()

    def subscribe(self, loop: Any) -> _Subscriber:
        sub = _Subscriber(loop)
        with self._cond:
            self._subs.append(sub)
        sub.push(self.state())
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._cond:
            if sub in self._subs:
                self._subs.remove(sub)

    # ------------------------------------------------------------------ what the page and the assistant see

    def state(self, *, publish_overlay: bool = False) -> dict[str, Any]:
        """Everything the page draws."""
        with self._cond:
            cfg = self._config()
            sess = self._session
            out: dict[str, Any] = {"enabled": cfg["enabled"], "rev": self._rev, "reads_text": bool(self.reader.available()), "session": None}
            if sess is not None:
                now = self.clock()
                info: dict[str, Any] = {"id": sess.id, "agent": sess.agent, "goal": sess.goal, "status": sess.status, "index": sess.index,
                                        "count": len(sess.steps), "listening": sess.waiters > 0 or now - sess.agent_at < 20,
                                        "asked_help": sess.help_at is not None, "message": sess.message, "ended_by": sess.ended_by,
                                        "steps": [{"say": s["say"]} for s in sess.steps], "step": None, "log": list(sess.log)}
                if sess.status == "active" and sess.run is not None:
                    step, run = sess.steps[sess.index], sess.run
                    mode = step["_mode"]
                    auto = mode != "manual" and not (mode == "change" and (run.ambient or run.blind))
                    info["step"] = {"n": sess.index + 1, "say": step["say"], "hint": step["hint"], "keys": step["keys"],
                                    "marks": [public_mark(m) for m in step["marks"]], "mode": mode, "auto": auto,
                                    "until_text": step["until"].get("text", "")}
                out["session"] = info
            if self.overlay is not None:
                if publish_overlay:
                    self.overlay.update(out["session"])
                overlay = self.overlay.status()
                if overlay["available"]:
                    out["overlay"] = overlay
            return out

    def status(self) -> dict[str, Any]:
        """The same in the few words an assistant needs."""
        with self._cond:
            sess = self._session
            if sess is None:
                return {"session": "none"}
            out: dict[str, Any] = {"session": sess.status, "step": min(sess.index + 1, len(sess.steps)), "of": len(sess.steps)}
            if sess.status == "active" and sess.run is not None:
                step = sess.steps[sess.index]
                out["say"] = step["say"]
                mode = step["_mode"]
                if mode == "change" and (sess.run.ambient or sess.run.blind):
                    mode = "manual"
                out["ends_on"] = mode
                searching = [m["find"] for m in step["marks"] if m["state"] == "searching" and m["find"]]
                if searching:
                    out["searching"] = searching
                several = {m["find"]: m["_matches"] for m in step["marks"] if m["find"] and m["_matches"] > 1}
                if several:           # the first one in reading order was taken: nth, near or a longer text picks another
                    out["several_on_screen"] = several
            elif sess.status == "stopped":
                out["by"] = sess.ended_by
            if sess.unanswered and sess.status != "stopped":
                out["unanswered"] = list(sess.unanswered)       # said by the person and not answered yet
            return out

    # ------------------------------------------------------------------ the assistant's side

    def show(self, spec: dict[str, Any], *, wait_s: float = 0.0, each: bool = False, look: str = "auto",
             gone: Optional[threading.Event] = None) -> dict[str, Any]:
        """Show steps: starts a session when there is none. ``replace`` (default true) drops the steps that were not
        done yet; false adds the new ones after them. With ``wait_s`` it then waits like :meth:`wait`."""
        self._require_enabled()
        if not isinstance(spec, dict):
            raise _bad("Send an object.", 'Example: {"steps": [{"say": "Abre Archivo", "click": "Archivo"}]}.')
        spec = repair(spec)
        raw_steps = spec.get("steps")
        if raw_steps is None:
            own = {k: v for k, v in spec.items() if k in STEP_KEYS}
            raw_steps = [own] if own else []
        if isinstance(raw_steps, (str, dict)):
            raw_steps = [raw_steps]
        message = str(spec.get("message") or "").strip()[:MAX_TALK]
        if not isinstance(raw_steps, (list, tuple)) or not (raw_steps or message):
            raise _bad("Nothing to show.", 'Send steps: [{"say": "...", "click": "text on screen"}], a message for the person, or both.')
        if len(raw_steps) > MAX_STEPS:
            raise _bad(f"At most {MAX_STEPS} steps at once.", "Send the first ones and the rest when they are done.")
        space = self._space(spec)
        steps = [clean_step(raw, space) for raw in raw_steps]
        agent = str(spec.get("agent") or "").strip()[:40]
        goal = str(spec.get("goal") or "").strip()[:160]
        with self._cond:
            now = self.clock()
            sess = self._session
            if sess is None or sess.status == "stopped":
                sess = self._session = _Session(agent, goal, now)
                sess.delivered = self._seq
            else:
                sess.agent = agent or sess.agent
                sess.goal = goal or sess.goal
            if message:
                sess.say("agent", message)
            sess.touched = sess.agent_at = now
            sess.help_at = None
            sess.unanswered.clear()             # the assistant has reacted to whatever the person said
            if steps:
                keep = sess.steps[:sess.index] if spec.get("replace", True) is not False or sess.status == "done" else sess.steps
                fresh = len(keep) <= sess.index           # the step on screen is a new one
                sess.steps = keep + steps
                sess.status = "active"
                if fresh or sess.run is None:
                    sess.run = _Run(now)
                sess.step_shown()
            elif sess.status == "active" and sess.run is None:      # a new session that only talks: nothing to do yet
                sess.status = "done"
            self._ensure_runner()
            self._publish()
        self._sync_resources()
        self._read_for_current(sess)
        if wait_s and wait_s > 0:
            return self.wait(wait_s, each=each, look=look, gone=gone)
        out = self.status()
        out["space"] = self._space_note(space)
        return out

    def _space_note(self, space: Space) -> Any:
        return [round(space.size[0]), round(space.size[1])] if space.size else None

    def wait(self, wait_s: float = 60.0, *, each: bool = False, look: str = "auto",
             gone: Optional[threading.Event] = None) -> dict[str, Any]:
        """Wait until something needs the assistant: the plan is finished, the person asks for help or writes, a text
        that was pointed at is not on screen, a step takes too long, or the session ended. ``each`` also returns on
        every step that is done. Returns the events since the last call and where the session is.

        ``gone`` is set by whoever carries the call when the caller hung up: the wait ends without taking the events,
        which stay for the next call instead of going into an answer nobody reads."""
        try:
            limit = min(max(float(wait_s), 0.0), MAX_WAIT_S)
        except (TypeError, ValueError):
            limit = 0.0
        if limit != limit:                      # NaN gets through min and max, and a wait of NaN seconds never ends
            limit = 0.0
        deadline = time.monotonic() + limit
        with self._cond:
            sess = self._session
            if sess is None:
                return {"session": "none", "events": [], "next": "No session is running. copilot_show starts one."}
            sess.touched = sess.agent_at = self.clock()
            sess.waiters += 1
            self._publish()
            left = False
            try:
                while True:
                    if gone is not None and gone.is_set():
                        left = True
                        break
                    fresh = [e for e in self._events if e["seq"] > sess.delivered]
                    wake = [e for e in fresh if each or e["event"] in ATTENTION]
                    if wake or sess.status == "stopped" or self._session is not sess:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(min(remaining, 0.5))
            finally:
                sess.waiters -= 1
            if left:
                self._publish()
                return {"session": sess.status if self._session is sess else "stopped", "events": [], "abandoned": True}
            if fresh:
                sess.delivered = fresh[-1]["seq"]
            sess.touched = sess.agent_at = self.clock()
            if any(e["event"] == "help" for e in fresh):
                sess.help_at = None
            out = self.status() if self._session is sess else {"session": "stopped", "by": sess.ended_by}
            out["events"] = [{k: v for k, v in e.items() if k != "seq"} for e in fresh]
            kinds = [e["event"] for e in wake]
            if not wake and sess.status == "active":
                out["still_waiting"] = True
            out["next"] = self._next_hint(sess, kinds)
            self._publish()
        if out["session"] in ("active", "done") and (look == "always" or (look != "never" and any(k in NEEDS_EYES for k in kinds))):
            self._attach_look(out)
        return out

    @staticmethod
    def _next_hint(sess: _Session, kinds: list[str]) -> str:
        if sess.status == "stopped":
            return "The session ended. Do not show anything else unless the person asks again."
        if "help" in kinds or "say" in kinds:
            return "The person needs you: read the event, look at the picture, answer with a message and show the corrected step(s)."
        if sess.unanswered and not kinds:
            return "The person is still waiting for your answer (unanswered): reply with a message and, if needed, new steps."
        if "anchor_missing" in kinds:
            return "A text you pointed at is not on screen: look, then show the step again with a text that is there or with coordinates."
        if "stalled" in kinds:
            return "No progress on this step: look and help (a clearer mark, or another way)."
        if sess.status == "done":
            return "The plan is finished: check the picture, then show the next steps or copilot_stop."
        return "Still on the plan: call copilot_wait again."

    def _attach_look(self, out: dict[str, Any]) -> None:
        try:
            out.update(self.look())
        except BairdError as exc:
            out["look_error"] = str(exc)

    def look(self, *, max_side: Optional[int] = None, region: Any = None, text: bool = False, image: bool = True) -> dict[str, Any]:
        """The picture now, small enough to be cheap, and the space the assistant's coordinates are read in from now on.
        ``region`` ([x, y, w, h] in the current space) zooms; ``text`` adds every text on screen with its place."""
        publication = self.capture_publication()
        cfg = self._require_enabled()
        side = int(min(max(int(max_side or cfg["look_side"]), 320), 2048))
        view = (0.0, 0.0, 1.0, 1.0)
        crop = None
        if region is not None:
            quad = _numbers(region, 4) if not isinstance(region, dict) else _numbers([region.get(k) for k in ("x", "y", "w", "h")], 4)
            if not quad:
                raise _bad("region must be [x, y, w, h] in the coordinates of your last look.")
            box = self._space({}).box(*quad)
            view = (box[0], box[1], box[2], box[3])
            crop = {"x": box[0], "y": box[1], "w": box[2], "h": box[3], "unit": "fraction"}
        with self.engine.request():
            self.engine.wait_for_picture()
            picture = self.library.input_size()
            jpeg, width, height = (self.library.grab_jpeg(crop, side) if image or picture is None else (b"", 0, 0))
            lines = self._read_text() if text else None
        if not image:
            scale = min(1.0, side / max(picture[0] * view[2], picture[1] * view[3]))
            width, height = max(1, round(picture[0] * view[2] * scale)), max(1, round(picture[1] * view[3] * scale))
        with publication, self._cond:
            look_id = uuid.uuid4().hex[:6]
            self._looks[look_id] = {"view": view, "size": (width, height), "jpeg": jpeg, "at": self.clock()}
            while len(self._looks) > LOOK_KEEP:
                self._looks.popitem(last=False)
            self._last_look = look_id
            if self._session is not None:
                self._session.touched = self._session.agent_at = self.clock()
        out: dict[str, Any] = {"look": look_id, "space": [width, height]}
        if picture:
            out["picture"] = [picture[0], picture[1]]
        if text:
            out["text"] = self._dump(lines, Space((width, height), view)) if lines is not None else []
            if lines is None:
                out["text_error"] = self.text_error or "The text on screen could not be read."
        if image:
            out["_image"] = {"data": base64.b64encode(jpeg).decode("ascii"), "mime": "image/jpeg"}
        return out

    def clear_looks(self) -> None:
        with self._cond:
            self._looks.clear()
            self._last_look = None

    def look_image(self, look_id: str) -> bytes:
        with self._cond:
            look = self._looks.get(look_id)
        if look is None or not look["jpeg"]:
            raise BairdError("not_found", "That look is no longer kept.", "Look again.")
        return look["jpeg"]

    @staticmethod
    def _dump(text_map: dict[str, Any], space: Space) -> list[str]:
        """Every line of text inside the view as ``"x,y text"`` (its centre, in the assistant's pixels)."""
        w, h = max(1, text_map.get("w") or 1), max(1, text_map.get("h") or 1)
        out = []
        for line in text_map.get("lines", []):
            fx, fy = (line[0] + line[2] / 2) / w, (line[1] + line[3] / 2) / h
            if space.view[0] <= fx <= space.view[0] + space.view[2] and space.view[1] <= fy <= space.view[1] + space.view[3]:
                x, y = space.px(fx, fy)
                out.append(f"{x},{y} {line[4]}")
        return out

    def texts(self, query: str = "") -> dict[str, Any]:
        """The texts on screen with their place (no picture): all of them, or where ``query`` is written."""
        self._require_enabled()
        query = repair(str(query or ""))
        space = self._space({})
        with self.engine.request():
            self.engine.wait_for_picture()
            text_map = self._read_text()
        if text_map is None:
            raise BairdError("unavailable", self.text_error or "The text on screen could not be read.", "Marks placed by coordinates still work.")
        if not space.size:
            space = Space((text_map["w"], text_map["h"]))
        out: dict[str, Any] = {"space": self._space_note(space)}
        if query.strip():
            w, h = max(1, text_map["w"]), max(1, text_map["h"])
            matches = []
            for m in find_text(text_map["lines"], query, limit=8):
                box = [m["box"][0] / w, m["box"][1] / h, m["box"][2] / w, m["box"][3] / h]
                matches.append({"text": m["text"], "at": space.px(box[0] + box[2] / 2, box[1] + box[3] / 2), "box": space.px_box(box),
                                "score": m["score"]})
            out.update(query=query.strip(), count=len(matches), matches=matches)
        else:
            out["lines"] = self._dump(text_map, space)
        return out

    def stop(self, message: str = "", *, by: str = "agent") -> dict[str, Any]:
        with self._cond:
            sess = self._session
            if sess is None or sess.status == "stopped":
                return {"session": "none"}
            self._end(sess, by, str(repair(message) or "").strip()[:MAX_SAY])
        self._sync_resources()
        return {"session": "stopped", "by": by}

    # ------------------------------------------------------------------ the person's side

    def user(self, action: str, text: str = "") -> dict[str, Any]:
        """What the person does on the page: next, back, repeat, help, say (a line for the assistant) or stop."""
        action = str(action or "").lower()
        text = str(text or "").strip()[:400]
        with self._cond:
            sess = self._session
            if action == "stop":
                if sess is not None and sess.status != "stopped":
                    self._end(sess, "user", "")
            elif sess is None or sess.status == "stopped":
                raise BairdError("not_found", "No hay ninguna guía en marcha.", status=409)
            else:
                now = self.clock()
                sess.touched = now
                if action == "next":
                    if sess.status == "active":
                        self._advance(sess, "user")
                elif action == "back":
                    if sess.index > 0:
                        sess.index = min(sess.index, len(sess.steps)) - 1
                        sess.status = "active"
                        sess.run = _Run(now)
                        sess.step_shown()
                        self._emit("back", step=sess.index + 1)
                elif action == "repeat":
                    if sess.status == "active":
                        sess.run = _Run(now)
                elif action == "help":
                    sess.help_at = now
                    sess.say("you", text or "No me sale este paso.")
                    sess.ask({"event": "help", "step": min(sess.index + 1, len(sess.steps)), **({"text": text} if text else {})})
                    self._emit("help", step=min(sess.index + 1, len(sess.steps)), text=text)
                elif action == "say":
                    if not text:
                        raise _bad("Escribe algo para el copilot.")
                    sess.help_at = now
                    sess.say("you", text)
                    sess.ask({"event": "say", "text": text})
                    self._emit("say", text=text)
                else:
                    raise _bad(f"Unknown action: {action!r}.", "next, back, repeat, help, say or stop.")
            self._publish()
        self._sync_resources()
        return self.state()

    # ------------------------------------------------------------------ session life

    def _end(self, sess: _Session, by: str, message: str) -> None:
        if message:
            sess.say("agent", message)
        sess.status = "stopped"
        sess.run = None
        sess.ended_by = by
        sess.message = message
        sess.ended_at = self.clock()
        self._emit("stopped", by=by)
        self._publish()

    def _advance(self, sess: _Session, by: str, settled: Optional[bytes] = None, text_map: Optional[dict[str, Any]] = None) -> None:
        """The step is done. ``settled`` is the still frame the screen ended on, when the runner saw it: the next step
        starts watching from it at once instead of waiting for the screen to go still again. ``text_map`` is the
        reading that showed the step was done: the next step's text marks are placed from it, with no second reading."""
        sess.step_done(sess.steps[sess.index])
        done = sess.index + 1
        sess.index = done
        sess.touched = self.clock()
        self._emit("advanced", step=done, by=by)
        if done >= len(sess.steps):
            sess.status = "done"
            sess.run = None
            self._emit("done", steps=len(sess.steps))
        else:
            sess.run = run = _Run(self.clock(), settled)
            sess.step_shown()
            if text_map is not None and self._wants_text(sess.steps[sess.index]):
                run.read_at, run.read_armed, run.activity, run.dirty = self.clock(), True, False, False
                self._after_read(sess, text_map, first=True)
                return
        self._publish()

    def _ensure_runner(self) -> None:
        if self.background and not self._closed.is_set() and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, name="baird-copilot", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._closed.is_set():
            try:
                alive = self._tick()
            except Exception:  # noqa: BLE001 - the guide must keep going
                log.exception("copilot tick failed")
                alive = True
            self._sync_resources()
            if not alive:
                with self._cond:
                    if self._session is None:
                        self._thread = None
                        return
            self._closed.wait(TICK_S)

    def _sync_resources(self) -> None:
        """Capture demand and the frame tap follow the session. Never called with the copilot lock held (the capture
        engine has its own)."""
        with self._res_lock:
            with self._cond:
                sess = self._session
                want = sess is not None and sess.status in ("active", "done") and not self._closed.is_set()
                restart, self._tap_restart = self._tap_restart, False
            tap = self.frames if isinstance(self.frames, FrameTap) else None
            if want:
                if not self._demand:
                    self.engine.set_demand(self._demand_key, True)
                    self._demand = True
                if tap is not None:
                    if restart:
                        tap.restart()
                    tap.ensure()
            else:
                if tap is not None:
                    tap.stop()
                if self._demand:
                    self.engine.set_demand(self._demand_key, False)
                    self._demand = False

    def settings_changed(self) -> None:
        """The person changed the copilot's settings: turning it off ends what is on screen."""
        with self._cond:
            sess = self._session
            if not self._config()["enabled"] and sess is not None and sess.status != "stopped":
                self._end(sess, "user", "")
            self._publish()
        self._sync_resources()

    def source_changed(self, running: bool) -> None:
        """A new capture session started or the picture went away (called by the capture engine's state hook)."""
        self._tap_restart = True
        if self.overlay is not None:
            self.overlay.source_changed(running)

    def overlay_changed(self) -> None:
        with self._cond:
            self._publish()

    def overlay_control(self, action: str, language: str | None = None) -> dict[str, Any]:
        if self.overlay is None:
            raise BairdError("unavailable", "Monitor overlay is unavailable.", status=409)
        with self._cond:
            self.overlay.control(action, language)
            self._publish()
            return self.state()

    def close(self) -> None:
        self._closed.set()
        if self.overlay is not None:
            self.overlay.close()
        with self._cond:
            sess = self._session
            if sess is not None and sess.status != "stopped":
                self._end(sess, "shutdown", "")
            self._session = None
            for sub in self._subs:
                sub.push(None)
            self._cond.notify_all()
        self._sync_resources()
        try:
            self.reader.stop()
        except Exception:  # noqa: BLE001
            log.debug("stopping the text reader", exc_info=True)

    # ------------------------------------------------------------------ reading the screen

    def _read_text(self) -> Optional[dict[str, Any]]:
        """Every text on the picture now (``{"w", "h", "lines"}`` in picture pixels), or ``None`` when it cannot be
        read; the reason stays in ``text_error``. About half a second: never call it with the lock held."""
        path = self.work_dir / f"copilot-{uuid.uuid4().hex[:10]}.bmp"
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            size = self.library.input_size()
            scale = max(1.0, min(2.0, 4096 / size[0])) if size else 2.0
            self.library.grab_file(None, path, scale=scale)
            result = scaled(self.reader.read(path, self._config()["ocr_lang"]), scale)
            self.text_error = ""
        except BairdError as exc:
            self.text_error = str(exc)[:200]
            return None
        except Exception as exc:  # noqa: BLE001
            self.text_error = f"{type(exc).__name__}: {exc}"[:200]
            log.debug("reading the screen failed", exc_info=True)
            return None
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return result

    def _read_for_current(self, sess: _Session) -> None:
        """Place the text marks of the step on screen right away, so the assistant's call already says what was found."""
        with self._cond:
            run = sess.run
            if self._session is not sess or sess.status != "active" or run is None or not self._wants_text(sess.steps[sess.index]):
                return
        try:
            self.engine.wait_for_picture()
        except BairdError:
            return
        text_map = self._read_text()
        with self._cond:
            if self._session is sess and sess.run is run and text_map is not None:
                run.read_at = self.clock()
                self._after_read(sess, text_map, first=True)

    @staticmethod
    def _wants_text(step: dict[str, Any]) -> bool:
        return step["_mode"] in ("typed", "text", "gone") or any(m["_anchor"] or m["_to_anchor"] for m in step["marks"])

    @staticmethod
    def _locate(anchor: dict[str, Any], text_map: dict[str, Any], stick: Optional[tuple[float, float]] = None
                ) -> Optional[tuple[list[float], tuple[float, float], int, tuple[float, float]]]:
        """The box of the anchored text, the point it stands for (fractions), how many equally good places show that
        text and the middle of the text itself; ``None`` when it is not on screen. ``stick`` is where the text was at
        the last reading: of several places, the nearest is the same one."""
        matches = find_text(text_map["lines"], anchor["text"], limit=12)
        if not matches:
            return None
        w, h = max(1, text_map["w"]), max(1, text_map["h"])
        best = matches[0]["score"]
        tier = [m for m in matches if m["score"] >= best - 0.05] if matches[0]["exact"] else matches

        def away(m: dict[str, Any], at: tuple[float, float]) -> float:
            return ((m["box"][0] + m["box"][2] / 2) / w - at[0]) ** 2 + ((m["box"][1] + m["box"][3] / 2) / h - at[1]) ** 2

        if stick is not None:
            pick = min(matches, key=lambda m: away(m, stick))
        elif anchor["near"]:
            pick = min(matches, key=lambda m: away(m, anchor["near"]))
        elif anchor["nth"]:
            ordered = sorted(tier, key=lambda m: (round(m["box"][1] / 12), m["box"][0]))
            if anchor["nth"] > len(ordered):
                return None
            pick = ordered[anchor["nth"] - 1]
        else:
            pick = tier[0]
        x, y, bw, bh = pick["box"]
        px, py = x + bw / 2, y + bh / 2
        gap_x, gap_y = max(40.0, 2 * bh), max(24.0, 1.5 * bh)
        side = anchor["side"]
        if side == "right":
            px = x + bw + gap_x
        elif side == "left":
            px = x - gap_x
        elif side == "below":
            py = y + bh + gap_y
        elif side == "above":
            py = y - gap_y
        pad = 6.0
        box = [_clamp((x - pad) / w), _clamp((y - pad) / h), round((bw + 2 * pad) / w, 5), round((bh + 2 * pad) / h, 5)]
        count = 1 if anchor["near"] or anchor["nth"] else len(tier)
        return box, (_clamp(px / w + anchor["dx"]), _clamp(py / h + anchor["dy"])), count, ((x + bw / 2) / w, (y + bh / 2) / h)

    def _place_anchors(self, step: dict[str, Any], text_map: dict[str, Any],
                       before: Optional[dict[str, Optional[tuple[float, float]]]] = None,
                       now: Optional[dict[str, Optional[tuple[float, float]]]] = None) -> tuple[bool, float]:
        """Put the text marks where their text is now. Returns whether every text was found and how far the page
        carried the farthest mark (part of the picture).

        A mark that is already placed keeps to its own copy of the text, the nearest one, and follows it across the
        screen only when the other lines moved the same way (``before`` and ``now``: where each line was and is). The
        same words turning up somewhere else, in a title or an address bar, are what doing the step looks like, not a
        scroll: the mark stays where it was."""
        found_all, moved = True, 0.0
        for mark in step["marks"]:
            if not (mark["_anchor"] or mark["_to_anchor"]):
                continue
            ok = True
            for key, kept in (("_anchor", "_at"), ("_to_anchor", "_to_at")):
                anchor = mark[key]
                if not anchor:
                    continue
                was = mark.get(kept) if before is not None else None
                spot = self._locate(anchor, text_map, stick=was)
                if spot is not None and was is not None:
                    dx, dy = spot[3][0] - was[0], spot[3][1] - was[1]
                    far = math.hypot(dx, dy)
                    if far >= MOVE_MIN:
                        if carried(before or {}, now or {}, anchor["text"], dx, dy):
                            moved = max(moved, far)
                        else:
                            spot = None
                if spot is None:
                    ok = False
                    continue
                box, (x, y), count, mark[kept] = spot
                if key == "_to_anchor":
                    mark["to"] = [x, y]
                    continue
                if was is None:
                    mark["_matches"] = count
                mark.update(x=x, y=y)
                if mark["kind"] in ("box", "type") and not anchor["side"]:
                    mark["box"] = box
            placed = mark["x"] is not None and (mark["kind"] != "arrow" or mark["to"] is not None)
            mark["state"] = "ok" if ok else "lost" if placed else "searching"
            found_all = found_all and ok
        return found_all, moved

    def _after_read(self, sess: _Session, text_map: Optional[dict[str, Any]], first: bool = False) -> None:
        step, run = sess.steps[sess.index], sess.run
        assert run is not None
        verify, run.verify = run.verify, False
        frame = self.frames.latest()
        settled = frame[1] if frame is not None else None
        if text_map is None:
            if verify:                      # nothing can tell a scroll from the real thing: take the change
                self._advance(sess, "screen", settled)
            return
        unplaced = any(m["state"] == "searching" for m in step["marks"])
        places = line_places(text_map)
        _, moved = self._place_anchors(step, text_map, run.places, places)
        run.places = places
        if unplaced and not any(m["state"] == "searching" for m in step["marks"]):
            # The target has just come on screen: what changed until now was its arrival, not the person doing the step.
            run.baseline, run.triggered_at, run.hit = settled, None, False
        mode = step["_mode"]
        if mode == "typed":
            seen = count_text(text_map["lines"], step["_probe"])
            if run.base_count is None:
                run.base_count = seen
            elif seen > run.base_count:
                self._advance(sess, "typed", text_map=text_map)
                return
        elif mode == "text" and not first:
            if count_text(text_map["lines"], step["until"]["text"]) > 0:
                self._advance(sess, "text", text_map=text_map)
                return
        elif mode == "gone" and not first:
            if count_text(text_map["lines"], step["until"]["text"]) == 0:
                run.gone_misses += 1
                if run.gone_misses >= 2:        # two readings in a row without it: it is really gone
                    self._advance(sess, "text", text_map=text_map)
                    return
                run.activity = True             # read again soon to make sure
            else:
                run.gone_misses = 0
        if moved >= MOVE_MIN:
            # The page scrolled or the window moved: the marks follow it, and that change is not the step being done.
            run.baseline, run.triggered_at, run.hit = settled, None, False
        elif verify:
            self._advance(sess, "screen", settled, text_map)
            return
        self._publish()

    @staticmethod
    def _hit(step: dict[str, Any], baseline: bytes, current: bytes) -> bool:
        """Whether the picture changed, since the step appeared, the way doing the step would change it: enough
        around one of the marks, or a good part of the screen anywhere - but not just a notification popping up in
        a corner."""
        until = step["until"]
        if until.get("region"):
            return changed(baseline, current, until["region"]) >= until.get("threshold", REGION_CHANGE)
        total, box = spread(baseline, current)
        if box is None:
            return False
        windows: list[list[float]] = []
        for mark in step["marks"]:
            if mark["kind"] not in (*ACTIONS, "box") or mark["x"] is None:
                continue
            if mark["kind"] == "box" and mark["box"]:
                b = mark["box"]
                windows.append([b[0] - 0.04, b[1] - 0.04, b[2] + 0.08, b[3] + 0.08])
            else:
                windows.append([mark["x"] - 0.12, mark["y"] - 0.14, 0.24, 0.28])
            if mark["to"]:
                windows.append([mark["to"][0] - 0.12, mark["to"][1] - 0.14, 0.24, 0.28])
        if total >= CHANGE_MIN:
            for x, y, w, h in windows:
                x0, y0, x1, y1 = max(0.0, x), max(0.0, y), min(1.0, x + w), min(1.0, y + h)
                if x1 > x0 and y1 > y0 and changed(baseline, current, [x0, y0, x1 - x0, y1 - y0]) >= CHANGE_LOCAL:
                    return True
        aside = box[0] >= CORNER and (box[1] >= CORNER or box[1] + box[3] <= 1 - CORNER)
        return total >= until.get("threshold", CHANGE_FAR if windows else CHANGE_PLAIN) and not aside

    def _tick(self) -> bool:
        """One look at the running session. False when there is no session left to run."""
        with self._cond:
            sess = self._session
            if sess is None:
                return False
            now = self.clock()
            if sess.status == "stopped":
                if now - sess.ended_at >= LINGER_S:
                    self._session = None
                    self._publish()
                    return False
                return True
            if not sess.waiters and now - sess.touched >= self._config()["idle_minutes"] * 60:
                self._end(sess, "idle", "")
                return True
            if sess.status != "active" or sess.run is None:
                return True
            step, run = sess.steps[sess.index], sess.run
            mode = step["_mode"]
            searching = any(m["state"] == "searching" for m in step["marks"])
            frame = self.frames.latest()
            if frame is not None and frame[0] != run.frame_seq:
                if run.prev is not None:
                    motion = changed(run.prev, frame[1])
                    if motion > 0:
                        run.activity = True
                    if motion >= MOTION_QUIET:
                        run.quiet_since = None
                        run.dirty = True
                    elif run.quiet_since is None:
                        run.quiet_since = now
                run.prev, run.frame_seq = frame[1], frame[0]
                run.hit = mode == "change" and run.baseline is not None and not searching and self._hit(step, run.baseline, frame[1])
            live = frame is not None and now - frame[2] < 3.0
            quiet = now - run.quiet_since if run.quiet_since is not None else 0.0
            age = now - run.shown_at
            wants_text = self._wants_text(step)
            if not run.armed:
                if not live:
                    if age >= 2.5:
                        run.armed = run.blind = True
                elif age >= ARM_S and quiet >= QUIET_ARM_S:
                    run.armed, run.baseline = True, frame[1]
                elif age >= AMBIENT_S:
                    run.armed = run.ambient = True
                if not run.armed:
                    return True
                self._publish()
            read = wants_text and not run.read_armed
            if mode == "after" and age >= step["until"]["seconds"]:
                self._advance(sess, "time")
                return True
            if mode == "change" and live and not run.verify and not searching and run.baseline is not None:
                if run.hit and run.triggered_at is None:
                    run.triggered_at = now
                if run.triggered_at is not None and (quiet >= SETTLE_S or now - run.triggered_at >= MAX_SETTLE_S):
                    if not run.hit:                   # it went back to how it was (something flashed)
                        run.triggered_at = None
                    elif any(m["_anchor"] and m["x"] is not None for m in step["marks"]) and self.reader.available():
                        run.verify = read = True
                    else:
                        self._advance(sess, "screen", frame[1])
                        return True
            if wants_text and not read:
                since = now - run.read_at
                if mode in ("typed", "text", "gone") or searching:       # waiting for a text: read when anything moves
                    read = since >= READ_EVERY_S and (run.activity or not live or since >= 4.0)
                elif run.triggered_at is None:                           # following a placed text: read after it moved
                    read = run.dirty and since >= READ_EVERY_S and (quiet >= 0.25 or since >= 2.5)
            if not run.stalled and age >= step["timeout_s"]:
                run.stalled = True
                self._emit("stalled", step=sess.index + 1, seconds=int(age))
            if not run.missing_told and age >= MISSING_S and searching:
                run.missing_told = True
                missing = [m["find"] or m["_to_anchor"]["text"] for m in step["marks"] if m["state"] == "searching"]
                self._emit("anchor_missing", step=sess.index + 1, texts=missing, why=self.text_error or "not on screen")
            if not read:
                return True
            run.read_at, run.read_armed, run.activity, run.dirty = now, True, False, False
        text_map = self._read_text()
        with self._cond:
            if self._session is sess and sess.status == "active" and sess.run is run:
                self._after_read(sess, text_map)
        return True


# ---------------------------------------------------------------------------------------------- the page an assistant reads

def brief(base: str, copilot: Copilot, capture: dict[str, Any]) -> str:
    """What ``GET /copilot`` answers: what this is, where things stand and how to drive it, short enough to read whole."""
    size = capture.get("input") or {}
    picture = f"{size.get('width')}x{size.get('height')}" if size.get("width") else "size unknown until it is live"
    now = copilot.status()
    session = "no session" if now["session"] == "none" else \
        f"session {now['session']}, step {now.get('step')}/{now.get('of')}" + (f": {now['say']}" if now.get("say") else "")
    off = "" if copilot.settings["copilot"]["enabled"] else "\nCOPILOT MODE IS TURNED OFF: ask the person to turn it on with the Copilot button in Baird's top bar.\n"
    return f"""# Baird copilot: guide the person live on the other computer's screen
{off}
Baird shows the screen of the computer the person is working on. You draw on top of that live picture: a red dot
where to click, boxes, arrows, the text to type, the keys to press, with one short instruction per step. The person
does the steps there; you cannot click or type for them. Baird moves to the next step by itself (it watches the
screen), so you are only needed when a plan ends or the person gets stuck.

Now: capture {capture.get('state', 'unknown')} ({picture}), {session}.

## The loop: one call per batch
1. Look    GET  {base}/copilot/look.jpg            the screen as a JPEG (?w=1024 by default, ?region=x,y,w,h zooms)
           GET  {base}/copilot/text                every text on screen as "x,y text" lines, no picture (?q=Guardar finds one)
2. Guide   POST {base}/copilot/show                {{"agent":"<your name>","goal":"...","message":"...","steps":[...],"wait_s":120}}
3. Wait    POST {base}/copilot/wait                {{"wait_s":120}}   when you have nothing new to show
4. End     POST {base}/copilot/stop                {{"message":"Listo"}}
show and wait answer when the plan is finished, the person asks for help or writes to you (events help / say), a
text you pointed at is not on screen (anchor_missing), a step takes too long (stalled) or the session ended
(stopped). The answer has "events", where the session is, "next" (what to do) and, when eyes are needed, "look_url":
a fresh picture to GET. "still_waiting": true means nothing happened yet: call wait again. "unanswered" is what the
person said or asked that you have not answered yet (answer with a message; it is there until you do).

## Steps
{{"say":"Abre el menú Archivo","click":"Archivo"}}                 click on that text: Baird finds it on screen (OCR)
{{"say":"Pulsa aquí","click":[412,236]}}                            or on coordinates
{{"say":"Escribe el nombre","type":"informe-final","at":{{"find":"Nombre de archivo","side":"right"}}}}
{{"say":"Guarda","keys":"Ctrl+S"}}    {{"say":"Arrástralo","arrow":["informe.txt","Papelera"]}}
{{"say":"Revisa este panel","box":[40,120,380,500]}}    {{"say":"Espera a que acabe","until":{{"text":"Completado"}}}}
Other marks: dbl, right (double and right click), note (a label: {{"note":"No toques esto","at":[900,80]}}).
Several marks in one step: "marks":[{{"click":"Aceptar"}},{{"box":"Nombre de la rama"}}].
"say" is the subtitle under the picture (one short sentence); "hint" is a second line for the why or the detail.
"message" (in show, with or without steps) is you talking to the person: it goes to the conversation beside the
picture, where they can also write to you (event say). Use it to explain, to answer and to say what comes next.
A place is "text on screen", [x,y], [x,y,w,h] or {{"find":"Guardar","nth":2,"side":"right|left|above|below","dx":40}}.
Coordinates are pixels of the last picture you got from look (any size); before any look, pixels of the full picture.
"until" says when a step is over: "auto" (default: the screen changes where you pointed, or the text to type shows
up), "manual" (the person presses Hecho), {{"text":"..."}} appears, {{"gone":"..."}}, {{"after":3}}. The person can always
press Hecho or Atrás. "replace":false adds steps after the pending ones instead of replacing them.

## Keep it fast and cheap
- Point at texts instead of coordinates whenever the target has a label: no look needed, exact, and it follows scrolling.
- Send the whole plan at once. Later steps may point at texts that are not on screen yet: each mark appears when its text does.
- Look only when you need eyes: the first screen, after "done", on help. /copilot/text is cheaper than a picture when
  you only need labels; zoom with ?region= instead of asking for a bigger picture.
- "say" in the person's language, one short imperative sentence. Never put passwords or secrets in a mark: tell the
  person to type them.
- Stay with the person: while the session is active keep calling wait. After "stopped", stop.

MCP (Baird's connector) has the same as tools: copilot_look, copilot_show, copilot_wait, copilot_find,
copilot_status, copilot_stop. They return the picture itself.
Bodies are JSON (UTF-8). curl: curl -s {base}/copilot/show -d '{{"steps":[{{"say":"Pulsa Guardar","click":"Guardar"}}],"wait_s":120}}'
PowerShell: $b = @{{steps=@(@{{say='Pulsa Guardar';click='Guardar'}});wait_s=120}} | ConvertTo-Json -Depth 8;
  Invoke-RestMethod {base}/copilot/show -Method Post -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($b))
"""
