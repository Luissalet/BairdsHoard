"""The regular monitor command resolves a Hub authority inside the producer."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from contextlib import contextmanager
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from baird_hoard import capture, monitor_windows, monitor_worker
from baird_hoard.config import Config
from baird_hoard.errors import BairdError
from baird_hoard.main import create_app
from baird_hoard.monitor_input import feed_samples
from baird_hoard.monitor_privacy import configured_policy
from baird_hoard.monitor_source import MonitorSource, worker_command
from baird_hoard.services import Services

from .test_monitor_privacy import HubDouble
from .test_monitor_source import MONITOR, confirm_test_shortcut, set_local_capture


def test_confirmed_private_cut_replaces_temporary_policy_unavailable(monkeypatch, tmp_path):
    hub = HubDouble()
    authority = ModuleType("monitor_cutoff_test_hub")
    authority.connect = lambda: hub
    monkeypatch.setitem(sys.modules, authority.__name__, authority)
    config = Config(data_dir=tmp_path, port=17697, data_dir_configured=True,
                    monitor_capture=True, isolated_instance=True, background=False)
    monitor = MonitorSource("double-ffmpeg", tmp_path, inventory=lambda: [MONITOR],
                            policy_provider=f"{authority.__name__}:connect")
    svc = Services(config, monitor_source=monitor, confirm_capture=confirm_test_shortcut)
    with TestClient(create_app(config, svc), base_url="http://127.0.0.1:17697", client=("127.0.0.1", 50000)) as client:
        svc.sources.select("monitor", MONITOR.id)
        set_local_capture(client, True)
        local = svc.engine.local_source()
        read = hub.read
        monkeypatch.setattr(hub, "read", lambda *_: None)
        assert not svc.engine._check_local_policy(local)
        assert client.get("/api/status").json()["source"]["blocked"] == "policy_unavailable"

        monkeypatch.setattr(hub, "read", read)
        hub.change(private=True)
        source = client.get("/api/status").json()["source"]
        assert source["blocked"] == "privacy_blocked" and not source["active"]
        response = client.post("/api/agent/call", headers={"Authorization": f"Bearer {svc.token}"},
                               json={"name": "screen_look", "arguments": {"save": False}})
        assert response.status_code == 409 and response.json()["code"] == "privacy_blocked"
        assert svc.engine.buffer.latest() == (b"", 0)

        hub.change(private=False)
        source = client.get("/api/status").json()["source"]
        assert source["blocked"] == "privacy_blocked" and not source["active"]


@pytest.mark.parametrize(("policy_change", "cutoff"), [
    ({"private": True}, "privacy_blocked"),
    ({"excluded": True}, "application_excluded"),
    ({"audio_excluded": True}, "application_excluded"),
])
def test_default_monitor_command_connects_configured_hub(monkeypatch, tmp_path, capsys, policy_change, cutoff):
    hub = HubDouble()
    linked_scopes = []
    authority_module = ModuleType("monitor_test_hub")

    def connect():
        linked_scopes.append("connected")
        return hub

    authority_module.connect = connect
    monkeypatch.setitem(sys.modules, authority_module.__name__, authority_module)
    monkeypatch.setenv("BAIRD_MONITOR_POLICY_PROVIDER", "monitor_test_hub:connect")
    monkeypatch.setenv("BAIRD_MONITOR_CAPTURE", "1")
    monkeypatch.setenv("BAIRD_SYNTHETIC_CAPTURE", "0")
    monkeypatch.setenv("BAIRD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BAIRD_ISOLATED_INSTANCE", "1")
    monkeypatch.setenv("BAIRD_PORT", "17697")
    monkeypatch.setattr(monitor_windows, "desktop_inventory", lambda: [MONITOR])
    monkeypatch.setattr(capture, "ensure_helper", lambda: pytest.fail("unexpected HDMI helper"))

    monkeypatch.setattr("baird_hoard.services.ffmpeg_path", lambda: "double-ffmpeg")
    monkeypatch.setattr("baird_hoard.services.ffprobe_path", lambda _ffmpeg: None)
    config = Config.from_env()
    svc = Services(config, confirm_capture=confirm_test_shortcut)
    svc.settings.update({"ocr_auto": False})
    with TestClient(create_app(config, svc), base_url="http://127.0.0.1:17697", client=("127.0.0.1", 50000)) as client:
        assert svc.monitor_source.worker is worker_command and svc.engine._thread is None
        selected = svc.sources.select("monitor", MONITOR.id)
        assert not selected["active"] and selected["modes"] == ["hdmi", "monitor", "overlay"]
        set_local_capture(client, True)
        command = svc.engine.local_source().producer.command(svc.settings)
    assert command[1:3] == ["-m", "baird_hoard.monitor_worker"]
    assert command[-2:] == ["--policy-provider", "monitor_test_hub:connect"]
    monkeypatch.setattr(sys, "argv", command[2:])

    reads, acquired, acquired_at_cutoff = [], [], []
    feed = io.BytesIO()

    @contextmanager
    def synthetic_frames(_monitor):
        def grab():
            acquired.append("video")
            return bytes(8 * 8 * 4)

        yield grab

    class PCM:
        def read(self, length):
            reads.append(length)
            if len(reads) == 2:
                hub.change(**policy_change)
                acquired_at_cutoff.extend(acquired)
                if "audio_excluded" not in policy_change:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            self.gate.check()
                        except BairdError:
                            return bytes(length)  # The separate video sampler detected the cutoff.
                        time.sleep(0.005)
                    pytest.fail("The video sampler did not observe the policy change")
            return self.gate.acquire("audio", lambda: self.block(length))

        def block(self, length):
            acquired.append("audio")
            return bytes(length)

    @contextmanager
    def open_double(_audio, command, monitor, frames, _dimensions, gate, _microphone):
        assert command[command.index("-c:v") + 1] == "libx264" and "-init_hw_device" not in command
        pcm = PCM()
        pcm.gate = gate
        feed_samples(feed, pcm, monitor, frames=frames, size=(8, 8), gate=gate,
                     stop=threading.Event(), fps=30, channels=2)
        pytest.fail("a stopped input must not start the encoder")
        yield

    monkeypatch.setattr(monitor_windows, "desktop_frames", synthetic_frames)
    monkeypatch.setattr(monitor_worker, "_listen_for_quit", lambda _stop: None)
    monkeypatch.setattr(monitor_worker, "audio_helper", lambda _work_dir: "double-audio")
    monkeypatch.setattr(monitor_worker, "available_encoders", lambda _ffmpeg: {"libx264"})
    monkeypatch.setattr(monitor_worker, "open_guarded_encoder", open_double)
    assert monitor_worker.main() == 1
    assert linked_scopes == ["connected", "connected"]
    assert "video" in acquired and acquired.count("audio") == 1
    assert acquired == acquired_at_cutoff
    assert len(reads) == 2 and feed.getvalue().count(b"00db") == 1
    assert json.loads(capsys.readouterr().err)["code"] == cutoff


@pytest.mark.parametrize("provider", ["", "missing_module:connect", "monitor_test_hub:rejected", "invalid"])
def test_unavailable_policy_provider_grants_no_authority(provider, monkeypatch):
    module = ModuleType("monitor_test_hub")

    def rejected():
        raise ConnectionError("synthetic Hub failure")

    module.rejected = rejected
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert configured_policy(provider) is None
