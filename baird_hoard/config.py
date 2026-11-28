"""Process configuration (environment) and the editable settings (``<data>/settings.json``)."""

from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import APP_ID, DEFAULT_PORT
from .hoard_link.appconfig import AppPaths, env_flag, env_int, env_str
from .hoard_link.atomic import read_json, write_json_atomic
from .hoard_link.guard import parse_allowed_hosts
from .enhance import DEFAULT_ENHANCE, normalize_enhance
from .hotkeys import normalize_hotkey
from .monitor_source import normalize_selection

REPO_ROOT = Path(__file__).resolve().parent.parent


def default_media_dir() -> str:
    """``~/Videos/Baird`` - a short, visible folder: the files are meant to be dragged out of it."""
    return str(Path.home() / "Videos" / "Baird")


DEFAULT_COPILOT: dict[str, Any] = {
    "enabled": True,      # off: no assistant can draw over the live picture
    "look_side": 1024,    # longest side of the picture an assistant gets when it looks (smaller = cheaper for it)
    "idle_minutes": 15,   # a session nobody touches (no assistant call, no step done) ends by itself after this long
    "ocr_lang": "",       # language Windows reads the screen in (es-ES, en-US...); empty = the first one of the user profile
}


def normalize_copilot(raw: Any, base: Any = None) -> dict[str, Any]:
    data = dict(DEFAULT_COPILOT)
    for source in (base, raw):
        if isinstance(source, dict):
            data.update({k: v for k, v in source.items() if k in DEFAULT_COPILOT and v is not None})
    try:
        side, idle = int(data["look_side"]), float(data["idle_minutes"])
    except (TypeError, ValueError):
        raise ValueError("copilot.look_side and copilot.idle_minutes must be numbers") from None
    return {"enabled": data["enabled"] is not False, "look_side": int(min(max(side, 480), 2048)),
            "idle_minutes": round(min(max(idle, 1.0), 240.0), 1), "ocr_lang": str(data.get("ocr_lang") or "").strip()[:20]}


DEFAULT_SETTINGS: dict[str, Any] = {
    "device": "C835",            # part of the DirectShow name of the capture device ("AVerMedia C835 Capture")
    "media_dir": "",             # where screenshots, clips and recordings go (default_media_dir())
    "clip_seconds": 30,          # length of "save the last seconds"
    "buffer_seconds": 120,       # how much of the past is kept in memory (the longest possible clip)
    "snapshot_format": "png",    # png (exact pixels) or jpg (smaller)
    "audio_input": "hdmi",       # hdmi (the sound that comes with the picture) or line (the 3.5 mm input)
    "capture_always_on": False,  # opt in to capture even without viewers, recordings or rules
    "capture_enabled": True,     # capture card preference
    "hdmi_armed": False,         # local HDMI authorization, kept across restarts
    "monitor_capture": False,   # show this PC's display sources after a local opt-in
    "overlay_mode": False,       # allow the physical overlay source
    "source_mode": None,         # last source selection; None in profiles saved before this field
    "monitor": None,             # stable monitor selection from /api/monitors
    "monitor_microphone": False, # separate microphone track, explicitly enabled
    "light_height": 720,         # "light copy" of a video: height in pixels
    "light_kbps": 2500,          # "light copy" of a video: video bitrate
    "ocr_auto": True,            # read the text of every new screenshot in the background (Kafka's OCR)
    "hotkeys_enabled": True,     # global shortcuts, also when another window has the focus
    "hotkey_shot": "ctrl+alt+c",
    "hotkey_clip": "ctrl+alt+x",
    "enhance": DEFAULT_ENHANCE,  # GPU enhancement of the picture: effects, where it applies, which GPU (enhance.py)
    "copilot": DEFAULT_COPILOT,  # live guidance drawn over the picture by an assistant (copilot.py)
}

SNAPSHOT_FORMATS = ("png", "jpg")
AUDIO_INPUTS = ("hdmi", "line", "spdif")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")
    port: int = DEFAULT_PORT
    allowed_hosts: tuple[str, ...] = ()
    data_dir_configured: bool = False
    fake: bool = False              # demo/tests: a synthetic source instead of the device
    fake_source: str = ""           # with ``fake``: loop this video file instead of the test pattern
    background: bool = True         # run OCR, watches, hotkeys and optional always-on capture
    isolated_instance: bool = False
    synthetic_capture: bool = False  # opt in to the marked, device-free test source
    monitor_capture: bool = False    # keep the desktop source enabled in a test profile
    overlay_mode: bool = False        # select the overlay source in a monitor test profile
    monitor_policy_provider: str | None = None  # Trusted runtime adapter available inside the worker.

    @property
    def paths(self) -> AppPaths:
        return AppPaths(APP_ID, REPO_ROOT, self.data_dir, self.data_dir_configured)

    @property
    def token_path(self) -> Path:
        return self.paths.token_path

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def index_path(self) -> Path:
        return self.data_dir / "media-index.json"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @classmethod
    def from_env(cls) -> "Config":
        data_env = env_str("BAIRD_DATA_DIR")
        isolated_instance = bool(data_env) and env_flag("BAIRD_ISOLATED_INSTANCE")
        return cls(
            data_dir=Path(data_env).expanduser() if data_env else REPO_ROOT / "data",
            port=env_int("BAIRD_PORT", default=DEFAULT_PORT) or DEFAULT_PORT,
            allowed_hosts=tuple(parse_allowed_hosts(os.environ.get("BAIRD_ALLOWED_HOSTS", ""))),
            data_dir_configured=bool(data_env),
            fake=env_flag("BAIRD_FAKE"),
            fake_source=env_str("BAIRD_FAKE_SOURCE") or "",
            background=not isolated_instance,
            isolated_instance=isolated_instance,
            synthetic_capture=env_flag("BAIRD_SYNTHETIC_CAPTURE"),
            monitor_capture=env_flag("BAIRD_MONITOR_CAPTURE"),
            overlay_mode=env_flag("BAIRD_OVERLAY_MODE"),
            monitor_policy_provider=env_str("BAIRD_MONITOR_POLICY_PROVIDER"),
        )


