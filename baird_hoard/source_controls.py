"""The selected source, explicit activation, and the monitor backend boundary."""

from __future__ import annotations

import logging
from _thread import RLock
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, ContextManager, Literal
from uuid import uuid4

from .capture import CaptureEngine
from .capture_source import CaptureSource
from .errors import BairdError
from .screen_privacy import ScreenGate, ScreenPolicy, ScreenPolicySubscription

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
    layout: str | None = None


@dataclass(frozen=True)
class ScreenBackend:
    displays: Callable[[], Sequence[Display]]
    producer: Callable[[Display], CaptureSource]
    visible_apps: Callable[[str], Iterable[str] | None]
    read_scoped: Callable[[Display], ScreenPolicy] | None = None
    permit: Callable[[Display], ContextManager[bool]] | None = None
    prepare_policy: Callable[[Display], None] | None = None


@dataclass(frozen=True)
class Selection:
    mode: SourceMode = "hdmi"
    monitor: str | None = None
    layout: str | None = None


class SourceControls:
    def __init__(self, engine: CaptureEngine, recording_state: Callable[[], str],
                 backend: ScreenBackend | None = None, read_policy: Callable[[], ScreenPolicy] | None = None,
                 subscribe_policy: ScreenPolicySubscription | None = None, *,
                 initial: Selection | None = None,
                 prepare_change: Callable[[Selection], None] | None = None,
                 overlay_enabled: Callable[[], bool] | None = None, lock: RLock | None = None):
        self._engine = engine
        self._recording_state = recording_state
        self._backend = backend
        self._read_policy = read_policy
        self._subscribe_policy = subscribe_policy
        self._unsubscribe_policy: Callable[[], None] | None = None
        self.lock = lock if lock is not None else RLock()
        self._overlay_enabled = overlay_enabled or (lambda: True)
        self._selection = initial or Selection()
        self._prepare_change = prepare_change
        self._revision = 0
        if self._selection.mode != "hdmi":
            self._engine.select_local_source()
        self._gate: ScreenGate | None = None
        self._undo: tuple[str, Selection] | None = None

    @property
    def selection(self) -> Selection:
        return self._selection

    def status(self, *, list_displays: bool = False) -> dict[str, Any]:
        with self.lock:
            choice = self._selection
            monitors = self._displays() if list_displays or choice.mode != "hdmi" else ()
            block = self._gate.blocked if self._gate else None
            if block == "policy_unavailable" and self._backend.read_scoped is not None:
                block = self._gate.refresh_cutoff()
            if choice.monitor and not any(display.id == choice.monitor and display.layout == choice.layout for display in monitors):
                block = block or "missing_display"
            return {
                "mode": choice.mode,
                "modes": ["hdmi", "monitor", "overlay"] if self._overlay_enabled() else ["hdmi", "monitor"],
                "monitor": choice.monitor,
                "active": self._engine.source_enabled() and block is None,
                "blocked": block,
                "revision": self._revision,
                "monitors": [asdict(display) for display in monitors],
            }

    def select(self, mode: SourceMode, monitor: str | None = None) -> dict[str, Any]:
        with self.lock:
            self._require_mode(mode)
            previous = self._selection
            display = self._find(monitor) if mode != "hdmi" else None
            if mode != "hdmi" and monitor and display is None:
                raise BairdError("missing_display", "Choose the display again.", status=409)
            choice = Selection(mode, display.id if display else None, display.layout if display else None)
            if choice == previous and not (self._gate and self._gate.blocked):
                return self.status()
            self._change(choice)
            token = uuid4().hex
            self._undo = (token, previous)
            return {**self.status(), "undo_token": token}

    def undo(self, token: str) -> dict[str, Any]:
        with self.lock:
            if self._undo is None or token != self._undo[0]:
                raise BairdError("conflict", "That source change can no longer be undone.")
            self._require_mode(self._undo[1].mode)
            self._change(self._undo[1])
            self._undo = None
            return self.status()

    def set_active(self, active: bool) -> dict[str, Any]:
        with self.lock:
            choice = self._selection
            if choice.mode == "hdmi":
                raise BairdError("invalid", "The capture card uses the capture setting.")
            if not active:
                try:
                    self._engine.select_local_source()
                finally:
                    self._close_policy_subscription()
                    self._gate = None
                    self._revision += 1
                return self.status()
            if self._engine.source_enabled():
                return self.status()
            self._check_recording()
            if self._backend is None:
                raise BairdError("capture_unavailable", "This source is unavailable.", status=503)
            if self._read_policy is None and self._backend.read_scoped is None:
                raise BairdError("policy_unavailable", "Screen privacy settings are unavailable.", status=503)
            display = self._find(choice.monitor)
            if display is None or display.layout != choice.layout:
                raise BairdError("missing_display", "Choose the display again.", status=409)
            self._close_policy_subscription()
            scoped = self._backend.read_scoped
            read_policy = (lambda: scoped(display)) if scoped is not None else self._read_policy
            permit = self._backend.permit
            gate = ScreenGate(read_policy, display.id,
                              lambda find_id: self._display_present(find_id, display.layout), self._backend.visible_apps,
                              (lambda: permit(display)) if permit is not None else None)
            self._gate = gate
            if self._subscribe_policy is not None:
                try:
                    unsubscribe = self._subscribe_policy(
                        display.id, lambda reason: self._engine.revoke_local_source(gate, reason))
                    if not callable(unsubscribe):
                        raise ValueError("The screen policy subscription returned no unsubscribe callback.")
                    self._unsubscribe_policy = unsubscribe
                except Exception:
                    log.debug("Screen policy subscription failed", exc_info=True)
                    gate.block(None)
            try:
                if gate.blocked is None and self._backend.prepare_policy is not None:
                    try:
                        self._backend.prepare_policy(display)
                    except Exception:
                        log.debug("Screen capture policy preparation failed", exc_info=True)
                        gate.block(None)
                if gate.check() is None:
                    with gate.publication() as allowed:
                        if allowed:
                            self._engine.select_local_source(self._backend.producer(display), gate=gate)
                            self._revision += 1
                if gate.blocked is not None:
                    raise BairdError(gate.blocked, "Screen capture is blocked by the current privacy settings.", status=409)
                return self.status()
            except Exception:
                self._close_policy_subscription()
                raise

    def close(self) -> None:
        with self.lock:
            if self._gate is not None:
                self._engine.revoke_local_source(self._gate, None, confirm=False)
            self._close_policy_subscription()

    def _close_policy_subscription(self) -> None:
        unsubscribe, self._unsubscribe_policy = self._unsubscribe_policy, None
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:
                log.debug("Could not close screen policy subscription", exc_info=True)

    def _check_recording(self) -> None:
        if self._recording_state() in ("recording", "paused", "finishing"):
            raise BairdError("recording_in_progress", "Finish the recording before changing source.", status=409)

    def _require_mode(self, mode: SourceMode) -> None:
        if mode == "overlay" and not self._overlay_enabled():
            raise BairdError("capture_unavailable", "Enable the overlay in Baird Settings first.", status=503)

    def _change(self, choice: Selection) -> None:
        self._check_recording()
        if self._prepare_change is not None:
            self._prepare_change(choice)
        self._selection = choice
        self._revision += 1
        self._gate = None
        try:
            if choice.mode == "hdmi":
                self._engine.restore_default_source()
            else:
                self._engine.select_local_source()
        finally:
            self._close_policy_subscription()

    def _displays(self) -> Sequence[Display]:
        if self._backend is not None:
            try:
                return self._backend.displays()
            except OSError:
                log.debug("Display enumeration failed", exc_info=True)
        return ()

    def _find(self, display_id: str | None) -> Display | None:
        return next((display for display in self._displays() if display.id == display_id), None)

    def _display_present(self, display_id: str, layout: str | None) -> bool:
        display = self._find(display_id)
        return display is not None and display.layout == layout
