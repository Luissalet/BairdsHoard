"""The monitor's device pulls share the privacy authority's cutoff."""

from __future__ import annotations

import io
import json
import queue
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from baird_hoard import capture, monitor_worker
from baird_hoard.config import Settings
from baird_hoard.errors import BairdError
from baird_hoard.monitor_input import AudioGrants, feed_samples
from baird_hoard.monitor_privacy import AcquisitionGate, PolicySnapshot
from baird_hoard.tsbuf import TsBuffer

from .conftest import FFMPEG
from .test_monitor_source import MONITOR, choose


class HubDouble:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = PolicySnapshot(1, False, False, False)
        self.reads = 0
        self.online = True

    def read(self, monitor, layout):
        self.reads += 1
        assert (monitor, layout) == (MONITOR.id, choose(MONITOR, [MONITOR])["layout"])
        with self.lock:
            if not self.online:
                raise ConnectionError("policy service offline")
            return self.state

    @contextmanager
    def lease(self, monitor, layout, revision, medium):
        with self.lock:
            state = self.state
            yield (self.online and revision == state.revision and not state.private and not state.excluded
                   and not (medium == "audio" and state.audio_excluded))

    def change(self, **fields):
        with self.lock:
            self.state = replace(self.state, revision=self.state.revision + 1, **fields)


def guard(hub):
    return AcquisitionGate(hub, choose(MONITOR, [MONITOR]), clock=lambda: 0.0)


@pytest.mark.parametrize(("setting", "medium", "code"), [
    ("private", "video", "privacy_blocked"),
    ("excluded", "video", "application_excluded"),
    ("excluded", "audio", "application_excluded"),
    ("audio_excluded", "audio", "application_excluded"),
])
def test_cached_positive_cannot_take_another_sample_after_cut(setting, medium, code):
    hub = HubDouble()
    gate = guard(hub)
    samples = []
    gate.acquire(medium, lambda: samples.append(medium))
    gate.acquire(medium, lambda: samples.append(medium))
    assert hub.reads == 1

    hub.change(**{setting: True})
    with pytest.raises(BairdError) as error:
        gate.acquire(medium, lambda: samples.append("after cutoff"))
    assert error.value.code == code and samples == [medium, medium]
    hub.change(**{setting: False})
    with pytest.raises(BairdError):
        gate.acquire(medium, lambda: samples.append("reopened"))
    assert samples == [medium, medium] and hub.reads == 2


@pytest.mark.parametrize("offline", [True, False])
def test_unknown_or_unconfirmed_policy_fails_before_a_device(offline):
    hub = HubDouble() if offline else None
    gate = guard(hub)
    if hub is not None:
        gate.preflight()
        hub.online = False
    with pytest.raises(BairdError) as error:
        gate.acquire("video", lambda: pytest.fail("physical sample"))
    assert error.value.code == "policy_unavailable"


def test_cached_metadata_refreshes_and_unknown_policy_does_not_open_input():
    hub = HubDouble()
    ticks = [0.0]
    gate = AcquisitionGate(hub, choose(MONITOR, [MONITOR]), clock=lambda: ticks[0])
    gate.preflight()
    ticks[0] += 0.099
    gate.preflight()
    assert hub.reads == 1
    ticks[0] += 0.001
    hub.online = False
    with pytest.raises(BairdError, match="could not be confirmed"):
        gate.acquire("video", lambda: pytest.fail("physical sample"))
    assert hub.reads == 2


def test_delayed_read_still_needs_a_fresh_lease_before_every_pull():
    clock = [0.0]

    class DelayedHub(HubDouble):
        def read(self, monitor, layout):
            snapshot = super().read(monitor, layout)
            clock[0] += 0.12
            return snapshot

    hub = DelayedHub()
    gate = AcquisitionGate(hub, choose(MONITOR, [MONITOR]), clock=lambda: clock[0])
    assert gate.acquire("video", lambda: "first frame") == "first frame"
    hub.change(private=True)
    with pytest.raises(BairdError, match="Private mode"):
        gate.acquire("video", lambda: pytest.fail("acquired under the old revision"))
    assert hub.reads == 2