class Settings:
    """Thread-safe JSON settings with defaults; ``update`` validates and writes atomically."""

    def __init__(self, path: Path, *, isolated_instance: bool = False):
        self.path = path
        self._isolated_instance = isolated_instance
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        raw = read_json(self.path, default={}) or {}
        data = copy.deepcopy(DEFAULT_SETTINGS)
        if self._isolated_instance:
            data.update(capture_enabled=False, hotkeys_enabled=False)
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
            # Legacy HDMI profiles had only the capture preference. A saved source
            # selection or display settings rule out an automatic grant.
            if ("hdmi_armed" not in raw and raw.get("capture_enabled") is True
                    and raw.get("source_mode") is None and raw.get("monitor") is None
                    and not raw.get("monitor_capture") and not raw.get("overlay_mode")):
                data["hdmi_armed"] = True
        return normalize(data)

    def get(self) -> dict[str, Any]:
        with self._lock:
            out = copy.deepcopy(self._data)
        out["media_dir_effective"] = self.media_dir()
        return out

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._data[key])

    def media_dir(self) -> str:
        with self._lock:
            if self._data["media_dir"]:
                return self._data["media_dir"]
            return str(self.path.parent / "media") if self._isolated_instance else default_media_dir()

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = copy.deepcopy(self._data)
            for key, value in dict(changes).items():
                if key == "media_dir_effective":
                    continue
                if key not in DEFAULT_SETTINGS:
                    raise ValueError(f"Unknown setting: {key}")
                if value is None and key != "monitor":
                    continue
                data[key] = (normalize_enhance(value, data[key]) if key == "enhance" else
                             normalize_copilot(value, data[key]) if key == "copilot" else value)
            data = normalize(data)
            write_json_atomic(self.path, data)
            self._data = data
        return self.get()


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    data["device"] = str(data.get("device") or "C835").strip()[:80] or "C835"
    media = str(data.get("media_dir") or "").strip()
    data["media_dir"] = str(Path(media).expanduser()) if media else ""
    data["buffer_seconds"] = int(min(max(int(data["buffer_seconds"]), 10), 600))
    data["clip_seconds"] = int(min(max(int(data["clip_seconds"]), 3), data["buffer_seconds"]))
    if data["snapshot_format"] not in SNAPSHOT_FORMATS:
        raise ValueError("snapshot_format must be png or jpg")
    data["audio_input"] = str(data.get("audio_input") or "hdmi").strip().lower()
    if data["audio_input"] not in AUDIO_INPUTS and not (data["audio_input"].isdigit() and len(data["audio_input"]) == 1):
        raise ValueError("audio_input must be hdmi, line, spdif or a crossbar input number")
    data["capture_enabled"] = bool(data["capture_enabled"])
    data["hdmi_armed"] = (data["hdmi_armed"] is True and data["capture_enabled"]
                         and data["source_mode"] in (None, "hdmi"))
    data["monitor_capture"] = data["monitor_capture"] is True
    data["overlay_mode"] = data["overlay_mode"] is True
    if data["source_mode"] not in ("hdmi", "monitor", "overlay"):
        data["source_mode"] = None
    data["monitor"] = normalize_selection(data["monitor"])
    data["monitor_microphone"] = data["monitor_microphone"] is True
    data["capture_always_on"] = data["capture_always_on"] is True
    data["light_height"] = int(min(max(int(data["light_height"]), 240), 1080))
    data["light_kbps"] = int(min(max(int(data["light_kbps"]), 300), 20000))
    data["ocr_auto"] = bool(data["ocr_auto"])
    data["hotkeys_enabled"] = bool(data["hotkeys_enabled"])
    data["enhance"] = normalize_enhance(data.get("enhance"))
    data["copilot"] = normalize_copilot(data.get("copilot"))
    for key in ("hotkey_shot", "hotkey_clip"):
        try:
            data[key] = normalize_hotkey(data[key])
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from exc
    if data["hotkey_shot"] == data["hotkey_clip"]:
        raise ValueError("The screenshot and clip shortcuts must be different.")
    return data
