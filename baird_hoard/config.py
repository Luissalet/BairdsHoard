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
from .hotkeys import normalize_hotkey

REPO_ROOT = Path(__file__).resolve().parent.parent


def default_media_dir() -> str:
    """``~/Videos/Baird`` - a short, visible folder: the files are meant to be dragged out of it."""
    return str(Path.home() / "Videos" / "Baird")


DEFAULT_SETTINGS: dict[str, Any] = {
    "device": "C835",            # part of the DirectShow name of the capture device ("AVerMedia C835 Capture")
    "media_dir": "",             # where screenshots, clips and recordings go (default_media_dir())
    "clip_seconds": 30,          # length of "save the last seconds"
    "buffer_seconds": 120,       # how much of the past is kept in memory (the longest possible clip)
    "snapshot_format": "png",    # png (exact pixels) or jpg (smaller)
    "audio_input": "hdmi",       # hdmi (the sound that comes with the picture) or line (the 3.5 mm input)
    "capture_enabled": True,     # off frees the device for other programs
    "light_height": 720,         # "light copy" of a video: height in pixels
    "light_kbps": 2500,          # "light copy" of a video: video bitrate
    "ocr_auto": True,            # read the text of every new screenshot in the background (Kafka's OCR)
    "hotkeys_enabled": True,     # global shortcuts, also when another window has the focus
    "hotkey_shot": "ctrl+alt+c",
    "hotkey_clip": "ctrl+alt+x",
}

SNAPSHOT_FORMATS = ("png", "jpg")
AUDIO_INPUTS = ("hdmi", "line")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")
    port: int = DEFAULT_PORT
    allowed_hosts: tuple[str, ...] = ()
    data_dir_configured: bool = False
    fake: bool = False              # demo/tests: a synthetic source instead of the device
    background: bool = True         # the capture thread; tests switch it off

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
        return cls(
            data_dir=Path(data_env).expanduser() if data_env else REPO_ROOT / "data",
            port=env_int("BAIRD_PORT", default=DEFAULT_PORT) or DEFAULT_PORT,
            allowed_hosts=tuple(parse_allowed_hosts(os.environ.get("BAIRD_ALLOWED_HOSTS", ""))),
            data_dir_configured=bool(data_env),
            fake=env_flag("BAIRD_FAKE"),
        )


class Settings:
    """Thread-safe JSON settings with defaults; ``update`` validates and writes atomically."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        raw = read_json(self.path, default={}) or {}
        data = copy.deepcopy(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
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
            return self._data["media_dir"] or default_media_dir()

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = copy.deepcopy(self._data)
            for key, value in dict(changes).items():
                if key == "media_dir_effective":
                    continue
                if key not in DEFAULT_SETTINGS:
                    raise ValueError(f"Unknown setting: {key}")
                if value is None:
                    continue
                data[key] = value
            data = normalize(data)
            self._data = data
            write_json_atomic(self.path, data)
        return self.get()


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    data["device"] = str(data.get("device") or "C835").strip()[:80] or "C835"
    media = str(data.get("media_dir") or "").strip()
    data["media_dir"] = str(Path(media).expanduser()) if media else ""
    data["buffer_seconds"] = int(min(max(int(data["buffer_seconds"]), 10), 600))
    data["clip_seconds"] = int(min(max(int(data["clip_seconds"]), 3), data["buffer_seconds"]))
    if data["snapshot_format"] not in SNAPSHOT_FORMATS:
        raise ValueError("snapshot_format must be png or jpg")
    if data["audio_input"] not in AUDIO_INPUTS:
        raise ValueError("audio_input must be hdmi or line")
    data["capture_enabled"] = bool(data["capture_enabled"])
    data["light_height"] = int(min(max(int(data["light_height"]), 240), 1080))
    data["light_kbps"] = int(min(max(int(data["light_kbps"]), 300), 20000))
    data["ocr_auto"] = bool(data["ocr_auto"])
    data["hotkeys_enabled"] = bool(data["hotkeys_enabled"])
    for key in ("hotkey_shot", "hotkey_clip"):
        try:
            data[key] = normalize_hotkey(data[key])
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from exc
    if data["hotkey_shot"] == data["hotkey_clip"]:
        raise ValueError("The screenshot and clip shortcuts must be different.")
    return data
