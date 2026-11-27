"""Desktop topology, encoder selection and live producer with device-free inputs."""

from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird_hoard import capture
from baird_hoard.config import Config
from baird_hoard.errors import BairdError
from baird_hoard.main import create_app
from baird_hoard.monitor_source import DesktopMonitor, MonitorSource, layout_id, selected_monitor
from baird_hoard.monitor_worker import SOFTWARE, encoder_order, ffmpeg_command, run_capture
from baird_hoard.services import Services

from .conftest import FFMPEG

MONITOR = DesktopMonitor("panel-left", "Panel", -160, 0, 160, 90, 1, 2, 0, 0x10DE)
SECOND = DesktopMonitor("panel-right", "Panel", 0, 0, 160, 90, 1, 0, 0, 0x8086)
PORT = 17525
CAPTURE_CONTROL = "/api/settings/capture-control"


def choose(monitor, inventory):
    return {"id": monitor.id, "layout": layout_id(inventory)}


def capture_control_headers(client):
    return {"origin": str(client.base_url).rstrip("/"), "sec-fetch-site": "same-origin"}


def confirm_test_shortcut(key, cancelled, registered):
    registered.set()
    return 6 <= key <= 11 and not cancelled.is_set()


def set_local_capture(client, enabled):
    headers = capture_control_headers(client)
    issued = client.post(CAPTURE_CONTROL, headers=headers)
    assert issued.status_code == 200 and issued.json()["ticket"], issued.text
    changed = client.put(CAPTURE_CONTROL, headers=headers, json={"ticket": issued.json()["ticket"], "enabled": enabled})
    assert changed.status_code == 200 and changed.json()["active"] is enabled, changed.text


def test_status_drops_stale_monitor_levels_and_zeros_them_on_stop(tmp_path):
    settings = capture.Settings(tmp_path / "settings.json", isolated_instance=True)
    settings.update({"capture_enabled": True})
    source = MonitorSource("double-ffmpeg", tmp_path)
    engine = capture.CaptureEngine(settings, capture.TsBuffer(), source=source, demand_only=True)
    engine._set(state="running", audio_level=0.1831, microphone_level=0.1, level_monotonic=time.monotonic())
    assert engine.snapshot()["audio_level"] == 0.1831

    engine._set(state="running", audio_level=0.1831, microphone_level=0.1, level_monotonic=time.monotonic() - 5)
    assert engine.snapshot()["audio_level"] == 0.0
    assert engine.snapshot()["microphone_level"] == 0.0

    engine._set(state="running", audio_level=0.1831, microphone_level=0.1, level_monotonic=time.monotonic())
    engine._set(state="idle")
    assert engine.snapshot()["audio_level"] == 0.0
    assert engine.snapshot()["microphone_level"] is None


def make_small_transport(path, *, microphone, seconds):
    command = ffmpeg_command(FFMPEG, MONITOR, SOFTWARE, microphone,
                             video="testsrc2=size=160x90:rate=30,format=yuv420p[desktop]", device=False)
    channels = [1200, -900] + ([2200, -1800] if microphone else [])
    pcm = b"".join(struct.pack("<" + "h" * len(channels), *(x * sign for x in channels))
                   for sign in ([1] * 60 + [-1] * 60))
    pcm *= int(seconds * 400 + 1)
    subprocess.run([*command[:-1], "-t", str(seconds), str(path)], input=pcm,
                   capture_output=True, check=True, timeout=15, creationflags=capture.NO_WINDOW)
    return path.read_bytes()


def test_selection_uses_physical_identity_and_entire_layout():
    chosen = choose(MONITOR, [MONITOR, SECOND])
    reenumerated = replace(MONITOR, adapter=0, output=1)
    assert selected_monitor(chosen, [SECOND, reenumerated]) == reenumerated
    for changed in ([SECOND], [MONITOR, replace(SECOND, x=40)], [replace(MONITOR, width=320), SECOND],
                    [replace(MONITOR, id="replacement"), SECOND], [MONITOR, MONITOR]):
        with pytest.raises(BairdError, match="Choose a monitor again") as error:
            selected_monitor(chosen, changed)
        assert error.value.code == "monitor_reselect"


