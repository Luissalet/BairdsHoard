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
    eng = CaptureEngine(Settings(tmp_path / "settings.json"), TsBuffer(keep_seconds=10))
    return eng


def use_script(monkeypatch, eng, code: str) -> None:
    monkeypatch.setattr(eng, "_command", lambda: [sys.executable, "-c", code])


def test_helper_hanging_after_run_failure_is_killed_and_reported_stuck(engine, monkeypatch):
    use_script(monkeypatch, engine, HANG_AFTER_RUN_FAILED)
    t0 = time.time()
    engine._run_once()
    assert time.time() - t0 < 8
    snap = engine.snapshot()
    assert snap["state"] == "stuck"
    assert snap["hint"] == STUCK_HINT
    assert engine._proc is None


def test_repeated_graph_failures_become_stuck(engine, monkeypatch):
    use_script(monkeypatch, engine, GRAPH_FAILED)
    engine._run_once()
    assert engine.snapshot()["state"] == "error"
    engine._run_once()
    assert engine.snapshot()["state"] == "error"
    engine._run_once()
    assert engine.snapshot()["state"] == "stuck"


def test_helper_that_never_sends_anything_is_killed(engine, monkeypatch):
    use_script(monkeypatch, engine, SILENT)
    t0 = time.time()
    engine._run_once()
    assert time.time() - t0 < 8
    assert engine._proc is None
    assert engine.snapshot()["state"] in ("error", "stuck")
