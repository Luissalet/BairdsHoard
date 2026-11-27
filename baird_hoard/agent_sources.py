"""Use the source controller before opening a live image from any agent tool."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from secrets import choice, token_urlsafe
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .capture_approval import APPROVAL_KEYS, ConfirmCapture
from .errors import BairdError
from .monitor_source import selected_monitor

SourceMode = Literal["hdmi", "monitor", "overlay"]


class SourceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SourceMode
    monitor: str | None = Field(None, min_length=1, max_length=512,
                                description="Stable display ID from source_list for monitor or overlay.")


class SourceBoundArgs(BaseModel):
    source: SourceArgs | None = Field(None, description="Expected capture source. Omit to use the current selection.")


class SourceUndoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    undo_token: str = Field(..., min_length=32, max_length=32)


def _focus(svc, expected: SourceArgs | None, live: bool) -> tuple:
    state = svc.sources.status()
    mode, monitor = state["mode"], state["monitor"]
    if expected is not None:
        if expected.mode == "hdmi" and expected.monitor is not None:
            raise BairdError("invalid", "HDMI does not use a monitor ID.")
        if expected.mode != mode or (expected.monitor is not None and expected.monitor != monitor):
            raise BairdError("source_mismatch", "The requested source is not selected.",
                             "Use source_list to check the selection.", status=409)
    if live and mode != "hdmi":
        if state["blocked"]:
            raise BairdError(state["blocked"], "The local screen is blocked.",
                             "Ask the person to check the source in Baird.",
                             status=503 if state["blocked"] == "policy_unavailable" else 409)
        if not state["active"]:
            raise BairdError("source_off", "The local screen is off.",
                             "Ask the person to turn it on in Baird.", status=403)
    return mode, monitor, state.get("revision")


@dataclass(frozen=True)
class SourcePermit:
    services: Any
    expected: SourceArgs | None
    focus: tuple

    def check(self) -> None:
        if _focus(self.services, self.expected, True) != self.focus:
            raise BairdError("source_changed", "The source changed during the request. Look again.", status=409)

    @contextmanager
    def publish(self):
        with self.services.sources.lock:
            self.check()
            yield


_live_permit: ContextVar[SourcePermit | None] = ContextVar("live_source_permit", default=None)


def capture_publication(svc):
    """Take the call's permit before rendering. Hold its lock only while publishing."""
    permit = _live_permit.get()
    if permit is None or permit.services is not svc:
        permit = SourcePermit(svc, None, _focus(svc, None, True))
    return permit.publish()


def call_on_source(svc, expected: SourceArgs | None, run: Callable[[], Any], *, live: bool = True) -> Any:
    """Bind a call to its source and check that fresh results still belong to it."""
    original = _focus(svc, expected, live)
    if not live:
        return run()
    permit = SourcePermit(svc, expected, original)
    token = _live_permit.set(permit)
    try:
        result = run()
        permit.check()
        return result
    finally:
        _live_permit.reset(token)


CONTROL_LIFETIME = 600


@dataclass(frozen=True)
class CaptureControl:
    session: str
    revision: int
    mode: SourceMode
    monitor: str
    active: bool
    expires: float
    key: int


def activation_required() -> BairdError:
    return BairdError("source_activation_required", "Use the local screen switch in Baird's Settings.", status=403)


