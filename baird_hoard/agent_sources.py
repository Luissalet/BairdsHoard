"""Use the source controller before opening a live image from any agent tool."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from secrets import choice, token_urlsafe
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .capture_approval import APPROVAL_KEYS, APPROVAL_SECONDS, ConfirmCapture
from .errors import BairdError
from .monitor_source import DesktopMonitor, selected_monitor
from .source_controls import SourceControls

SourceMode = Literal["hdmi", "monitor", "overlay"]
ControlTarget = Literal["capture", "monitor_capture", "overlay_mode"]
LOCAL_FEATURES = frozenset({"monitor_capture", "overlay_mode"})


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
        if isinstance(svc.sources, SourceControls) and (state["blocked"] or not state["active"]):
            raise BairdError("no_signal", "The capture source is no longer available.", state="off")
        if state["blocked"]:
            raise BairdError(state["blocked"], "The local screen is blocked.",
                             "Ask the person to check the source in Baird.",
                             status=503 if state["blocked"] == "policy_unavailable" else 409)
        if not state["active"]:
            raise BairdError("source_off", "The local screen is off.",
                             "Ask the person to turn it on in Baird.", status=403)
    return mode, monitor, state.get("revision")


@dataclass
class SourcePermit:
    services: Any
    expected: SourceArgs | None
    focus: tuple
    committed: bool = field(init=False, default=False)

    def check(self) -> None:
        if _focus(self.services, self.expected, True) != self.focus:
            raise BairdError("source_changed", "The source changed during the request. Look again.", status=409)

    @contextmanager
    def publish(self, *, finalizes_call: bool):
        with self.services.sources.lock:
            self.check()
            yield
            if finalizes_call:
                self.committed = True


_live_permit: ContextVar[SourcePermit | None] = ContextVar("live_source_permit", default=None)


def capture_publication(svc, *, finalizes_call: bool = False):
    """Take the original permit and hold its lock only while publishing."""
    permit = _live_permit.get()
    if permit is None or permit.services is not svc:
        permit = SourcePermit(svc, None, _focus(svc, None, True))
    return permit.publish(finalizes_call=finalizes_call)


def call_on_source(svc, expected: SourceArgs | None, run: Callable[[], Any], *, live: bool = True) -> Any:
    """Bind a call to its source and check that fresh results still belong to it."""
    original = _focus(svc, expected, live)
    if not live:
        return run()
    permit = SourcePermit(svc, expected, original)
    integrated = getattr(svc.sources, "selector", None) is not None
    local = svc.engine.local_source() if integrated else None
    token = _live_permit.set(permit)
    try:
        result = run()
        if not permit.committed:
            if integrated:
                with permit.publish(finalizes_call=False), svc.engine.media_publication(local):
                    return result
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
    monitor: str | None
    active: bool
    expires: float
    target: ControlTarget


@dataclass(frozen=True)
class PendingCapture:
    session: str
    ticket: str
    key: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    registered: threading.Event = field(default_factory=threading.Event)


def activation_required() -> BairdError:
    return BairdError("source_activation_required", "Use the local screen switch in Baird's Settings.", status=403)


class ProfileSources:
    """Source controller for the fixed HDMI, monitor and overlay profiles.

    The source bar's SourceControls can supply the same status/select/undo
    operations when the live selector is composed with the monitor backend.
    """

    def __init__(self, services, confirm_capture: ConfirmCapture, *, selector: SourceControls | None = None):
        self._services = services
        self.selector = selector
        self.lock = selector.lock if selector is not None else threading.RLock()
        self._revision = 0
        self._undo: tuple[str, int, dict[str, str] | None] | None = None
        self._controls: dict[str, CaptureControl] = {}
        self._confirm = confirm_capture
        self._pending_capture: PendingCapture | None = None
        self._reserved_keys: dict[int, float] = {}

    def status(self, *, list_displays: bool = False) -> dict[str, Any]:
        svc = self._services
        with self.lock:
            features = svc.local_features()
            if self.selector is not None:
                state = self.selector.status(list_displays=list_displays)
                displays = [{**row, "x": row["left"], "y": row["top"],
                             "selection": {"id": row["id"], "layout": row["layout"]}}
                            for row in state["monitors"]]
                codes = {"private": "privacy_blocked", "excluded": "application_excluded",
                         "missing_display": "monitor_reselect"}
                return {**state, "monitors": displays, "requires_confirmation": True, "local_features": features,
                        "blocked": codes.get(state["blocked"], state["blocked"]),
                        "revision": self._revision}
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
            mode = ("overlay" if svc.config.overlay_mode else "monitor") if local else "hdmi"
            return {"mode": mode, "modes": [mode], "monitor": selection["id"] if selection else None,
                    "active": bool(active and not blocked), "blocked": blocked, "monitors": monitors,
                    "revision": self._revision, "requires_confirmation": local, "local_features": features}

    def overlay_monitor(self) -> DesktopMonitor | None:
        """Read the selected physical geometry without taking the source lock.

        The presenter can call this while the copilot holds its state lock.
        Source changes also clear copilot state while holding the source lock.
        """
        svc = self._services
        selector, monitor_source = self.selector, svc.monitor_source
        overlay = selector.selection.mode == "overlay" if selector is not None else svc.config.overlay_mode
        if not overlay or monitor_source is None:
            return None
        settings = svc.settings.get()
        if not self._armed() or settings["monitor"] is None:
            return None
        try:
            return selected_monitor(settings["monitor"], monitor_source.inventory())
        except (BairdError, OSError):
            return None

    def settings_applied(self, before: dict, after: dict) -> None:
        if self._services.monitor_source is not None and any(
            before[key] != after[key] for key in ("capture_enabled", "monitor", "monitor_microphone")
        ):
            self.advance_revision()

    def advance_revision(self) -> None:
        self._revision += 1
        self._controls.clear()
        if self._pending_capture is not None:
            self._pending_capture.cancelled.set()

    def close(self) -> None:
        with self.lock:
            if self._pending_capture is not None:
                self._pending_capture.cancelled.set()
            if self.selector is not None:
                self.selector.close()

    def _armed(self) -> bool:
        if self.selector is not None:
            return self._services.engine.source_enabled()
        return self._services.settings["capture_enabled"]

    def set_active(self, active: bool) -> dict[str, Any]:
        with self.lock:
            if self.selector is None or active:
                raise activation_required()
            self.selector.set_active(False)
            self.advance_revision()
            return self.status()

    def _set_confirmed(self) -> None:
        if self.selector is None:
            self._services.apply_settings({"capture_enabled": True})
        else:
            self.selector.set_active(True)
            self.advance_revision()

    def capture_control(self, session: str, target: ControlTarget = "capture") -> dict[str, Any]:
        """Issue a single-use control for the setting and display shown to the person."""
        with self.lock:
            state = self.status(list_displays=target == "capture")
            shown = None
            if target == "capture":
                selection = self._services.settings["monitor"]
                display = next((item for item in state["monitors"] if item["selection"] == selection), None)
                active = self._armed() if display is not None else False
                editable = display is not None
                if editable:
                    shown = {key: display[key] for key in ("name", "x", "y", "width", "height")}
            else:
                active = state["local_features"][target]
                editable = self._services.local_feature_editable(target)
            ticket = None
            if editable:
                now = time.monotonic()
                self._controls = {key: grant for key, grant in self._controls.items() if grant.expires > now}
                if len(self._controls) >= 64:
                    self._controls.pop(next(iter(self._controls)))
                ticket = token_urlsafe(32)
                self._controls[ticket] = CaptureControl(session, self._revision, state["mode"], state["monitor"],
                                                        active, now + CONTROL_LIFETIME, target)
            return {"active": active, "display": shown, "ticket": ticket}

    def _validate_control(self, grant: CaptureControl, enabled: bool) -> None:
        svc = self._services
        if grant.expires <= time.monotonic() or grant.revision != self._revision or grant.active == enabled:
            raise activation_required()
        if grant.target != "capture":
            if not svc.local_feature_editable(grant.target) or grant.active != svc.local_features()[grant.target]:
                raise activation_required()
            return
        state = self.status()
        if (grant.mode != state["mode"] or (state["blocked"] and enabled)
                or grant.monitor != state["monitor"] or grant.active != self._armed()):
            raise activation_required()
        if enabled:
            selected_monitor(svc.settings["monitor"], svc.monitor_source.inventory())

    def set_capture(self, *, session: str, ticket: str, enabled: bool, target: ControlTarget = "capture") -> None:
        with self.lock:
            grant = self._controls.get(ticket)
            if grant is None or grant.session != session or grant.target != target:
                raise activation_required()
            del self._controls[ticket]
            self._validate_control(grant, enabled)
            if not enabled:
                if target != "capture":
                    self._services.set_local_feature(target, False)
                elif self.selector is None:
                    self._services.apply_settings({"capture_enabled": False})
                else:
                    self.set_active(False)
                return
            if self._pending_capture is not None:
                raise BairdError("source_approval_pending", "A local capture confirmation is already pending.", status=409)
            now = time.monotonic()
            available_keys = [key for key in APPROVAL_KEYS if self._reserved_keys.get(key, 0.0) <= now]
            if not available_keys:
                raise BairdError("source_approval_pending", "Please wait before requesting another local confirmation.", status=409)
            pending = PendingCapture(session, ticket, choice(available_keys))
            # A shortcut shown by an expiring dialog must not approve a different request.
            self._reserved_keys[pending.key] = now + 2 * APPROVAL_SECONDS
            self._pending_capture = pending

        try:
            try:
                approved = self._confirm(pending.key, pending.cancelled, pending.registered)
            except OSError:
                approved = False
            with self.lock:
                if approved is not True or not pending.registered.is_set() or pending.cancelled.is_set():
                    raise BairdError("source_approval_required", "Confirm with the Windows shortcut shown in Baird.", status=403)
                self._validate_control(grant, True)
                if target == "capture":
                    self._set_confirmed()
                else:
                    self._services.set_local_feature(target, True)
        finally:
            with self.lock:
                self._pending_capture = None

    def cancel_capture_control(self, *, session: str, ticket: str) -> None:
        with self.lock:
            grant = self._controls.get(ticket)
            if grant is not None and grant.session == session:
                del self._controls[ticket]
            pending = self._pending_capture
            if pending is not None and (pending.session, pending.ticket) == (session, ticket):
                pending.cancelled.set()

    def capture_confirmation_key(self, *, session: str, ticket: str) -> int | None:
        with self.lock:
            pending = self._pending_capture
            if (pending is not None and (pending.session, pending.ticket) == (session, ticket)
                    and pending.registered.is_set() and not pending.cancelled.is_set()):
                return pending.key
            return None

    def _check_recording(self) -> None:
        if self._services.library.recorder.status()["state"] in ("recording", "paused", "finishing"):
            raise BairdError("recording_in_progress", "Finish the recording before changing source.", status=409)

    def select(self, mode: SourceMode, monitor: str | None = None) -> dict[str, Any]:
        svc = self._services
        with self.lock:
            if self.selector is not None:
                if mode == "hdmi" and monitor is not None:
                    raise BairdError("invalid", "HDMI does not use a monitor ID.")
                return self._select_integration(lambda: self.selector.select(mode, monitor))
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

    def _select_integration(self, change: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        previous = self.selector.selection
        state = change()
        selection = self.selector.selection
        if state.get("undo_token") or previous != selection:
            self._services.copilot.clear_looks()
            self.advance_revision()
        status = self.status()
        return {**status, **{key: state[key] for key in ("undo_token",) if key in state}}

    def undo(self, token: str) -> dict[str, Any]:
        with self.lock:
            if self.selector is not None:
                return self._select_integration(lambda: self.selector.undo(token))
            if self._undo is None or self._undo[:2] != (token, self._revision):
                raise BairdError("conflict", "That source change can no longer be undone.")
            self._check_recording()
            self._services.apply_settings({"monitor": self._undo[2], "capture_enabled": False})
            self._undo = None
            return self.status()