def test_revision_cutoff_serializes_with_an_inflight_acquisition():
    hub = HubDouble()
    gate = guard(hub)
    entered, finish, revoking = threading.Event(), threading.Event(), threading.Event()
    sequence = []

    def sample():
        entered.set()
        assert finish.wait(4)
        sequence.append("covered frame")

    def revoke():
        revoking.set()
        hub.change(private=True)
        sequence.append("cutoff")

    with ThreadPoolExecutor(max_workers=2) as pool:
        inside = pool.submit(gate.acquire, "video", sample)
        try:
            assert entered.wait(4)
            cut = pool.submit(revoke)
            assert revoking.wait(4)
            assert not cut.done()
        finally:
            finish.set()
        inside.result(4)
        cut.result(4)
    with pytest.raises(BairdError):
        gate.acquire("video", lambda: sequence.append("after cutoff"))
    assert sequence == ["covered frame", "cutoff"]


def test_feeder_checks_privacy_after_pcm_wait_and_before_every_frame():
    hub = HubDouble()
    gate = guard(hub)
    frames_taken = []
    out = io.BytesIO()

    class Pcm:
        reads = 0

        def read(self, n):
            self.reads += 1
            if self.reads == 2:
                hub.change(excluded=True)
            return bytes(n)

    @contextmanager
    def frames(display):
        def grab():
            frames_taken.append("acquired")
            return bytes(8 * 8 * 4)
        yield grab

    with pytest.raises(BairdError) as error:
        feed_samples(out, Pcm(), MONITOR, (8, 8), frames, gate, threading.Event(), 30, 2)
    assert error.value.code == "application_excluded" and frames_taken == ["acquired"]
    assert out.getvalue().count(b"00db") == 1


def test_native_audio_request_receives_no_grant_after_exclusion():
    hub = HubDouble()
    gate = guard(hub)
    messages = queue.Queue()
    first, closed = threading.Event(), threading.Event()
    acquisitions = []

    class PermitPipe:
        def write(self, grant):
            assert grant == b"1"
            acquisitions.append("GetBuffer")
            messages.put(b"@complete")
            first.set()

        def flush(self):
            pass

        def close(self):
            messages.put(b"")
            closed.set()

    audio = SimpleNamespace(stdin=PermitPipe(), stderr=iter(messages.get, b""))
    grants = AudioGrants(audio, gate)
    try:
        messages.put(b"@acquire")
        assert first.wait(4)
        hub.change(audio_excluded=True)
        messages.put(b"@acquire")
        assert closed.wait(4)
        assert len(acquisitions) == 1
        assert grants.failure.code == "application_excluded"
    finally:
        messages.put(b"")
        for thread in grants.threads:
            thread.join(timeout=4)
    assert not any(thread.is_alive() for thread in grants.threads)


@pytest.mark.parametrize("resolution", ["acknowledged", "exited"])
def test_timed_out_audio_grant_serializes_cutoff_even_when_kill_fails(monkeypatch, resolution):
    from baird_hoard import monitor_input

    monkeypatch.setattr(monitor_input, "AUDIO_DEADLINE", 0.05)
    hub = HubDouble()
    replies = queue.Queue()
    permitted, kill_failed, closed, exited = [threading.Event() for _ in range(4)]
    starting_cut, cutoff = threading.Event(), threading.Event()
    samples = []

    class Pipe:
        def write(self, grant):
            assert grant == b"1"
            permitted.set()

        def flush(self):
            pass

        def close(self):
            closed.set()
            replies.put(b"")

    def kill():
        kill_failed.set()
        raise PermissionError("synthetic termination failure")

    def cut():
        starting_cut.set()
        hub.change(private=True)
        cutoff.set()

    audio = SimpleNamespace(stdin=Pipe(), stderr=iter(replies.get, b""), kill=kill,
                            poll=lambda: 0 if exited.is_set() else None)
    grants = AudioGrants(audio, guard(hub))
    revoke = threading.Thread(target=cut, daemon=True)
    try:
        replies.put(b"@acquire")
        assert permitted.wait(4) and kill_failed.wait(4)
        revoke.start()
        assert starting_cut.wait(4)
        assert not cutoff.wait(0.2) and not closed.is_set()
        if resolution == "acknowledged":
            samples.append(("GetBuffer", hub.state.private))
            replies.put(b"@complete")
        else:
            exited.set()
        assert cutoff.wait(4) and closed.wait(4)
        assert isinstance(grants.failure, RuntimeError)
        assert samples == ([("GetBuffer", False)] if resolution == "acknowledged" else [])
        with pytest.raises(BairdError, match="Private mode"):
            guard(hub).acquire("audio", lambda: pytest.fail("acquired audio after cutoff"))
    finally:
        exited.set()
        replies.put(b"")
        for thread in grants.threads:
            thread.join(4)
        if revoke.ident is not None:
            revoke.join(4)
    assert not any(thread.is_alive() for thread in (*grants.threads, revoke))


