"""Render the active copilot step on the captured display, without changing the session."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("baird.overlay")

HIDE_SHORTCUT = "Ctrl+Alt+Shift+F10"
LANGUAGES = {
    "es": {"dbl": "Doble clic", "right": "Clic derecho", "you": "Tú", "help": "Ayuda pedida",
           "manual": "Pulsa Hecho en Baird.", "searching": "Buscando", "lost": "Ya no veo",
           "change": "Avanza solo cuando lo hagas.", "typed": "Avanza solo cuando lo escribas.",
           "text": "Avanza cuando aparezca", "gone": "Avanza cuando desaparezca",
           "after": "Avanza solo en un momento."},
    "en": {"dbl": "Double-click", "right": "Right-click", "you": "You", "help": "Help requested",
           "manual": "Select Done in Baird.", "searching": "Looking for", "lost": "Can no longer see",
           "change": "Advances when you make the change.", "typed": "Advances when you type it.",
           "text": "Advances when this appears", "gone": "Advances when this disappears",
           "after": "Advances in a moment."},
}
Point = tuple[int, int]
Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class OverlayDisplay:
    id: str
    left: int
    top: int
    width: int
    height: int

    @property
    def bounds(self) -> Rect:
        return self.left, self.top, self.left + self.width, self.top + self.height

    def point(self, x: float, y: float, *, edge: bool = False) -> Point:
        limit = 0 if edge else 1
        return (max(0, min(self.width - limit, round(x * self.width))),
                max(0, min(self.height - limit, round(y * self.height))))


def selected_overlay_display(source: dict[str, Any]) -> OverlayDisplay | None:
    """Resolve the actual selected source using physical pixels from the capture backend."""
    if source["mode"] != "overlay" or not source["active"] or source["blocked"]:
        return None
    matches = [item for item in source["monitors"] if item["id"] == source["monitor"]]
    if len(matches) != 1:
        return None
    item = matches[0]
    if item["width"] <= 0 or item["height"] <= 0:
        return None
    return OverlayDisplay(item["id"], item["left"], item["top"], item["width"], item["height"])


@dataclass(frozen=True)
class OverlayMark:
    kind: str
    at: Point | None
    to: Point | None
    box: Rect | None
    text: str
    number: int
    lost: bool


@dataclass(frozen=True)
class OverlayFrame:
    display: OverlayDisplay
    dpi: int
    marks: tuple[OverlayMark, ...]
    bar_at_top: bool
    heading: str
    instruction: str
    hint: str
    keys: tuple[str, ...]
    mode: str
    conversation: tuple[str, ...]

    def pixels(self, dip: int) -> int:
        return round(dip * self.dpi / 96)


def render_frame(display: OverlayDisplay, dpi: int, session: dict[str, Any], language: str) -> OverlayFrame:
    step = session["step"]
    words = LANGUAGES[language]
    raw = [mark for mark in step["marks"] if mark["state"] != "searching"]
    numbered = [mark for mark in raw if mark["kind"] in ("click", "dbl", "right", "arrow")]
    numbers = {mark["id"]: i for i, mark in enumerate(numbered, start=1)} if len(numbered) > 1 else {}
    marks = []
    for mark in raw:
        box = None
        if mark["box"]:
            x, y, width, height = mark["box"]
            box = (*display.point(x, y, edge=True), *display.point(x + width, y + height, edge=True))
        at = display.point(mark["x"], mark["y"]) if mark["x"] is not None else None
        if at is None and box is None:
            continue
        to = display.point(*mark["to"]) if mark.get("to") else None
        marks.append(OverlayMark(mark["kind"], at, to, box, mark["text"] or words.get(mark["kind"], ""),
                                 numbers.get(mark["id"], 0), mark["state"] == "lost"))

    types = [mark["text"] for mark in step["marks"] if mark["kind"] == "type" and mark["text"]]
    searching = [mark["find"] for mark in step["marks"] if mark["state"] == "searching" and mark["find"]]
    lost = [mark["find"] for mark in raw if mark["state"] == "lost" and mark["find"]]
    keys = [" + ".join(combo) for combo in step["keys"]]
    mode = words.get(step["mode"], "") if step["auto"] else words["manual"]
    if step["auto"] and step["mode"] in ("text", "gone"):
        mode = f'{mode} «{step["until_text"]}».'
    if lost:
        mode = f'{words["lost"]} {", ".join(lost)}.'
    if searching:
        mode = f'{words["searching"]} {", ".join(searching)}…'
    if session["asked_help"]:
        mode = words["help"]
    agent = session["agent"] or "Copilot"
    conversation = [f'{words["you"] if entry["who"] == "you" else agent} · {entry["text"]}'
                    for entry in session["log"] if entry["who"] in ("you", "agent")]
    low = any((m["y"] is not None and m["y"] > .74) or (m["box"] and m["box"][1] + m["box"][3] > .76) for m in raw)
    high = any((m["y"] is not None and m["y"] < .26) or (m["box"] and m["box"][1] < .24) for m in raw)
    heading = f'{step["n"]}/{session["count"]}  ·  {agent}'
    return OverlayFrame(display, dpi, tuple(marks), low and not high, heading, step["say"], step["hint"],
                        tuple(keys + types), mode, tuple(conversation[-2:]))


class MemoryOverlay:
    """Windowless presenter. The frame is a drawing buffer in monitor-local physical pixels."""

    def __init__(self, dpi: int = 96):
        self.dpi = dpi
        self.frame: OverlayFrame | None = None
        self.exclude_capture = True
        self.shortcut = False

    def present(self, display: OverlayDisplay, session: dict[str, Any], language: str, exclude_capture: bool) -> None:
        self.frame = render_frame(display, self.dpi, session, language)
        self.exclude_capture = exclude_capture

    def pump(self) -> bool:
        return False

    def close(self) -> None:
        self.frame = None


class CopilotOverlay:
    """Keep native presentation on one owner thread and coalesce copilot updates."""

    def __init__(self, target: Callable[[], OverlayDisplay | None], changed: Callable[[], None], *,
                 test: bool = False, presenter: Callable[..., Any] | None = None):
        self._target = target
        self._changed = changed
        self._presenter = presenter or (MemoryOverlay if test else self._windows)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._running = False
        self._session: dict[str, Any] | None = None
        self._hidden = False
        self._include = False
        self._language = "es"
        self._status: dict[str, Any] = {"available": False, "visible": False, "shortcut": False, "error": ""}

    @staticmethod
    def _windows():
        from .overlay_windows import Win32Overlay
        return Win32Overlay()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "hidden": self._hidden, "included": self._include, "combo": HIDE_SHORTCUT}

    def update(self, session: dict[str, Any] | None) -> None:
        with self._lock:
            if (session or {}).get("id") != (self._session or {}).get("id"):
                self._hidden = self._include = False
            if session != self._session:
                self._session = session
                self._wake.set()
            self._start()

    def source_changed(self, running: bool) -> None:
        with self._lock:
            self._running = running
            if not running:
                self._include = False
            self._wake.set()
            self._start()

    def _start(self) -> None:
        if not self._closed and self._running and self._active(self._session) and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="baird-overlay", daemon=True)
            self._thread.start()

    @staticmethod
    def _active(session: dict[str, Any] | None) -> bool:
        return bool(session and session["status"] == "active" and session["step"])

    def control(self, action: str, language: str | None = None) -> None:
        if action not in ("hide", "show", "include", "exclude"):
            raise ValueError("Overlay action must be hide, show, include or exclude.")
        with self._lock:
            if language in LANGUAGES:
                self._language = language
            if action in ("hide", "show"):
                self._hidden = action == "hide"
            if action == "hide":
                self._include = False
            if action == "include":
                self._include = self._status["visible"] and not self._hidden
            if action == "exclude":
                self._include = False
            self._wake.set()

    def set_language(self, language: str | None) -> None:
        if language in LANGUAGES:
            with self._lock:
                self._language = language
                self._wake.set()

    def _report(self, *, available: bool, visible: bool = False, shortcut: bool = False, error: str = "") -> bool:
        status = {"available": available, "visible": visible, "shortcut": shortcut, "error": error}
        with self._lock:
            notify = status != self._status
            self._status = status
        return notify

    def _run(self) -> None:
        output = None
        last = None
        failed = None
        display = None
        next_probe = 0.0
        bound_session = None
        bound_display = None
        shortcut_changed = False
        try:
            while True:
                self._wake.clear()
                with self._lock:
                    if self._closed:
                        return
                    session, running = self._session, self._running
                    hidden, include, language = self._hidden, self._include, self._language
                active = self._active(session)
                session_id = session["id"] if session else None
                if session_id != bound_session:
                    bound_session, bound_display = session_id, None
                    next_probe = 0.0
                if not active or not running:
                    display = None
                    next_probe = 0.0
                elif time.monotonic() >= next_probe:
                    try:
                        display = self._target()
                    except (OSError, ValueError):
                        log.debug("Resolving the overlay display", exc_info=True)
                        display = None
                    next_probe = time.monotonic() + .25
                    if bound_display is None:
                        bound_display = display
                    # Selection can take a moment if the desktop is reconfiguring.
                    # Don't draw an old step after a stop that came in during that call.
                    with self._lock:
                        if self._closed or not self._running or self._session is not session:
                            self._wake.set()
                            continue
                selected = display is not None and display == bound_display
                key = (session_id, display, include)
                error = ""
                if selected and not hidden:
                    if failed == key:
                        error = "unavailable"
                    else:
                        try:
                            if output is None:
                                output = self._presenter()
                            frame = (display, session, language, include)
                            if frame != last:
                                output.present(display, session, language, not include)
                                last = frame
                            if output.pump():
                                self.control("hide")
                                hidden = shortcut_changed = True
                        except OSError:
                            log.warning("The monitor overlay could not be shown", exc_info=True)
                            failed = key
                            error = "unavailable"
                if output is not None and (not selected or hidden or error):
                    output.close()
                    output = last = None
                if hidden or not selected:
                    failed = None
                if not selected and include:
                    with self._lock:
                        self._include = False
                    shortcut_changed = True
                if self._report(available=selected, visible=output is not None,
                                shortcut=bool(output and output.shortcut), error=error) or shortcut_changed:
                    shortcut_changed = False
                    self._changed()
                self._wake.wait(.04 if output else .25 if active and running else None)
        finally:
            if output is not None:
                output.close()
            self._report(available=False)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._wake.set()
        if self._thread is not None:
            self._thread.join()
