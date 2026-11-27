from __future__ import annotations

import io
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from baird_hoard.capture_source import SyntheticSource
from baird_hoard.config import Config
from baird_hoard.main import create_app
from baird_hoard.screen_privacy import ScreenPolicy
from baird_hoard.services import Services
from baird_hoard.source_controls import Display, ScreenBackend

DISPLAY = Display("test:640x360@0,0", "Test display", 640, 360, 0, 0)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    state = SimpleNamespace(monitors=[DISPLAY], policy=ScreenPolicy(False, ()), apps=["math.exe"], opens=[])
    cfg = Config(data_dir=tmp_path, port=17529, data_dir_configured=True, synthetic_capture=True,
                 background=False, isolated_instance=True)
    monkeypatch.setattr("baird_hoard.capture.ensure_helper", lambda: pytest.fail("Native helper called"))
    monkeypatch.setattr("baird_hoard.api.ui.list_devices", lambda: pytest.fail("Native devices listed"))
    monkeypatch.setenv("HOARD_EVENTS", "0")

    def producer(display):
        state.opens.append(display.id)
        return SyntheticSource(None)

    svc = Services(cfg, screen_backend=ScreenBackend(lambda: state.monitors, producer, lambda _: state.apps),
                   read_screen_policy=lambda: state.policy)
    with TestClient(create_app(cfg, svc), base_url="http://127.0.0.1:17529", client=("127.0.0.1", 50000)) as client:
        yield client, svc, state


def choose(client, mode="monitor", monitor=DISPLAY.id):
    return client.post("/api/sources/select", json={"mode": mode, "monitor": monitor})


def activate(client, value=True):
    return client.post("/api/sources/active", json={"active": value})


def call(client, svc, name, args=None):
    return client.post("/api/agent/call", headers={"Authorization": f"Bearer {svc.token}"},
                       json={"name": name, "arguments": args or {}})


def test_selection_is_off_and_agent_can_only_select_or_undo(rig):
    client, svc, state = rig
    assert client.get("/api/status").json()["source"]["mode"] == "hdmi"
    listing = call(client, svc, "source_list").json()
    assert listing["modes"] == ["hdmi", "monitor", "overlay"]
    assert listing["monitors"][0]["id"] == DISPLAY.id

    result = call(client, svc, "source_select", {"mode": "overlay", "monitor": DISPLAY.id}).json()
    assert result["mode"] == "overlay" and not result["active"]
    svc.settings.update({"capture_enabled": True, "capture_always_on": True})
    with svc.engine.request():
        assert svc.engine._thread is None
    assert call(client, svc, "screen_look", {"save": False}).json()["code"] == "no_signal"
    assert call(client, svc, "source_activate", {"active": True}).status_code == 404
    assert state.opens == []
    svc.settings.update({"capture_enabled": False, "capture_always_on": False})
    undone = call(client, svc, "source_undo", {"undo_token": result["undo_token"]}).json()
    assert undone["mode"] == "hdmi"
    assert call(client, svc, "source_undo", {"undo_token": result["undo_token"]}).status_code == 409


@pytest.mark.parametrize(("policy", "apps", "reason"), [
    (ScreenPolicy(True, ()), [], "private"),
    (ScreenPolicy(False, ("protected.exe",)), [r"C:\Apps\PROTECTED.EXE"], "excluded"),
    (ScreenPolicy(False, ("protected.exe",)), None, "policy_unavailable"),
    (None, [], "policy_unavailable"),
])
def test_privacy_blocks_before_producer_is_created(rig, policy, apps, reason):
    client, svc, state = rig
    state.policy, state.apps = policy, apps
    assert choose(client).status_code == 200
    denied = activate(client)
    assert denied.status_code == 409 and denied.json()["code"] == reason
    source = client.get("/api/status").json()["source"]
    assert not source["active"] and source["blocked"] == reason
    assert svc.engine._proc is None and state.opens == []


def test_monitor_change_and_undo_start_off(rig):
    client, svc, state = rig
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    assert svc.engine._proc is None
    changes = choose(client, "overlay").json()
    assert changes["mode"] == "overlay" and not changes["active"]
    undone = call(client, svc, "source_undo", {"undo_token": changes["undo_token"]}).json()
    assert undone["mode"] == "monitor" and not undone["active"]
    assert state.opens == [DISPLAY.id]


def test_moved_display_needs_to_be_chosen_again(rig):
    client, _, state = rig
    assert choose(client).status_code == 200
    moved = Display("test:640x360@-640,0", DISPLAY.name, 640, 360, -640, 0)
    state.monitors[:] = [moved]
    denied = activate(client)
    assert denied.json()["code"] == "missing_display"
    assert state.opens == []
    assert choose(client, monitor=moved.id).status_code == 200
    assert activate(client).json()["active"]
    assert state.opens == [moved.id]


