"""Selection and on-demand producer for the optional desktop capture source."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import BairdError

if TYPE_CHECKING:
    from .config import Settings

RESELECT = "Choose a monitor again. Its identity or the desktop layout has changed."


@dataclass(frozen=True)
class DesktopMonitor:
    id: str
    name: str
    x: int
    y: int
    width: int
    height: int
    rotation: int
    adapter: int
    output: int
    vendor: int

    def identity(self) -> dict[str, str | int]:
        return {"id": self.id, "name": self.name, "x": self.x, "y": self.y,
                "width": self.width, "height": self.height, "rotation": self.rotation}


def normalize_selection(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("monitor must be a selection returned by /api/monitors")
    monitor_id, layout = value.get("id"), value.get("layout")
    if not isinstance(monitor_id, str) or not 1 <= len(monitor_id) <= 512:
        raise ValueError("monitor.id is required")
    if not isinstance(layout, str) or not re.fullmatch(r"[0-9a-f]{64}", layout):
        raise ValueError("monitor.layout must be the current desktop layout")
    return {"id": monitor_id, "layout": layout}


def layout_id(monitors: list[DesktopMonitor]) -> str:
    stable = sorted((m.identity() for m in monitors), key=lambda m: json.dumps(m, sort_keys=True))
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()


def selected_monitor(selection: dict[str, str] | None, monitors: list[DesktopMonitor]) -> DesktopMonitor:
    if selection and selection["layout"] == layout_id(monitors):
        matches = [m for m in monitors if m.id == selection["id"]]
        if len(matches) == 1:
            return matches[0]
    raise BairdError("monitor_reselect", RESELECT, status=409)


def windows_inventory() -> list[DesktopMonitor]:
    from .monitor_windows import desktop_inventory
    return desktop_inventory()


def worker_command(ffmpeg: str, selection: dict[str, str], microphone: bool, work_dir: Path) -> list[str]:
    cmd = [sys.executable, "-m", "baird_hoard.monitor_worker", "--ffmpeg", ffmpeg,
           "--work-dir", str(work_dir), "--selection", json.dumps(selection)]
    if microphone:
        cmd.append("--microphone")
    return cmd


@dataclass(frozen=True)
class MonitorSource:
    ffmpeg: str | None
    work_dir: Path
    inventory: Callable[[], list[DesktopMonitor]] = windows_inventory
    worker: Callable[[str, dict[str, str], bool, Path], list[str]] = worker_command
    policy_provider: str | None = None

    def list_monitors(self) -> list[dict[str, Any]]:
        monitors = self.inventory()
        layout = layout_id(monitors)
        counts = Counter(m.id for m in monitors)
        return [{**m.identity(), "selection": {"id": m.id, "layout": layout}}
                for m in monitors if counts[m.id] == 1]

    def command(self, settings: Settings) -> list[str]:
        return self.command_for(settings["monitor"], settings)

    def command_for(self, selection: dict[str, str] | None, settings: Settings) -> list[str]:
        """Open the display bound by the active source controller."""
        if not self.ffmpeg:
            raise BairdError("unavailable", "FFmpeg is required for monitor capture.")
        if selection is None:
            raise BairdError("monitor_reselect", "Choose a monitor to start capture.", status=409)
        selected_monitor(selection, self.inventory())
        command = self.worker(self.ffmpeg, selection, settings["monitor_microphone"], self.work_dir)
        if self.policy_provider:
            command = [*command, "--policy-provider", self.policy_provider]
        return command

    def initial_state(self) -> dict[str, Any]:
        return {"source": "monitor", "device": "", "encoder": "", "audio": False,
                "audio_level": 0.0, "microphone_level": None}
