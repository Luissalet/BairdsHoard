"""The capture engine never waits on a helper without a time limit, and a device that stops responding is reported as
``stuck`` with the replug hint instead of an endless error."""

from __future__ import annotations

import sys
import time

import pytest

from baird_hoard import capture
from baird_hoard.capture import CaptureEngine
from baird_hoard.config import Settings
from baird_hoard.native import STUCK_HINT
from baird_hoard.tsbuf import TsBuffer

HANG_AFTER_RUN_FAILED = (
    "import sys, time\n"
    "sys.stderr.write('{\"event\":\"error\",\"code\":\"run_failed\",\"message\":\"IMediaControl.Run 0x8007001F\"}\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(600)\n"
)
GRAPH_FAILED = (
    "import sys\n"
    "sys.stderr.write('{\"event\":\"error\",\"code\":\"graph_failed\",\"message\":\"no encoder\"}\\n')\n"
    "sys.exit(3)\n"
)
SILENT = "import time\ntime.sleep(600)\n"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "EXIT_GRACE_S", 0.5)
    monkeypatch.setattr(capture, "START_LIMIT_S", 1.5)
    monkeypatch.setattr(capture, "bind_to_app", lambda proc: None)
    eng = CaptureEngine(Settings(tmp_path / "settings.json"), TsBuffer(keep_seconds=10), fake=True)
    yield eng
    eng.stop()


def use_script(monkeypatch, eng, code: str) -> None:
    monkeypatch.setattr(eng, "_command", lambda: [sys.executable, "-c", code])


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "capture state did not arrive"
        time.sleep(0.01)


def test_helper_hanging_after_run_failure_is_killed_and_reported_stuck(engine, monkeypatch):
    use_script(monkeypatch, engine, HANG_AFTER_RUN_FAILED)
    t0 = time.time()
    engine._demands.add("test")
    engine._run_once()
    assert time.time() - t0 < 8
    snap = engine.snapshot()
    assert snap["state"] == "stuck"
    assert snap["hint"] == STUCK_HINT
    assert engine._proc is None


def test_repeated_graph_failures_become_stuck(engine, monkeypatch):
    use_script(monkeypatch, engine, GRAPH_FAILED)
    engine._demands.add("test")
    engine._run_once()
    assert engine.snapshot()["state"] == "error"
    engine._run_once()
    assert engine.snapshot()["state"] == "error"
    engine._run_once()
    assert engine.snapshot()["state"] == "stuck"


def test_helper_that_never_sends_anything_is_killed(engine, monkeypatch):
    use_script(monkeypatch, engine, SILENT)
    engine._demands.add("test")
    t0 = time.time()
    engine._run_once()
    assert time.time() - t0 < 8
    assert engine._proc is None
    assert engine.snapshot()["state"] in ("error", "stuck")


NO_SIGNAL = (
    "import sys\n"
    "print('{\"event\":\"error\",\"code\":\"no_data\",\"message\":\"No HDMI data\"}', file=sys.stderr)\n"
    "sys.exit(4)\n"
)


def test_no_signal_backs_off_and_only_retries_with_demand(engine, monkeypatch):
    assert capture.RETRY_DELAYS_S == (15, 60, 300, 900)
    monkeypatch.setattr(capture, "RETRY_DELAYS_S", (0.4, 0.8, 1.2))
    use_script(monkeypatch, engine, NO_SIGNAL)

    with engine.request():
        wait_for(lambda: engine.snapshot()["restarts"] == 1 and engine.snapshot()["retry_in_s"] > 0)
        assert engine.snapshot()["state"] == "no_signal"
        first_wait = engine.snapshot()["retry_in_s"]
        wait_for(lambda: engine.snapshot()["restarts"] == 2 and engine.snapshot()["retry_in_s"] > 0.5)
        assert engine.snapshot()["retry_in_s"] > first_wait
        engine.retry_now()
        wait_for(lambda: engine.snapshot()["restarts"] == 3 and engine.snapshot()["retry_in_s"] > 1)
    time.sleep(1.25)
    engine.retry_now()
    assert engine.snapshot()["restarts"] == 3


def test_hourly_limit_covers_manual_retries_and_survives_restart(engine, monkeypatch):
    assert capture.OPEN_LIMIT == 12 and capture.OPEN_WINDOW_S == 3600
    monkeypatch.setattr(capture, "OPEN_LIMIT", 2)
    use_script(monkeypatch, engine, NO_SIGNAL)
    with engine.request():
        wait_for(lambda: engine.snapshot()["retry_in_s"] > 0)
        engine.retry_now()
        wait_for(lambda: engine.snapshot()["restarts"] == 2 and engine.snapshot()["retry_in_s"] > 0)
        engine.retry_now()
        wait_for(lambda: engine.snapshot()["state"] == "rate_limited")
        engine.retry_now()
        assert engine.snapshot()["retry_in_s"] > 3500
        assert engine.snapshot()["restarts"] == 2
    engine.stop()

    with monkeypatch.context() as restart:
        restart.setattr(capture, "OPEN_WINDOW_S", 1)
        time.sleep(1.1)
        assert engine._reserve_opening() == 0
        assert engine._reserve_opening() == 0
        assert engine._reserve_opening() > 0
    fresh = CaptureEngine(engine.settings, TsBuffer(keep_seconds=10), fake=True)
    use_script(monkeypatch, fresh, NO_SIGNAL)
    try:
        with fresh.request():
            wait_for(lambda: fresh.snapshot()["state"] == "rate_limited")
            assert fresh.snapshot()["restarts"] == 0
    finally:
        fresh.stop()


def test_only_last_consumer_releases_the_helper_after_grace(engine, monkeypatch):
    monkeypatch.setattr(capture, "IDLE_GRACE_S", 0.4)
    monkeypatch.setattr(capture, "START_LIMIT_S", 5)
    use_script(monkeypatch, engine, SILENT)
    engine.settings_changed()
    assert engine.snapshot()["state"] == "idle"
    assert engine._thread is None and not engine._budget_path.exists()

    with engine.request():
        engine.set_demand("viewer", True)
        wait_for(lambda: engine._proc is not None)
        proc = engine._proc
    time.sleep(0.5)
    assert proc.poll() is None
    engine.set_demand("viewer", False)
    assert proc.poll() is None
    wait_for(lambda: engine.snapshot()["state"] == "idle" and engine._proc is None)
    assert proc.poll() is not None