def test_privacy_cut_drops_transport_data_before_listeners(rig, monkeypatch):
    client, svc, state = rig
    monkeypatch.setattr("baird_hoard.screen_privacy.POLICY_REFRESH_S", 0)
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine = svc.engine
    seen = []
    engine.add_listener(lambda chunk, *_: seen.append(chunk))
    packet = b"\x47\x00\x20\x10" + bytes(184)

    class Stream(io.BytesIO):
        def read1(self, size):
            chunk = self.read(188)
            if self.tell() == 376:
                state.policy = ScreenPolicy(True, ())
            return chunk

    proc = SimpleNamespace(stdout=Stream(packet * 2))
    engine._proc = proc
    try:
        engine._pump(proc, {"got_data": False}, engine._local_source)
    finally:
        engine._proc = None
    assert seen == [packet]
    assert engine.snapshot()["state"] == "off"
    assert engine.buffer.latest() == (b"", 0)
    assert svc.sources.status()["blocked"] == "private"
    assert not svc.sources.status()["active"]
    state.policy = ScreenPolicy(False, ())
    assert not svc.sources.status()["active"]
    assert activate(client).json()["active"]


def test_old_remux_generation_cannot_enqueue_after_source_reset(rig, monkeypatch):
    _, svc, _ = rig
    live = svc.live
    seen = []
    live._viewers.append(SimpleNamespace(push=seen.append))
    old = live._generation

    def check_cleanup(_proc, *, discard):
        assert discard
        assert seen == [{"type": "reset"}]
        assert live._generation != old

    with monkeypatch.context() as patch:
        patch.setattr(live, "_close_proc", check_cleanup)
        live.source_changed(False)
    live._broadcast({"type": "init"}, generation=old)
    live._broadcast(b"old picture", generation=old)
    assert seen == [{"type": "reset"}]


def test_privacy_supervisor_cuts_a_silent_producer(rig, monkeypatch, ts_bytes):
    client, svc, state = rig
    waiting, stopped = threading.Event(), threading.Event()

    class Stream(io.BytesIO):
        def read1(self, _size):
            data = self.read()
            if data:
                return data
            waiting.set()
            return b"" if stopped.wait(5) else pytest.fail("Producer was left running after privacy changed")

    proc = SimpleNamespace(stdout=Stream(ts_bytes), stderr=io.BytesIO(), pid=101,
                           terminate=stopped.set, poll=lambda: -15 if stopped.is_set() else None,
                           wait=lambda **_: -15)
    monkeypatch.setattr("baird_hoard.capture.subprocess.Popen", lambda *_, **__: proc)
    monkeypatch.setattr("baird_hoard.capture.bind_to_app", lambda _: None)
    monkeypatch.setattr(SyntheticSource, "command", lambda *_: ["synthetic-double"])

    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine = svc.engine
    with engine.request():
        assert waiting.wait(5)
        assert engine.snapshot()["state"] == "running" and engine.buffer.latest()[1]
        state.policy = ScreenPolicy(True, ())
        # The supervisor has to stop the producer before another byte or a picture request.
        assert stopped.wait(2)
        status = client.get("/api/status").json()
        assert status["capture"]["state"] == "off" and status["capture"]["buffered_seconds"] == 0
        assert status["source"]["blocked"] == "private"
        assert call(client, svc, "screen_look", {"save": False}).json()["code"] == "no_signal"
        assert client.post("/api/snapshot").status_code == 409
        assert client.post("/api/clip").status_code == 409
        assert client.post("/api/record/start").status_code == 409


def test_picture_request_rechecks_policy_even_with_recent_data(rig, monkeypatch, ts_bytes):
    client, svc, state = rig
    engine = svc.engine
    monkeypatch.setattr(engine, "_start", lambda: None)
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine.buffer.feed(ts_bytes)
    engine._set(state="running")
    state.policy = ScreenPolicy(True, ())
    assert call(client, svc, "screen_look", {"save": False}).json()["code"] == "no_signal"
    assert engine.buffer.latest() == (b"", 0)


def test_revoke_during_snapshot_discards_the_decoded_file(rig, monkeypatch, ts_bytes):
    client, svc, state = rig
    engine = svc.engine
    monkeypatch.setattr(engine, "_start", lambda: None)
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine.buffer.feed(ts_bytes)
    engine._set(state="running")

    outputs = []

    def decode(args, **_):
        dest = Path(args[-1])
        dest.write_bytes(b"decoded")
        outputs.append(dest)
        state.policy = ScreenPolicy(True, ())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(svc.library, "_run", decode)
    response = client.post("/api/snapshot")
    assert response.status_code == 409
    assert len(outputs) == 1 and not outputs[0].exists()
    assert engine.buffer.latest() == (b"", 0)