class ProfileSources:
    """Source controller for the fixed HDMI or monitor profiles.

    The source bar's SourceControls can supply the same status/select/undo
    operations when the live selector is composed with the monitor backend.
    """

    def __init__(self, services, confirm_capture: ConfirmCapture):
        self._services = services
        self.lock = threading.RLock()
        self._revision = 0
        self._undo: tuple[str, int, dict[str, str] | None] | None = None
        self._controls: dict[str, CaptureControl] = {}
        self._confirm = confirm_capture
        self._pending_capture: tuple[str, str, threading.Event] | None = None

    def status(self, *, list_displays: bool = False) -> dict[str, Any]:
        svc = self._services
        with self.lock:
            settings = svc.settings.get()
            local = svc.monitor_source is not None
            selection = settings["monitor"] if local else None
            active = settings["capture_enabled"] and (not local or selection is not None)
            blocked = None
            if local and active:
                engine = svc.engine.snapshot()
                if engine["state"] == "reselect":
                    blocked = "monitor_reselect"
                elif engine["state"] == "privacy_blocked":
                    blocked = engine["privacy"]
            monitors = []
            if local and list_displays:
                monitors = [{**display, "left": display["x"], "top": display["y"]}
                            for display in svc.monitor_source.list_monitors()]
            mode = "monitor" if local else "hdmi"
            return {"mode": mode, "modes": [mode], "monitor": selection["id"] if selection else None,
                    "active": bool(active and not blocked), "blocked": blocked, "monitors": monitors,
                    "revision": self._revision}

    def settings_applied(self, before: dict, after: dict) -> None:
        if self._services.monitor_source is not None and any(
            before[key] != after[key] for key in ("capture_enabled", "monitor", "monitor_microphone")
        ):
            self.advance_revision()

    def advance_revision(self) -> None:
        self._revision += 1
        self._controls.clear()
        if self._pending_capture is not None:
            self._pending_capture[2].set()

    def capture_control(self, session: str) -> dict[str, Any]:
        """Issue a single-use control for the display shown to the person."""
        with self.lock:
            state = self.status(list_displays=True)
            selection = self._services.settings["monitor"]
            armed = self._services.settings["capture_enabled"]
            display = next((item for item in state["monitors"] if item["selection"] == selection), None)
            if display is None:
                return {"active": False, "display": None, "ticket": None}
            now = time.monotonic()
            self._controls = {key: grant for key, grant in self._controls.items() if grant.expires > now}
            if len(self._controls) >= 64:
                self._controls.pop(next(iter(self._controls)))
            ticket = token_urlsafe(32)
            key = choice(APPROVAL_KEYS)
            self._controls[ticket] = CaptureControl(session, self._revision, state["mode"], state["monitor"],
                                                    armed, now + CONTROL_LIFETIME, key)
            shown = {key: display[key] for key in ("name", "x", "y", "width", "height")}
            return {"active": armed, "display": shown, "ticket": ticket, "approval_key": key}

    def _validate_control(self, grant: CaptureControl, enabled: bool) -> None:
        svc = self._services
        state = self.status()
        if (grant.expires <= time.monotonic() or grant.revision != self._revision
                or grant.mode != state["mode"] or (state["blocked"] and enabled)
                or grant.monitor != state["monitor"] or grant.active != svc.settings["capture_enabled"]
                or grant.active == enabled):
            raise activation_required()
        if enabled:
            selected_monitor(svc.settings["monitor"], svc.monitor_source.inventory())

    def set_capture(self, *, session: str, ticket: str, enabled: bool) -> None:
        with self.lock:
            grant = self._controls.get(ticket)
            if grant is None or grant.session != session:
                raise activation_required()
            del self._controls[ticket]
            self._validate_control(grant, enabled)
            if not enabled:
                self._services.apply_settings({"capture_enabled": False})
                return
            if self._pending_capture is not None:
                raise BairdError("source_approval_pending", "A local capture confirmation is already pending.", status=409)
            cancelled = threading.Event()
            self._pending_capture = (session, ticket, cancelled)

        try:
            try:
                approved = self._confirm(grant.key, cancelled)
            except OSError:
                approved = False
            with self.lock:
                if approved is not True or cancelled.is_set():
                    raise BairdError("source_approval_required", "Confirm the selected screen with the Windows shortcut shown in Baird.", status=403)
                self._validate_control(grant, True)
                self._services.apply_settings({"capture_enabled": True})
        finally:
            with self.lock:
                self._pending_capture = None

    def cancel_capture_control(self, *, session: str, ticket: str) -> None:
        with self.lock:
            grant = self._controls.get(ticket)
            if grant is not None and grant.session == session:
                del self._controls[ticket]
            pending = self._pending_capture
            if pending is not None and pending[:2] == (session, ticket):
                pending[2].set()

    def _check_recording(self) -> None:
        if self._services.library.recorder.status()["state"] in ("recording", "paused", "finishing"):
            raise BairdError("recording_in_progress", "Finish the recording before changing source.", status=409)

    def select(self, mode: SourceMode, monitor: str | None = None) -> dict[str, Any]:
        svc = self._services
        with self.lock:
            state = self.status()
            if mode != state["mode"]:
                raise BairdError("capture_unavailable", "This source is unavailable in the current profile.",
                                 "Check source_list for the available modes.", status=503)
            if mode == "hdmi":
                if monitor is not None:
                    raise BairdError("invalid", "HDMI does not use a monitor ID.")
                return state
            earlier = svc.settings["monitor"]
            selection = None
            if monitor is not None:
                display = next((item for item in svc.monitor_source.list_monitors() if item["id"] == monitor), None)
                if display is None:
                    raise BairdError("monitor_reselect", "Choose the display again.", status=409)
                selection = display["selection"]
            if selection == earlier and not state["blocked"]:
                return state
            self._check_recording()
            svc.apply_settings({"monitor": selection, "capture_enabled": False})
            token = uuid4().hex
            self._undo = (token, self._revision, earlier)
            return {**self.status(), "undo_token": token}

    def undo(self, token: str) -> dict[str, Any]:
        with self.lock:
            if self._undo is None or self._undo[:2] != (token, self._revision):
                raise BairdError("conflict", "That source change can no longer be undone.")
            self._check_recording()
            self._services.apply_settings({"monitor": self._undo[2], "capture_enabled": False})
            self._undo = None
            return self.status()