def test_hardware_selection_respects_display_adapter_and_mic_is_optional():
    known = {"h264_nvenc", "h264_amf", "h264_qsv", SOFTWARE}
    assert encoder_order(MONITOR, known) == ["h264_nvenc", SOFTWARE]
    assert encoder_order(SECOND, known) == ["h264_qsv", SOFTWARE]
    assert encoder_order(replace(SECOND, vendor=0x1002), known) == ["h264_amf", SOFTWARE]
    assert encoder_order(MONITOR, {"h264_qsv", SOFTWARE}) == [SOFTWARE]
    options = ffmpeg_command("ffmpeg", MONITOR, "h264_nvenc", False)
    assert "d3d11va=display:2" in options and "-forced-idr" in options
    assert options[options.index("-i") - 1] == "avi"
    assert options[options.index("-i") + 1] == "pipe:0"
    assert "hwupload" in options[options.index("-filter_complex") + 1]
    assert all("ddagrab" not in option for option in options)
    assert "pan=" not in options[options.index("-filter_complex") + 1]
    opts_with_mic = ffmpeg_command("ffmpeg", SECOND, "h264_qsv", True)
    filters = opts_with_mic[opts_with_mic.index("-filter_complex") + 1]
    assert "qsv=encoder@display" in opts_with_mic and "c0=c2|c1=c3[mic]" in filters
    assert "-forced_idr" in opts_with_mic


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    path = tmp_path_factory.mktemp("monitor-ts") / "pattern.ts"
    return path, make_small_transport(path, microphone=True, seconds=3.1)


def test_failed_hardware_start_does_not_publish_a_second_transport(stream):
    _, payload = stream
    stop = threading.Event()
    reports, opened, closed = [], [], []

    class Attempt:
        levels = {}

        def __init__(self, chunks):
            self.chunks = iter(chunks)

        def chunk(self, timeout):
            return next(self.chunks)

        def failure(self):
            return "encoder unavailable"

    class Sink(io.BytesIO):
        def write(self, data):
            result = super().write(data)
            stop.set()
            return result

    @contextmanager
    def attempt(monitor, encoder, microphone):
        opened.append((monitor.id, encoder, microphone))
        try:
            yield Attempt([b"failed bytes", b""] if encoder == "h264_nvenc" else [payload])
        finally:
            closed.append(encoder)

    sink = Sink()
    run_capture(lambda: MONITOR, {"h264_nvenc", SOFTWARE}, attempt, True, stop, sink,
                lambda event, **meta: reports.append((event, meta)))
    assert opened == [(MONITOR.id, "h264_nvenc", True), (MONITOR.id, SOFTWARE, True)]
    assert closed == ["h264_nvenc", SOFTWARE]
    assert sink.getvalue() == payload
    assert [kind for kind, _ in reports] == ["warning", "ready"]
    assert reports[-1][1]["encoder"] == SOFTWARE


def test_topology_changes_before_first_frame_block_publication(stream):
    _, payload = stream
    inventory = [MONITOR]
    selection = choose(MONITOR, inventory)
    sink = io.BytesIO()

    def resolve():
        return selected_monitor(selection, inventory)

    class Attempt:
        levels = {}

        def chunk(self, timeout):
            inventory[0] = replace(MONITOR, y=25)
            return payload

    @contextmanager
    def attempt(monitor, encoder, microphone):
        yield Attempt()

    with pytest.raises(BairdError) as error:
        run_capture(resolve, {SOFTWARE}, attempt, False, threading.Event(), sink)
    assert error.value.code == "monitor_reselect"
    assert not sink.getvalue()


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not available")
def test_synthetic_screen_and_audio_follow_the_monitor_transport_contract(stream):
    path, _ = stream
    data = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_frames", "-show_format", "-of", "json", str(path)],
        creationflags=capture.NO_WINDOW, timeout=10,
    ))
    assert data["format"]["format_name"] == "mpegts"
    video, system, mic = data["streams"]
    assert (video["codec_name"], video["r_frame_rate"], video["width"], video["height"]) == ("h264", "30/1", 160, 90)
    assert [(a["codec_name"], a["sample_rate"], a["channels"]) for a in (system, mic)] == [("aac", "48000", 2)] * 2
    timestamps = [float(f["best_effort_timestamp_time"]) for f in data["frames"] if
                  f["media_type"] == "video" and f["key_frame"]]
    assert [t - timestamps[0] for t in timestamps] == pytest.approx([0, 1, 2, 3], abs=0.001)


