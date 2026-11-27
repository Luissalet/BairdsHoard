from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

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
    state = SimpleNamespace(monitors=[DISPLAY], policy=ScreenPolicy(False, ()), apps=["math.exe"], opens=[], callbacks=[])
    cfg = Config(data_dir=tmp_path, port=17529, data_dir_configured=True, synthetic_capture=True,
                 background=False, isolated_instance=True)
    monkeypatch.setattr("baird_hoard.capture.ensure_helper", lambda: pytest.fail("Native helper called"))
    monkeypatch.setattr("baird_hoard.api.ui.list_devices", lambda: pytest.fail("Native devices listed"))
    monkeypatch.setenv("HOARD_EVENTS", "0")

    def producer(display):
        state.opens.append(display.id)
        return SyntheticSource(None)

    def subscribe(display_id, revoked):
        assert display_id in {display.id for display in state.monitors}
        state.callbacks.append(revoked)
        return lambda: state.callbacks.remove(revoked)

    svc = Services(cfg, screen_backend=ScreenBackend(lambda: state.monitors, producer, lambda _: state.apps),
                   read_screen_policy=lambda: state.policy, subscribe_screen_policy=subscribe)
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


def test_privacy_cut_drops_transport_data_before_listeners(rig):
    client, svc, state = rig
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
    waiting, stopped, release_frame = threading.Event(), threading.Event(), threading.Event()
    frames = []

    class Stream(io.BytesIO):
        def read1(self, _size):
            data = self.read()
            if data:
                frames.append("initial")
                return data
            waiting.set()
            assert release_frame.wait(5), "The test did not release the blocked producer"
            if stopped.is_set():
                return b""
            frames.append("after cutoff")
            return ts_bytes

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
        try:
            assert stopped.wait(2)
        finally:
            release_frame.set()
        status = client.get("/api/status").json()
        assert frames == ["initial"]
        assert status["capture"]["state"] == "off" and status["capture"]["buffered_seconds"] == 0
        assert status["source"]["blocked"] == "private"
        assert call(client, svc, "screen_look", {"save": False}).json()["code"] == "no_signal"
        assert client.post("/api/snapshot").status_code == 409
        assert client.post("/api/clip").status_code == 409
        assert client.post("/api/record/start").status_code == 409


def test_private_policy_blocks_the_first_local_read(rig):
    client, svc, state = rig
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine = svc.engine
    reads = []

    def frame(_):
        reads.append("frame")
        return b""

    proc = SimpleNamespace(stdout=SimpleNamespace(read1=frame))
    engine._proc = proc
    try:
        state.policy = ScreenPolicy(True, ())
        engine._pump(proc, {"got_data": False}, engine.local_source())
    finally:
        engine._proc = None
    assert reads == []
    assert svc.sources.status()["blocked"] == "private"


@pytest.mark.parametrize(("changed_policy", "blocked"), [
    (ScreenPolicy(True, ()), "private"),
    (ScreenPolicy(False, ("math.exe",)), "excluded"),
    (None, "policy_unavailable"),
])
def test_policy_change_between_reads_blocks_next_frame(rig, monkeypatch, changed_policy, blocked):
    client, svc, state = rig
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine = svc.engine
    local = engine.local_source()
    reads, delivered = [], []

    def read_frame(_):
        reads.append(len(reads) + 1)
        return b"frame-" + bytes([reads[-1]]) if reads[-1] <= 2 else b""

    def receive(frame, *_):
        delivered.append(frame)
        # Change the authority after the first ready frame, in the same pump turn.
        # No subscription callback or watchdog tick is needed to deny the next read.
        state.policy = changed_policy

    monkeypatch.setattr(engine.buffer, "feed", lambda data: (data, [0]))
    monkeypatch.setattr(engine.buffer, "latest", lambda: (b"keyframe", 1))
    engine.add_listener(receive)
    proc = SimpleNamespace(stdout=SimpleNamespace(read1=read_frame))
    engine._proc = proc
    try:
        engine._pump(proc, {"got_data": False}, local)
    finally:
        engine._proc = None
    assert reads == [1]
    assert delivered == [b"frame-\x01"]
    assert svc.sources.status()["blocked"] == blocked
    assert not svc.sources.status()["active"]


