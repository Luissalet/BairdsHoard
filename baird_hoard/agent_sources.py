"""Use the source controller before opening a live image from any agent tool."""

from __future__ import annotations

import threading
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import BairdError

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
                             "Ask the person to check the source in Baird.", status=409)
        if not state["active"]:
            raise BairdError("source_off", "The local screen is off.",
                             "Ask the person to turn it on in Baird.", status=403)
    return mode, monitor, state.get("revision")


def call_on_source(svc, expected: SourceArgs | None, run: Callable[[], Any], *, live: bool = True) -> Any:
    """Bind a call to its source and check that fresh results still belong to it."""
    original = _focus(svc, expected, live)
    result = run()
    if live and _focus(svc, expected, live) != original:
        raise BairdError("source_changed", "The source changed during the request. Look again.", status=409)
    return result


class ProfileSources:
    """Source controller for the fixed HDMI or monitor profiles.

    The source bar's SourceControls can supply the same status/select/undo
    operations when the live selector is composed with the monitor backend.
    """

    def __init__(self, services):
        self._services = services
        self._lock = threading.RLock()
        self._revision = 0
        self._undo: tuple[str, int, dict[str, str] | None] | None = None

    def status(self, *, list_displays: bool = False) -> dict[str, Any]:
        svc = self._services
        with self._lock:
            settings = svc.settings.get()
            local = svc.monitor_source is not None
            selection = settings["monitor"] if local else None
            active = settings["capture_enabled"] and (not local or selection is not None)
            blocked = "monitor_reselect" if local and active and svc.engine.snapshot()["state"] == "reselect" else None
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
            with self._lock:
                self._revision += 1

    def _check_recording(self) -> None:
        if self._services.library.recorder.status()["state"] in ("recording", "paused", "finishing"):
            raise BairdError("recording_in_progress", "Finish the recording before changing source.", status=409)

    def select(self, mode: SourceMode, monitor: str | None = None) -> dict[str, Any]:
        svc = self._services
        with self._lock:
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
        with self._lock:
            if self._undo is None or self._undo[:2] != (token, self._revision):
                raise BairdError("conflict", "That source change can no longer be undone.")
            self._check_recording()
            self._services.apply_settings({"monitor": self._undo[2], "capture_enabled": False})
            self._undo = None
            return self.status()