def wait_until(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("expected state did not arrive")


def test_monitor_worker_starts_from_checkout_without_package_install(tmp_path, monkeypatch):
    # The parent sees the selected display. The real child entrypoint sees only a
    # fake inventory, where the selection has disappeared, and exits before audio.
    doubles = tmp_path / "doubles"
    doubles.mkdir()
    child_cwd = tmp_path / "worker-cwd.txt"
    (doubles / "sitecustomize.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from types import ModuleType\n"
        "\n"
        "def desktop_inventory():\n"
        f"    Path({str(child_cwd)!r}).write_text(str(Path.cwd()), encoding='utf-8')\n"
        "    return []\n"
        "\n"
        "module = ModuleType('baird_hoard.monitor_windows')\n"
        "module.desktop_inventory = desktop_inventory\n"
        "sys.modules[module.__name__] = module\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("PYTHONPATH", str(doubles))
    monkeypatch.setattr(capture, "ensure_helper", lambda: pytest.fail("unexpected HDMI helper"))
    source = MonitorSource(str(tmp_path / "unused-ffmpeg"), tmp_path, lambda: [MONITOR])
    profile = Config(data_dir=tmp_path / "profile", port=PORT, monitor_capture=True,
                     isolated_instance=True, background=False)
    svc = Services(profile, monitor_source=source)
    svc.settings.update({"capture_enabled": True, "monitor": choose(MONITOR, [MONITOR])})
    try:
        with svc.engine.request():
            with pytest.raises(BairdError) as error:
                svc.engine.wait_for_picture()
        assert error.value.code == "monitor_reselect", svc.engine.snapshot()
        assert Path(child_cwd.read_text(encoding="utf-8")) == repo_root
    finally:
        svc.stop()


def test_services_open_only_on_demand_and_require_a_new_selection(stream, tmp_path, monkeypatch):
    path, _ = stream
    starts = []
    inventory = [MONITOR]
    shim = tmp_path / "mock_monitor.py"
    shim.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "data=Path(sys.argv[1]).read_bytes()\n"
        "sys.stdout.buffer.write(data[:32768]);sys.stdout.buffer.flush()\n"
        "time.sleep(0.2)\n"
        "print(json.dumps({'event':'ready','device':'lab','encoder':'mock',"
        "'input':{'width':160,'height':90}}),file=sys.stderr,flush=True)\n"
        "sys.stdout.buffer.write(data[32768:]);sys.stdout.buffer.flush()\n"
        "for line in sys.stdin:\n"
        "    if 'quit' in line: break\n", encoding="utf-8",
    )

    def producer(ffmpeg, selection, microphone, work_dir):
        starts.append((selection, microphone))
        return [sys.executable, "-B", str(shim), str(path)]

    monkeypatch.setattr(capture, "IDLE_GRACE_S", 0.1)
    monkeypatch.setattr(capture, "ensure_helper", lambda: pytest.fail("unexpected HDMI helper"))
    source = MonitorSource(FFMPEG, tmp_path, lambda: inventory, producer)
    profile = Config(data_dir=tmp_path / "profile", port=PORT, monitor_capture=True, isolated_instance=True, background=False)
    svc = Services(profile, monitor_source=source, confirm_capture=confirm_test_shortcut)
    svc.settings.update({"capture_always_on": True, "ocr_auto": False})

    with TestClient(create_app(profile, svc), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        catalog = client.get("/api/monitors").json()
        selection = catalog["monitors"][0]["selection"]
        assert catalog["enabled"] and client.get("/api/devices").json()["devices"] == []
        assert client.put("/api/settings", json={"monitor": selection}).status_code == 200
        set_local_capture(client, True)
        assert not starts and svc.engine._thread is None

        with svc.engine.request():
            svc.engine.wait_for_picture()
            assert svc.engine.snapshot()["device"] == "lab"
            assert svc.engine.snapshot()["encoder"] == "mock"
        wait_until(lambda: svc.engine.snapshot()["state"] == "idle")
        assert starts == [(selection, False)] and svc.engine._proc is None

        inventory[0] = replace(MONITOR, x=-200)
        with svc.engine.request():
            with pytest.raises(BairdError) as error:
                svc.engine.wait_for_picture()
            assert error.value.code == "monitor_reselect"
        assert len(starts) == 1
        inventory[0] = MONITOR
        with svc.engine.request():
            with pytest.raises(BairdError, match="Choose a monitor again"):
                svc.engine.wait_for_picture()
        assert client.put("/api/settings", json={"monitor": selection, "monitor_microphone": True}).status_code == 200
        assert not svc.sources.status()["active"] and len(starts) == 1
        set_local_capture(client, True)
        with svc.engine.request():
            svc.engine.wait_for_picture()
        wait_until(lambda: svc.engine.snapshot()["state"] == "idle")
        assert starts[-1] == (selection, True) and len(starts) == 2


def test_monitor_catalog_is_disabled_in_the_default_profile(tmp_path):
    profile = Config(data_dir=tmp_path, port=PORT, isolated_instance=True, background=False)
    source = MonitorSource(None, tmp_path, lambda: pytest.fail("unexpected display enumeration"))
    svc = Services(profile, monitor_source=source)
    with TestClient(create_app(profile, svc), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/monitors").json() == {"enabled": False, "monitors": []}