@pytest.mark.parametrize(("reason", "blocked"), [
    ("private", "private"), ("excluded", "excluded"), (None, "policy_unavailable"),
])
def test_subscription_revokes_even_during_a_policy_read(rig, monkeypatch, reason, blocked):
    client, svc, state = rig
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine = svc.engine
    local = engine.local_source()
    reading, release, stopped = threading.Event(), threading.Event(), threading.Event()
    reads = []

    def stale_policy():
        reading.set()
        assert release.wait(8)
        return ScreenPolicy(False, ())

    monkeypatch.setattr(local.gate, "_read_policy", stale_policy)
    proc = SimpleNamespace(stdout=SimpleNamespace(read1=lambda _: reads.append("frame")),
                           poll=lambda: -15 if stopped.is_set() else None, terminate=stopped.set)
    engine._proc, engine._proc_local = proc, local
    try:
        with ThreadPoolExecutor(max_workers=2) as worker:
            pending = worker.submit(engine._pump, proc, {"got_data": False}, local)
            try:
                assert reading.wait(5)
                # The callback must finish before the stale positive reply is released.
                worker.submit(state.callbacks[0], reason).result(timeout=2)
                assert stopped.is_set() and not pending.done()
                source = svc.sources.status()
                assert source["blocked"] == blocked and not source["active"]
                denied = worker.submit(call, client, svc, "screen_look", {"save": False}).result(timeout=2)
                assert denied.status_code == 409 and denied.json()["code"] == "no_signal"
            finally:
                release.set()
            pending.result(timeout=3)
    finally:
        engine._proc = engine._proc_local = None
    assert reads == []


def test_source_switch_releases_subscription_and_ignores_its_late_event(rig):
    client, svc, state = rig
    assert choose(client).is_success
    assert activate(client).json()["active"]
    old = state.callbacks[0]
    assert choose(client, "overlay").is_success
    assert state.callbacks == []
    assert activate(client).json()["active"]
    assert len(state.callbacks) == 1
    old("private")
    assert svc.sources.status()["active"]
    state.callbacks[0]("excluded")
    assert svc.sources.status()["blocked"] == "excluded"
    assert activate(client, False).is_success
    assert state.callbacks == []


def test_failed_subscription_keeps_local_source_off(rig, monkeypatch):
    client, svc, state = rig

    def unavailable(*_):
        raise ConnectionError("test Hub subscription failed")

    monkeypatch.setattr(svc.sources, "_subscribe_policy", unavailable)
    assert choose(client).is_success
    result = activate(client)
    assert result.status_code == 409 and result.json()["code"] == "policy_unavailable"
    assert not svc.sources.status()["active"] and state.opens == []


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


@pytest.mark.parametrize(("kind", "file_prefix", "suffix"), [
    ("snapshot", "Captura", ".png"),
    ("clip", "Clip", ".mp4"),
    ("look", "Captura", ".png"),
])
@pytest.mark.parametrize("revoke", [False, True])
def test_in_flight_media_stays_out_of_library(rig, monkeypatch, ts_bytes, kind, file_prefix, suffix, revoke):
    client, svc, state = rig
    engine = svc.engine
    monkeypatch.setattr(engine, "_start", lambda: None)
    monkeypatch.setattr("baird_hoard.media._stamp", lambda: "2000-01-01 00.00.00")
    monkeypatch.setattr(svc.library, "_probe", lambda _: {"duration": 1})
    assert choose(client).status_code == 200
    assert activate(client).json()["active"]
    engine.buffer.feed(ts_bytes)
    engine._set(state="running", input={"width": 640, "height": 360})
    existing = svc.library.folder / "published.png"
    existing.write_bytes(b"previously published")
    output = svc.library._new_path(file_prefix, suffix, " (1 s)" if kind == "clip" else "")
    started, release = threading.Event(), threading.Event()
    staged = []

    def encode(args, **_):
        if kind == "look" and args[-1] == "pipe:1":
            # The model JPEG is still in flight after the PNG has been prepared.
            started.set()
            assert release.wait(8)
            return SimpleNamespace(returncode=0, stdout=b"synthetic jpeg")
        dest = Path(args[-1])
        dest.write_bytes(b"pending media")
        staged.append(dest)
        if kind != "look":
            started.set()
            assert release.wait(8)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(svc.library, "_run", encode)
    with ThreadPoolExecutor(max_workers=1) as worker:
        if kind == "look":
            saving = worker.submit(call, client, svc, "screen_look", {"save": True})
        else:
            payload = {"seconds": 1} if kind == "clip" else {}
            saving = worker.submit(client.post, "/api/" + kind, json=payload)
        try:
            assert started.wait(5)
            assert staged[0].is_file() and not saving.done()
            if revoke:
                state.policy = ScreenPolicy(True, ())
                assert client.post("/api/record/start").status_code == 409
                assert client.get("/api/status").json()["source"]["blocked"] == "private"
            assert {p["name"] for p in client.get("/api/media").json()["items"]} == {existing.name}
            for name in (output.name, staged[0].name):
                assert client.get("/api/files/" + quote(name)).status_code == 404
                assert client.get("/media/" + quote(name)).status_code == 404
            assert client.get("/api/files/" + existing.name).content == b"previously published"
        finally:
            release.set()
        response = saving.result(timeout=5)

    assert not staged[0].exists()
    if revoke:
        assert response.status_code == 409, response.text
        assert list(svc.library.folder.iterdir()) == [existing]
    else:
        assert response.status_code == 200, response.text
        saved = response.json()["name"]
        assert client.get("/api/files/" + quote(saved)).content == b"pending media"
        assert {p["name"] for p in client.get("/api/media").json()["items"]} == {existing.name, saved}
