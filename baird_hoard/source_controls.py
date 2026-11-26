"""The selected source, explicit activation, and the monitor backend boundary."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import uuid4

from .capture import CaptureEngine
from .capture_source import CaptureSource
from .errors import BairdError
from .screen_privacy import ScreenGate, ScreenPolicy

log = logging.getLogger("baird.sources")

SourceMode = Literal["hdmi", "monitor", "overlay"]


@dataclass(frozen=True)
class Display:
    id: str
    name: str
    width: int
    height: int
    left: int
    top: int


@dataclass(frozen=True)
class ScreenBackend:
    displays: Callable[[], Sequence[Display]]
    producer: Callable[[Display], CaptureSource]
    visible_apps: Callable[[str], Iterable[str] | None]


@dataclass(frozen=True)
class Selection:
    mode: SourceMode = "hdmi"
    monitor: str | None = None


class SourceControls:
    def __init__(self, engine: CaptureEngine, recording_state: Callable[[], str],
                 backend: ScreenBackend | None = None, read_policy: Callable[[], ScreenPolicy] | None = None):
        self._engine = engine
        self._recording_state = recording_state
        self._backend = backend
        self._read_policy = read_policy
        self._lock = threading.RLock()
        self._selection = Selection()
        self._gate: ScreenGate | None = None
        self._undo: tuple[str, Selection] | None = None

    def status(self, *, list_displays: bool = False) -> dict[str, Any]:
        with self._lock:
            choice = self._selection
            monitors = self._displays() if list_displays or choice.mode != "hdmi" else ()
            return {
                "mode": choice.mode,
                "modes": ["hdmi", "monitor", "overlay"],
                "monitor": choice.monitor,
                "active": self._engine.source_enabled(),
                "blocked": self._gate.blocked if self._gate else None,
                "monitors": [asdict(display) for display in monitors],
            }

    def select(self, mode: SourceMode, monitor: str | None = None) -> dict[str, Any]:
        with self._lock:
            previous = self._selection
            choice = Selection(mode, monitor if mode != "hdmi" else None)
            if choice == previous:
                return self.status()
            if choice.monitor and not self._find(choice.monitor):
                raise BairdError("missing_display", "Choose the display again.", status=409)
            self._change(choice)
            token = uuid4().hex
            self._undo = (token, previous)
            return {**self.status(), "undo_token": token}

    def undo(self, token: str) -> dict[str, Any]:
        with self._lock:
            if self._undo is None or token != self._undo[0]:
                raise BairdError("conflict", "That source change can no longer be undone.")
            self._change(self._undo[1])
            self._undo = None
            return self.status()

    def set_active(self, active: bool) -> dict[str, Any]:
        with self._lock:
            choice = self._selection
            if choice.mode == "hdmi":
                raise BairdError("invalid", "The capture card uses the capture setting.")
            if not active:
                self._engine.select_local_source()
                self._gate = None
                return self.status()
            if self._engine.source_enabled():
                return self.status()
            self._check_recording()
            if self._backend is None:
                raise BairdError("capture_unavailable", "This source is unavailable.", status=503)
            if self._read_policy is None:
                raise BairdError("policy_unavailable", "Screen privacy settings are unavailable.", status=503)
            display = self._find(choice.monitor)
            if display is None:
                raise BairdError("missing_display", "Choose the display again.", status=409)
            self._gate = ScreenGate(self._read_policy, display.id, self._display_present, self._backend.visible_apps)
            reason = self._gate.check(fresh=True)
            if reason:
                raise BairdError(reason, "Screen capture is blocked by the current privacy settings.", status=409)
            self._engine.select_local_source(self._backend.producer(display), check=self._gate.check)
            return self.status()

    def _check_recording(self) -> None:
        if self._recording_state() in ("recording", "paused", "finishing"):
            raise BairdError("recording_in_progress", "Finish the recording before changing source.", status=409)

    def _change(self, choice: Selection) -> None:
        self._check_recording()
        self._selection = choice
        self._gate = None
        if choice.mode == "hdmi":
            self._engine.restore_default_source()
        else:
            self._engine.select_local_source()

    def _displays(self) -> Sequence[Display]:
        if self._backend is not None:
            try:
                return self._backend.displays()
            except OSError:
                log.debug("Display enumeration failed", exc_info=True)
        return ()

    def _find(self, display_id: str | None) -> Display | None:
        return next((display for display in self._displays() if display.id == display_id), None)

    def _display_present(self, display_id: str) -> bool:
        return self._find(display_id) is not None
