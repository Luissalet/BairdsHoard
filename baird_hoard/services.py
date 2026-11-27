"""Everything the app runs, built once per process: settings, the in-memory past, the capture engine, the live view,
the library, the text index (OCR), the screen watcher, the global-shortcut helper, the copilot and the agent token."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from . import __version__
from .agent_sources import LOCAL_FEATURES, ProfileSources, activation_required, capture_publication
from .capture import CaptureEngine, ffmpeg_path, ffprobe_path
from .capture_approval import ConfirmCapture, confirm_with_windows
from .capture_source import SyntheticSource
from .config import REPO_ROOT, Config, Settings
from .copilot import Copilot
from .enhance import EnhanceStage, Enhancer, StageSource
from .hoard_link import fam_notify, family
from .hoard_link.appconfig import env_flag
from .hoard_link.tokens import read_or_create_token
from .hotkeys import HotkeyHelper
from .live import LiveRemux
from .media import Library
from .monitor_privacy import PolicySnapshot, configured_policy
from .monitor_source import BoundMonitorSource, MonitorSource, normalize_selection, selected_monitor
from .native import NATIVE_DIR
from .ocr import OcrIndex, fold
from .overlay import CopilotOverlay, OverlayDisplay, selected_overlay_display
from .recording_search import RecordingSearch
from .screen_privacy import ScreenPolicy, ScreenPolicySubscription
from .source_controls import Display, ScreenBackend, Selection, SourceControls
from .textmap import TextReader
from .transcribe import Transcriber
from .tsbuf import TsBuffer
from .watch import WatchEngine

log = logging.getLogger("baird")

HOTKEY_KEYS = ("hotkeys_enabled", "hotkey_shot", "hotkey_clip")


class Services:
    def __init__(self, config: Config, *, monitor_source: MonitorSource | None = None,
                 confirm_capture: ConfirmCapture = confirm_with_windows, screen_backend: ScreenBackend | None = None,
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
        monitor = monitor_source or MonitorSource(self.ffmpeg, config.work_dir, policy_provider=config.monitor_policy_provider)
        monitor_enabled = not config.synthetic_capture and (config.monitor_capture or self.settings["monitor_capture"])
        self.monitor_source = monitor if monitor_enabled else None
        if monitor_enabled and self.settings["capture_enabled"]:
            self.settings.update({"capture_enabled": False})
        # An injected producer without an authority can still exercise the fixed profile contract.
        native_selector = not config.synthetic_capture and (
            monitor_source is None or monitor.policy_provider is not None or read_screen_policy is not None
        ) and (monitor_enabled or screen_backend is None)
        fixed_monitor = monitor_enabled and not native_selector
        source = SyntheticSource(self.ffmpeg) if config.synthetic_capture else self.monitor_source if fixed_monitor else None
        self.engine = CaptureEngine(self.settings, self.buffer, ffmpeg=self.ffmpeg, fake=config.fake, on_state=self._on_state,
                                    fake_source=config.fake_source, source=source, demand_only=fixed_monitor,
                                    graceful_stop=fixed_monitor,
                                    source_cwd=REPO_ROOT if fixed_monitor else NATIVE_DIR,
                                    local_source_cwd=REPO_ROOT if native_selector else None)
        self.live = LiveRemux(self.engine, self.ffmpeg)
        self.enhancer = Enhancer(self.ffmpeg, self.ffprobe, config.work_dir)
        self.stage = EnhanceStage(self.engine, self.settings, self.enhancer, on_state=self._on_stage)
        self.live_enhanced = LiveRemux(StageSource(self.stage), self.ffmpeg)  # type: ignore[arg-type]
        self.library = Library(config, self.settings, self.buffer, self.engine, self.ffmpeg, self.ffprobe)
        def recording_state():
            return self.library.recorder.status()["state"]

        self._make_monitor_selector = None
        if native_selector:
            profile = ProfileSources(self, confirm_capture)
            self.sources = profile

            def prepare_change(choice: Selection) -> None:
                binding = {"id": choice.monitor, "layout": choice.layout} if choice.monitor else None
                self.settings.update({"monitor": binding, "capture_enabled": False})

            def make_selector(initial: Selection) -> SourceControls:
                backend = screen_backend or self._monitor_backend(monitor, read_screen_policy)
                return SourceControls(self.engine, recording_state, backend, read_screen_policy, subscribe_screen_policy,
                                      initial=initial, prepare_change=prepare_change,
                                      overlay_enabled=self._overlay_available, lock=profile.lock)

            self._make_monitor_selector = make_selector
            if monitor_enabled:
                saved = self.settings["monitor"] or {}
                overlay = config.overlay_mode or self.settings["overlay_mode"]
                profile.selector = make_selector(Selection("overlay" if overlay else "monitor",
                                                           saved.get("id"), saved.get("layout")))
        elif screen_backend is not None:
            self.sources = SourceControls(self.engine, recording_state, screen_backend, read_screen_policy,
                                          subscribe_screen_policy)
        else:
            self.sources = ProfileSources(self, confirm_capture)
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
        self.copilot.overlay = CopilotOverlay(
            self._overlay_display, self.copilot.overlay_changed,
            test=(config.isolated_instance or not config.background or config.fake or config.synthetic_capture
                  or env_flag("BAIRD_OVERLAY_TEST")),
        )
        self.library.capture_publication = lambda *, finalizes_call=False: capture_publication(self, finalizes_call=finalizes_call)
        self.copilot.capture_publication = lambda: capture_publication(self)
        self._monitor = monitor
        self.started = time.time()

    def _overlay_available(self) -> bool:
        # The environment monitor profile retains its original three source modes.
        legacy_overlay = self.config.monitor_capture and self._make_monitor_selector is not None
        return self.monitor_source is not None and (legacy_overlay or self.config.overlay_mode or self.settings["overlay_mode"])

    def local_features(self) -> dict[str, bool]:
        return {"monitor_capture": self.monitor_source is not None, "overlay_mode": self._overlay_available()}

    def local_feature_editable(self, feature: str) -> bool:
        if self._make_monitor_selector is None or self.config.monitor_capture:
            return False
        return (feature == "monitor_capture" or
                (feature == "overlay_mode" and self.monitor_source is not None and not self.config.overlay_mode))

    def set_local_feature(self, feature: str, enabled: bool) -> None:
        """Apply a change already authorized by the local settings control."""
        profile = self.sources
        with profile.lock:
            if not self.local_feature_editable(feature):
                raise activation_required()
            if feature == "monitor_capture":
                if enabled:
                    selector = self._make_monitor_selector(Selection())
                    self.settings.update({feature: True})
                    self.monitor_source = self._monitor
                    profile.selector = selector
                else:
                    if profile.selector.selection.mode != "hdmi":
                        profile.select("hdmi")
                    self.settings.update({feature: False, "overlay_mode": False})
                    profile.selector.close()
                    profile.selector = None
                    self.monitor_source = None
            else:
                if not enabled and profile.selector.selection.mode == "overlay":
                    profile.select("monitor")
                self.settings.update({feature: enabled})
            profile.advance_revision()

    @staticmethod
    def _monitor_backend(monitor: MonitorSource, read_override) -> ScreenBackend:
        def displays():
            return [Display(row["id"], row["name"], row["width"], row["height"], row["x"], row["y"],
                            row["selection"]["layout"]) for row in monitor.list_monitors()]

        authority = configured_policy(monitor.policy_provider)

        def snapshot(display: Display) -> PolicySnapshot:
            state = authority.read(display.id, display.layout) if authority is not None else None
            if (not isinstance(state, PolicySnapshot) or type(state.revision) is not int
                    or any(type(flag) is not bool for flag in (state.private, state.excluded, state.audio_excluded))):
                raise ConnectionError("Screen privacy could not be confirmed.")
            return state

        def read(display: Display) -> ScreenPolicy:
            state = snapshot(display)
            return ScreenPolicy(state.private, (), state.excluded or state.audio_excluded)

        @contextmanager
        def permit(display: Display):
            state = snapshot(display)
            if state.private or state.excluded or state.audio_excluded:
                yield False
                return
            publication = getattr(authority, "publication", None)
            guard = (publication(display.id, display.layout, state.revision) if callable(publication)
                     else authority.lease(display.id, display.layout, state.revision, "video"))
            with guard as confirmed:
                yield confirmed

        return ScreenBackend(displays, lambda display: BoundMonitorSource(monitor, display.id, display.layout),
                             lambda _display: None, None if read_override is not None else read,
                             permit if authority is not None else None)

    def _overlay_display(self) -> OverlayDisplay | None:
        if self.engine.snapshot()["state"] != "running":
            return None
        sources = self.sources
        if isinstance(sources, ProfileSources):
            display = sources.overlay_monitor()
            return OverlayDisplay(display.id, display.x, display.y, display.width, display.height) if display else None
        return selected_overlay_display(sources.status())

    def start(self) -> None:
        if self.config.background:
            self.engine.settings_changed()
            self.ocr.start()
            self.recordings.resume()
            self.watch.start()
            self._start_hotkeys()

    def stop(self) -> None:
        if isinstance(self.sources, (ProfileSources, SourceControls)):
            self.sources.close()
        self.recordings.stop()
        self.transcriber.detail.cancel(closing=True)
        self.copilot.close()
        if self.library.recorder.state in ("recording", "paused"):
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
        if state == "privacy_blocked":
            self.library.recorder.source_changed()
        if state != "running" and previous == "running":
            self.copilot.clear_looks()
        self.stage.raw_state(state == "running")
        if state == "reselect" or (state == "privacy_blocked" and previous != state):
            with self.sources.lock:
                if self.engine.snapshot()["state"] == state:
                    if isinstance(self.sources, ProfileSources):
                        self.sources.advance_revision()
                    self.copilot.clear_looks()
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
        if LOCAL_FEATURES & changes.keys():
            raise activation_required()
        with self.sources.lock:
            return self._apply_settings(changes)

    def _apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        before = self.settings.get()
        if self.monitor_source is not None and changes.get("monitor") is not None:
            selected_monitor(normalize_selection(changes["monitor"]), self.monitor_source.inventory())
        selector = self.sources.selector if isinstance(self.sources, ProfileSources) else None
        if selector is not None and selector.selection.mode != "hdmi":
            if "monitor" in changes:
                binding = normalize_selection(changes["monitor"])
                self.sources.select(selector.selection.mode, binding["id"] if binding else None)
            if ("monitor_microphone" in changes and before["monitor_microphone"] != changes["monitor_microphone"]
                    and self.engine.source_enabled()):
                self.sources.set_active(False)
        after = self.settings.update(changes)
        if isinstance(self.sources, ProfileSources):
            self.sources.settings_applied(before, after)
        self.buffer.keep_seconds = after["buffer_seconds"]
        if self.monitor_source is not None:
            different_source = any(before[k] != after[k] for k in ("monitor", "monitor_microphone"))
            revoked = before["capture_enabled"] and not after["capture_enabled"]
            if different_source:
                self.library.recorder.source_changed()
            if different_source or revoked:
                self.copilot.clear_looks()
            reopen = different_source or revoked or "monitor" in changes
        else:
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
        return {"capture": cap, "source": self.sources.status(), "recording": self.library.recorder.status(), "viewers": self.live.viewers,
                "transcription": self.transcriber.status(), "jobs": self.library.jobs(), "enhance": self.stage.status(), "media_dir": self.settings.media_dir(), "ffmpeg": bool(self.ffmpeg),
                "clip_seconds": self.settings["clip_seconds"], "version": __version__,
                "watch": self.watch.summary(), "hotkeys": self.hotkeys.status(self.settings["hotkeys_enabled"]),
                "copilot": self.copilot.status()}

    def health(self) -> dict[str, Any]:
        cap = self.engine.snapshot()
        return {"capture": cap.get("state"), "device": cap.get("device") or None, "input": cap.get("input"),
                "recording": self.library.recorder.state, "ffmpeg": bool(self.ffmpeg)}
