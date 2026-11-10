"""Everything the app runs, built once per process: settings, the in-memory past, the capture engine, the live view,
the library, the text index (OCR), the screen watcher, the global-shortcut helper and the agent token."""

from __future__ import annotations

import logging
import time
from typing import Any

from . import __version__
from .capture import CaptureEngine, ffmpeg_path, ffprobe_path
from .config import Config, Settings
from .hoard_link import fam_notify, family
from .hoard_link.tokens import read_or_create_token
from .hotkeys import HotkeyHelper
from .live import LiveRemux
from .media import Library
from .ocr import OcrIndex
from .tsbuf import TsBuffer
from .watch import WatchEngine

log = logging.getLogger("baird")

HOTKEY_KEYS = ("hotkeys_enabled", "hotkey_shot", "hotkey_clip")


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
        self.ocr = OcrIndex(config.data_dir / "ocr-index.json", self.library, self.settings, background=config.background)
        self.library.on_new_image = self.ocr.enqueue
        self.library.on_rename = self.ocr.renamed
        self.library.on_delete = self.ocr.deleted
        self.watch = WatchEngine(config.data_dir / "watch.json", self.library, self.engine, config.work_dir,
                                 ocr_fn=lambda path: self.ocr.ocr_fn(path), notify_fn=self._notify, emit_fn=family.emit)
        self.hotkeys = HotkeyHelper(config.data_dir / "hotkeys-status.json", config.port)
        self.started = time.time()

    def start(self) -> None:
        if self.config.background:
            self.engine.start()
            self.ocr.start()
            self.watch.start()
            self._start_hotkeys()

    def stop(self) -> None:
        if self.library.recorder.state == "recording":
            try:
                self.library.recorder.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping the recording")
        self.watch.stop()
        self.ocr.stop()
        self.hotkeys.stop()
        self.live.stop()
        self.engine.stop()

    def _start_hotkeys(self) -> None:
        s = self.settings
        self.hotkeys.start(s["hotkeys_enabled"], {"shot": s["hotkey_shot"], "clip": s["hotkey_clip"]})

    def _notify(self, title: str, body: str, dedupe_key: str) -> dict[str, Any]:
        return fam_notify.notify(title, body, priority="normal", group="baird-watch", dedupe_key=dedupe_key)

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
        if self.config.background:
            if any(before[k] != after[k] for k in HOTKEY_KEYS):
                self._start_hotkeys()
            if after["ocr_auto"] and not before["ocr_auto"]:
                self.ocr.reindex()
        return after

    def status(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap, "recording": self.library.recorder.status(), "viewers": self.live.viewers,
                "jobs": self.library.jobs(), "media_dir": self.settings.media_dir(), "ffmpeg": bool(self.ffmpeg),
                "clip_seconds": self.settings["clip_seconds"], "version": __version__,
                "watch": self.watch.summary(), "hotkeys": self.hotkeys.status(self.settings["hotkeys_enabled"])}

    def health(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap.get("state"), "device": cap.get("device") or None, "input": cap.get("input"),
                "recording": self.library.recorder.state, "ffmpeg": bool(self.ffmpeg)}
