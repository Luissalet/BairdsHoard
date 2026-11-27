"""The opt-in source uses the normal engine and emits capture-compatible transport packets."""

from __future__ import annotations

import json
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from baird_hoard import capture
from baird_hoard.capture_source import SyntheticSource
from baird_hoard.config import Config, Settings
from baird_hoard.main import create_app
from baird_hoard.services import Services
from baird_hoard.tsbuf import TsBuffer

from .conftest import FFMPEG


def test_source_flag_defaults_to_the_hdmi_command(tmp_path, monkeypatch):
    monkeypatch.delenv("BAIRD_SYNTHETIC_CAPTURE", raising=False)
    monkeypatch.delenv("BAIRD_FAKE", raising=False)
    monkeypatch.setenv("BAIRD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BAIRD_ISOLATED_INSTANCE", "1")
    helper = tmp_path / "mock-helper.exe"
    monkeypatch.setattr(capture, "ensure_helper", lambda: helper)

    cfg = Config.from_env()
    assert not cfg.synthetic_capture
    svc = Services(cfg)
    svc.settings.update({"device": "Test Device", "audio_input": "line"})
    assert svc.engine._command() == [str(helper), "--device", "Test Device", "--audio", "line", "--out", "-"]
    assert svc.engine._proc is None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_synthetic_source_opens_on_demand_and_serves_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIRD_SYNTHETIC_CAPTURE", "1")
    monkeypatch.delenv("BAIRD_FAKE", raising=False)
    monkeypatch.setenv("BAIRD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BAIRD_ISOLATED_INSTANCE", "1")
    monkeypatch.setenv("BAIRD_PORT", "17523")
    monkeypatch.setattr(capture, "ensure_helper", lambda: pytest.fail("synthetic source tried to open the HDMI helper"))
    monkeypatch.setattr("baird_hoard.api.ui.list_devices", lambda: pytest.fail("synthetic source tried to list HDMI devices"))

    cfg = Config.from_env()
    assert cfg.synthetic_capture
    svc = Services(cfg)
    svc.settings.update({"capture_enabled": True, "ocr_auto": False})
    with TestClient(create_app(cfg, svc), base_url="http://127.0.0.1:17523", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/devices").json() == {"devices": ["synthetic"]}
        assert client.get("/api/status").json()["capture"]["state"] == "idle"
        assert svc.engine._proc is None
        result = client.post("/api/snapshot", json={})
        assert result.status_code == 200, result.text
        assert (result.json()["width"], result.json()["height"]) == (1280, 720)
        status = client.get("/api/status").json()["capture"]
        assert status["device"] == "synthetic" and status["audio"]
        assert not svc.engine._budget_path.exists()


@pytest.mark.skipif(not FFMPEG or not shutil.which("ffprobe"), reason="ffmpeg and ffprobe are needed")
def test_synthetic_stream_has_stereo_aac_and_one_second_keyframes(tmp_path):
    path = tmp_path / "synthetic.ts"
    command = SyntheticSource(FFMPEG).command(Settings(tmp_path / "settings.json"))
    subprocess.run([*command[:-1], "-t", "2.25", str(path)], check=True, timeout=15, creationflags=capture.NO_WINDOW)
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_frames", "-of", "json", str(path)],
        timeout=15, creationflags=capture.NO_WINDOW,
    )
    data = json.loads(probe)
    assert data["format"]["format_name"] == "mpegts"
    video, audio = data["streams"]
    assert (video["codec_name"], video["profile"], video["level"], video["r_frame_rate"]) == ("h264", "High", 40, "30/1")
    assert (audio["codec_name"], audio["profile"], audio["sample_rate"], audio["channels"]) == ("aac", "LC", "48000", 2)
    keyframes = [float(f["best_effort_timestamp_time"]) for f in data["frames"]
                 if f["media_type"] == "video" and f["key_frame"]]
    assert len(keyframes) == 3
    assert [keyframes[1] - keyframes[0], keyframes[2] - keyframes[1]] == pytest.approx([1.0, 1.0], abs=0.001)
    buffer = TsBuffer()
    _, starts = buffer.feed(path.read_bytes())
    assert len(starts) == len(keyframes)
    assert buffer.audio_pid is not None and buffer.latest()[1]
