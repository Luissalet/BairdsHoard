"""Everything the app runs, built once per process: settings, the in-memory past, the capture engine, the live view,
the library and the agent token."""

from __future__ import annotations

import logging
import time
from typing import Any

from . import __version__
from .capture import CaptureEngine, ffmpeg_path, ffprobe_path
from .config import Config, Settings
from .hoard_link.tokens import read_or_create_token
from .live import LiveRemux
from .media import Library
from .tsbuf import TsBuffer

log = logging.getLogger("baird")


class Services:
    def __init__(self, config: Config):
        self.config = config
        config.paths.ensure()
        self.settings = Settings(config.settings_path)
        self.token = read_or_create_token(config.token_path)
        self.ffmpeg = ffmpeg_path()
        self.ffprobe = ffprobe_path(self.ffmpeg)
        self.buffer = TsBuffer(keep_seconds=self.settings["buffer_seconds"])
        self._last_state = "off"
        self.engine = CaptureEngine(self.settings, self.buffer, ffmpeg=self.ffmpeg, fake=config.fake, on_state=self._on_state)
        self.live = LiveRemux(self.engine, self.ffmpeg)
        self.library = Library(config, self.settings, self.buffer, self.engine, self.ffmpeg, self.ffprobe)
        self.started = time.time()

    def start(self) -> None:
        if self.config.background:
            self.engine.start()

    def stop(self) -> None:
        if self.library.recorder.state == "recording":
            try:
                self.library.recorder.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping the recording")
        self.live.stop()
        self.engine.stop()

    def _on_state(self, snap: dict[str, Any]) -> None:
        state = snap.get("state", "")
        previous, self._last_state = self._last_state, state
        self.library.recorder.on_capture_state(state)
        if state == "running" and previous != "running":
            self.live.restart()

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        before = self.settings.get()
        after = self.settings.update(changes)
        self.buffer.keep_seconds = after["buffer_seconds"]
        if any(before[k] != after[k] for k in ("device", "audio_input", "capture_enabled")):
            self.engine.restart()
        return after

    def status(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap, "recording": self.library.recorder.status(), "viewers": self.live.viewers,
                "jobs": self.library.jobs(), "media_dir": self.settings.media_dir(), "ffmpeg": bool(self.ffmpeg),
                "clip_seconds": self.settings["clip_seconds"], "version": __version__}

    def health(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap.get("state"), "device": cap.get("device") or None, "input": cap.get("input"),
                "recording": self.library.recorder.state, "ffmpeg": bool(self.ffmpeg)}
