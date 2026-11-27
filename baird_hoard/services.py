"""Everything the app runs, built once per process: settings, the in-memory past, the capture engine, the live view,
the library, the text index (OCR), the screen watcher, the global-shortcut helper, the copilot and the agent token."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from . import __version__
from .capture import CaptureEngine, ffmpeg_path, ffprobe_path
from .capture_source import SyntheticSource
from .config import Config, Settings
from .copilot import Copilot
from .enhance import EnhanceStage, Enhancer, StageSource
from .hoard_link import fam_notify, family
from .hoard_link.tokens import read_or_create_token
from .hotkeys import HotkeyHelper
from .live import LiveRemux
from .media import Library
from .ocr import OcrIndex, fold
from .recording_search import RecordingSearch
from .screen_privacy import ScreenPolicy, ScreenPolicySubscription
from .source_controls import ScreenBackend, SourceControls
from .textmap import TextReader
from .transcribe import Transcriber
from .tsbuf import TsBuffer
from .watch import WatchEngine

log = logging.getLogger("baird")

HOTKEY_KEYS = ("hotkeys_enabled", "hotkey_shot", "hotkey_clip")


class Services:
    def __init__(self, config: Config, *, screen_backend: ScreenBackend | None = None,
                 read_screen_policy: Callable[[], ScreenPolicy] | None = None,
                 subscribe_screen_policy: ScreenPolicySubscription | None = None):
        self.config = config
        config.paths.ensure()
        self.settings = Settings(config.settings_path, isolated_instance=config.isolated_instance)
        self.token = read_or_create_token(config.token_path)
        self.ffmpeg = ffmpeg_path()
        self.ffprobe = ffprobe_path(self.ffmpeg)
        self.buffer = TsBuffer(keep_seconds=self.settings["buffer_seconds"])
        self._last_state = "off"
        source = SyntheticSource(self.ffmpeg) if config.synthetic_capture else None
        self.engine = CaptureEngine(self.settings, self.buffer, ffmpeg=self.ffmpeg, fake=config.fake, on_state=self._on_state,
                                    fake_source=config.fake_source, source=source)
        self.live = LiveRemux(self.engine, self.ffmpeg)
        self.enhancer = Enhancer(self.ffmpeg, self.ffprobe, config.work_dir)
        self.stage = EnhanceStage(self.engine, self.settings, self.enhancer, on_state=self._on_stage)
        self.live_enhanced = LiveRemux(StageSource(self.stage), self.ffmpeg)  # type: ignore[arg-type]
        self.library = Library(config, self.settings, self.buffer, self.engine, self.ffmpeg, self.ffprobe)
        self.sources = SourceControls(self.engine, lambda: self.library.recorder.status()["state"],
                                      screen_backend, read_screen_policy, subscribe_screen_policy)
        self.transcriber = Transcriber(self.library)
        self.library.attach_enhancer(self.enhancer, self.stage)
        self.ocr = OcrIndex(config.data_dir / "ocr-index.json", self.library, self.settings, background=config.background)
        self.recordings = RecordingSearch(self.library)
        self.library.on_new_image = self.ocr.enqueue
        self.library.on_rename = self._renamed
        self.library.on_delete = self._deleted
        self.watch = WatchEngine(config.data_dir / "watch.json", self.library, self.engine, config.work_dir,
                                 ocr_fn=lambda path: self.ocr.ocr_fn(path), notify_fn=self._notify, emit_fn=family.emit,
                                 on_demand=lambda active: self.engine.set_demand("watch", active))
        self.hotkeys = HotkeyHelper(config.data_dir / "hotkeys-status.json", config.port)
        self.copilot = Copilot(engine=self.engine, library=self.library, settings=self.settings, reader=TextReader(),
                               ffmpeg=self.ffmpeg, work_dir=config.work_dir)
        self.started = time.time()

    def start(self) -> None:
        if self.config.background:
            self.engine.settings_changed()
            self.ocr.start()
            self.recordings.resume()
            self.watch.start()
            self._start_hotkeys()

    def stop(self) -> None:
        self.sources.close()
        self.recordings.stop()
        self.transcriber.detail.cancel(closing=True)
        self.copilot.close()
        if self.library.recorder.state == "recording":
            try:
                self.library.recorder.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping the recording")
        self.watch.stop()
        self.ocr.stop()
        self.hotkeys.stop()
        self.live.stop()
        self.stage.stop()
        self.live_enhanced.stop()
        self.engine.stop()

    def _renamed(self, old, new):
        self.ocr.renamed(old, new)
        self.recordings.renamed(old, new)

    def _deleted(self, name):
        self.ocr.deleted(name)
        self.recordings.deleted(name)

    def search(self, query, limit, recording=None, after=None):
        result = self.ocr.search(query, limit)
        clips, progress = self.recordings.search(fold(query).split(), limit, recording=recording, after=after)
        result["items"] = clips if recording or after is not None else (clips + result["items"])[:limit]
        result["recordings"] = progress
        return result

    def _start_hotkeys(self) -> None:
        s = self.settings
        self.hotkeys.start(s["hotkeys_enabled"], {"shot": s["hotkey_shot"], "clip": s["hotkey_clip"]})

    def _notify(self, title: str, body: str, dedupe_key: str) -> dict[str, Any]:
        return fam_notify.notify(title, body, priority="normal", group="baird-watch", dedupe_key=dedupe_key)

    def _on_state(self, snap: dict[str, Any]) -> None:
        state = snap.get("state", "")
        previous, self._last_state = self._last_state, state
        self.library.recorder.on_capture_state(state)
        self.stage.raw_state(state == "running")
        if state != previous and (state == "running" or previous == "running"):
            self.live.source_changed(state == "running")
            self.copilot.source_changed(state == "running")

    def _on_stage(self, running: bool) -> None:
        """The enhanced picture started or stopped flowing: its viewers get a new stream (or a reset)."""
        self.live_enhanced.source_changed(running)

    def enhance_status(self) -> dict[str, Any]:
        fx = self.settings["enhance"]
        return {"settings": fx, "live": self.stage.status(), "capabilities": self.enhancer.capabilities(int(fx["gpu"])),
                "last_benchmark": self.enhancer.last_benchmark,
                "jobs": [j for j in self.library.jobs() if j.get("kind") == "enhance"]}

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        before = self.settings.get()
        after = self.settings.update(changes)
        self.buffer.keep_seconds = after["buffer_seconds"]
        reopen = not self.config.synthetic_capture and any(before[k] != after[k] for k in ("device", "audio_input"))
        if reopen or any(before[k] != after[k] for k in ("capture_enabled", "capture_always_on")):
            self.engine.settings_changed(reopen=reopen)
        if before["enhance"] != after["enhance"]:
            self.stage.configure()
        if before["copilot"] != after["copilot"]:
            self.copilot.settings_changed()
        if self.config.background:
            if any(before[k] != after[k] for k in HOTKEY_KEYS):
                self._start_hotkeys()
            if after["ocr_auto"] and not before["ocr_auto"]:
                self.ocr.reindex()
        return after

    def status(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap, "source": self.sources.status(), "recording": self.library.recorder.status(),
                "viewers": self.live.viewers,
                "transcription": self.transcriber.status(), "jobs": self.library.jobs(), "enhance": self.stage.status(), "media_dir": self.settings.media_dir(), "ffmpeg": bool(self.ffmpeg),
                "clip_seconds": self.settings["clip_seconds"], "version": __version__,
                "watch": self.watch.summary(), "hotkeys": self.hotkeys.status(self.settings["hotkeys_enabled"]),
                "copilot": self.copilot.status()}

    def health(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap.get("state"), "device": cap.get("device") or None, "input": cap.get("input"),
                "recording": self.library.recorder.state, "ffmpeg": bool(self.ffmpeg)}
