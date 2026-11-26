"""Pause a recording without retaining the paused interval or dropping either track."""

from __future__ import annotations

import json
import shutil
import subprocess
from contextlib import contextmanager

import pytest

from baird_hoard.capture import NO_WINDOW
from baird_hoard.config import Config, Settings
from baird_hoard.errors import BairdError
from baird_hoard.media import Library
from baird_hoard.tsbuf import TsBuffer

from .conftest import FFMPEG
from .test_monitor_source import make_small_transport, wait_until


class Engine:
    def __init__(self, buffer):
        self.buffer = buffer
        self.listener = None
        self.demands = set()
        self.encoder = "test"

    def add_listener(self, callback):
        self.listener = callback

    @contextmanager
    def request(self):
        yield

    def wait_for_picture(self):
        self.require_picture()

    def require_picture(self):
        assert self.buffer.latest()[1]

    def snapshot(self):
        return {"encoder": self.encoder}

    def set_demand(self, who, on):
        self.demands.add(who) if on else self.demands.discard(who)

    def feed(self, data):
        aligned, offsets = self.buffer.feed(data)
        self.listener(aligned, offsets, self.buffer.bytes_total)


@pytest.fixture
def recording(tmp_path):
    ffprobe = shutil.which("ffprobe")
    if not FFMPEG or not ffprobe:
        pytest.skip("ffmpeg and ffprobe needed")
    payload = make_small_transport(tmp_path / "pattern.ts", microphone=True, seconds=5.5)
    probe = TsBuffer()
    _, starts = probe.feed(payload)
    assert len(starts) == 6
    config = Config(data_dir=tmp_path / "profile", isolated_instance=True, background=False)
    config.work_dir.mkdir(parents=True)
    buffer = TsBuffer()
    engine = Engine(buffer)
    library = Library(config, Settings(config.settings_path, isolated_instance=True), buffer, engine, FFMPEG, ffprobe)
    return library.recorder, engine, payload, starts, ffprobe


def test_pause_resume_keeps_two_tracks_and_skips_the_paused_gops(recording):
    rec, engine, payload, starts, ffprobe = recording
    engine.feed(payload[:starts[1]])
    active = rec.start()
    assert active["state"] == "recording" and active["can_pause"]
    assert engine.demands == {"recording"}

    paused = rec.pause(target=active["target"])
    assert paused["state"] == "paused" and not engine.demands
    assert rec.pause() == paused
    engine.feed(payload[starts[1]:starts[3]])
    assert rec.status()["bytes"] == paused["bytes"]
    assert rec.status()["seconds"] == paused["seconds"]

    with pytest.raises(BairdError, match="This recording already ended"):
        rec.resume(target="obsolete.mp4")
    assert rec.resume(target=active["target"])["state"] == "recording"
    assert rec.status()["bytes"] == paused["bytes"]
    engine.feed(payload[starts[3]:])
    assert engine.demands == {"recording"} and rec.status()["bytes"] > paused["bytes"]
    rec.stop(target=active["target"])
    result = wait_until(lambda: rec.status()["last"] if rec.state == "idle" else None)
    assert result["name"] == active["target"] and "error" not in result

    data = json.loads(subprocess.check_output(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", result["path"]],
        creationflags=NO_WINDOW, timeout=10,
    ))
    video, system, mic = data["streams"]
    assert video["codec_name"] == "h264" and int(video["nb_frames"]) == 105
    assert [(a["codec_name"], a["channels"]) for a in (system, mic)] == [("aac", 2)] * 2
    assert float(data["format"]["duration"]) == pytest.approx(3.5, abs=0.2)
    assert len(list(rec.lib.config.work_dir.glob("rec-*"))) == 0


def test_paused_recording_is_saved_if_source_changes(recording):
    rec, engine, payload, starts, _ = recording
    engine.feed(payload[:starts[1]])
    name = rec.start()["target"]
    rec.pause()
    rec.source_changed()
    saved = wait_until(lambda: rec.status()["last"] if rec.state == "idle" else None)
    assert saved["name"] == name and "error" not in saved
    assert not engine.demands
    with pytest.raises(BairdError, match="There is no paused recording"):
        rec.resume()