def test_worker_declines_to_initialize_audio_or_encoder_without_policy(monkeypatch, capsys, tmp_path):
    import sys

    monkeypatch.setattr(monitor_worker, "windows_inventory", lambda: [MONITOR])
    monkeypatch.setattr(monitor_worker, "_listen_for_quit", lambda stop: None)
    monkeypatch.setattr(monitor_worker, "audio_helper", lambda *_: pytest.fail("opened audio"))
    monkeypatch.setattr(monitor_worker, "available_encoders", lambda *_: pytest.fail("opened encoder"))
    selection = choose(MONITOR, [MONITOR])
    monkeypatch.setattr(sys, "argv", ["worker", "--ffmpeg", "test", "--work-dir", str(tmp_path),
                                    "--selection", json.dumps(selection)])
    assert monitor_worker.main(frames=lambda *_: pytest.fail("opened screen")) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "policy_unavailable"


def test_engine_blocks_on_producer_notice_without_waiting_for_transport(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "settings.json", isolated_instance=True)
    settings.update({"capture_enabled": True})
    source = SimpleNamespace(initial_state=lambda: {"source": "monitor"})
    engine = capture.CaptureEngine(settings, TsBuffer(), source=source, demand_only=True)
    proc = SimpleNamespace(stderr=io.BytesIO(
        b'{"event":"error","code":"privacy_blocked","message":"Private mode"}\n'
    ))
    engine._proc = proc
    engine._demands.add("viewer")
    resets = []
    monkeypatch.setattr(engine.buffer, "reset", lambda: resets.append(True))
    engine._read_events(proc)
    assert resets == [True]
    assert engine.snapshot()["privacy"] == "privacy_blocked"
    assert not engine._wanted()
    with pytest.raises(BairdError) as error:
        engine.wait_for_picture()
    assert error.value.code == "privacy_blocked"
    settings.update({"capture_enabled": False})
    engine._proc = None
    engine.settings_changed()
    assert engine.snapshot()["state"] == "off" and not engine.snapshot().get("privacy")


def test_silent_encoder_still_observes_cut_and_drops_inflight_transport():
    hub = HubDouble()
    gate = guard(hub)
    sink = io.BytesIO()

    class Attempt:
        def chunk(self, timeout):
            hub.change(private=True)
            return b"transport encoded before cutoff"

    with pytest.raises(BairdError) as error:
        monitor_worker.stream_attempt(Attempt(), MONITOR, monitor_worker.SOFTWARE, lambda: MONITOR,
                                      threading.Event(), sink, lambda *_a, **_kw: None, gate)
    assert error.value.code == "privacy_blocked" and sink.getvalue() == b""


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
@pytest.mark.parametrize("microphone", [False, True])
def test_guarded_pipe_encodes_avi_to_keyed_transport_without_devices(microphone):
    gate = guard(HubDouble())
    monitor = replace(MONITOR, width=32, height=32)
    sink = io.BytesIO()

    @contextmanager
    def pixels(_monitor):
        yield lambda: b"\x00\xc2\x50\0" * (32 * 32)

    channels = 4 if microphone else 2
    pcm = io.BytesIO(bytes(48_000 * channels * 2 * 2))
    feed_samples(sink, pcm, monitor, (32, 32), pixels, gate, threading.Event(), 30, channels)
    cmd = monitor_worker.ffmpeg_command(FFMPEG, monitor, monitor_worker.SOFTWARE, microphone)
    assert "ddagrab" not in " ".join(cmd) and "-init_hw_device" not in cmd
    proc = subprocess.run(cmd, input=sink.getvalue(), capture_output=True, timeout=15, creationflags=capture.NO_WINDOW)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    buffer = TsBuffer()
    buffer.feed(proc.stdout)
    assert buffer.video_pid is not None and buffer.audio_pid is not None and buffer.latest()[1] > 0
