"""Agent source contract and activation boundary with synthetic doubles."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from baird_hoard import capture
from baird_hoard.agent_sources import SourceArgs, call_on_source
from baird_hoard.config import Config
from baird_hoard.errors import BairdError
from baird_hoard.main import create_app
from baird_hoard.monitor_source import MonitorSource
from baird_hoard.services import Services

from .conftest import FFMPEG
from .test_monitor_source import MONITOR, SECOND, make_small_transport

PORT = 17531
SOURCE = {"mode": "monitor", "monitor": MONITOR.id}


def agent(client, name, arguments=None, **body):
    return client.post("/api/agent/call", headers={"Authorization": f"Bearer {client.app.state.services.token}"},
                       json={"name": name, "arguments": arguments or {}, **body})


def human_settings(client, **body):
    result = client.put("/api/settings", json=body)
    assert result.status_code == 200, result.text
    return result.json()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    def unexpected_device(*args, **kwargs):
        pytest.fail("an agent tried to open a native device")

    monkeypatch.setattr(capture, "ensure_helper", unexpected_device)
    source = MonitorSource(FFMPEG or "ffmpeg", tmp_path, lambda: [MONITOR, SECOND], unexpected_device)
    config = Config(data_dir=tmp_path / "profile", port=PORT, monitor_capture=True,
                    isolated_instance=True, background=False)
    svc = Services(config, monitor_source=source)
    with TestClient(create_app(config, svc), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        yield client, svc


def test_contract_and_credential_boundary(profile):
    client, svc = profile
    choices = client.get("/api/agent/tools").json()
    tools = {row["name"]: row for row in choices["tools"]}
    for name in ("capture_status", "capture_retry", "screen_look", "snapshot_save", "clip_save",
                 "record_start", "record_pause", "record_resume", "record_stop",
                 "copilot_look", "copilot_show", "copilot_wait", "copilot_find", "copilot_status", "copilot_stop"):
        assert "source" in tools[name]["inputSchema"]["properties"], name
    assert tools["source_list"]["annotations"]["readOnlyHint"]
    select = tools["source_select"]["inputSchema"]
    assert set(select["properties"]) == {"mode", "monitor"} and select["additionalProperties"] is False
    assert "source_activate" not in tools

    unsigned = client.post("/api/agent/call", json={"name": "source_select", "arguments": SOURCE})
    assert unsigned.status_code in (401, 403) and svc.settings["monitor"] is None
    bad = agent(client, "source_select", {**SOURCE, "active": True})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_arguments"

    selected = agent(client, "source_select", SOURCE).json()
    assert selected["monitor"] == MONITOR.id and selected["active"] is False
    assert selected["undo_token"]

    signed_browser_route = client.put("/api/settings", json={"capture_enabled": True},
                                      headers={"Authorization": f"Bearer {svc.token}"})
    assert signed_browser_route.status_code == 403
    ui_call = client.post("/api/ui/call", json={"name": "settings_set", "arguments": {"capture_enabled": True}})
    assert ui_call.status_code == 403
    forged = agent(client, "settings_set", {"capture_enabled": True}, caller="ui")
    assert forged.status_code == 403 and forged.json()["code"] == "source_activation_required"
    assert not svc.settings["capture_enabled"] and svc.engine._thread is None


def test_disabled_source_gets_an_explicit_error_before_capture(profile):
    client, svc = profile
    active = agent(client, "source_select", SOURCE).json()
    assert active["active"] is False
    source_list = agent(client, "source_list").json()
    assert {item["id"] for item in source_list["monitors"]} == {MONITOR.id, SECOND.id}

    for name, args in {
        "screen_look": {}, "snapshot_save": {}, "clip_save": {}, "capture_retry": {}, "record_start": {},
        "record_resume": {}, "transcribe_start": {}, "watch_check": {"id": "example"},
        "copilot_look": {}, "copilot_show": {"steps": ["Pulsa aquí."], "wait_s": 0, "look": "never"},
        "copilot_wait": {"wait_s": 0}, "copilot_find": {"text": "Guardar"},
    }.items():
        response = agent(client, name, {**args, "source": SOURCE})
        assert response.status_code == 403 and response.json()["code"] == "source_off", (name, response.text)
        assert "_image" not in response.json()
    bench = agent(client, "enhance_benchmark", {"source": "live", "capture_source": SOURCE})
    assert bench.status_code == 403 and bench.json()["code"] == "source_off"

    implicit = agent(client, "copilot_look")
    assert implicit.status_code == 403 and implicit.json()["code"] == "source_off"
    mismatch = agent(client, "screen_look", {"source": {"mode": "monitor", "monitor": SECOND.id}})
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "source_mismatch"
    unavailable = agent(client, "source_select", {"mode": "overlay", "monitor": MONITOR.id})
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "capture_unavailable"
    assert agent(client, "capture_status", {"source": SOURCE}).json()["source"]["active"] is False

    for url in ("/copilot/look.jpg", "/copilot/look/not-a-look.jpg", "/copilot/text"):
        response = client.get(url)
        assert response.status_code == 403 and response.json()["code"] == "source_off"
    assert svc.copilot.status()["session"] == "none"
    assert agent(client, "copilot_stop", {"source": SOURCE}).status_code == 200
    stopped = agent(client, "record_stop", {"source": SOURCE})
    assert stopped.status_code == 409 and stopped.json()["code"] == "conflict"
    assert svc.engine._thread is None and not svc.engine._demands and svc.buffer.latest()[1] == 0


def test_selection_undo_restores_the_previous_monitor_off(profile):
    client, svc = profile
    displays = agent(client, "source_list").json()["monitors"]
    original = next(item["selection"] for item in displays if item["id"] == MONITOR.id)
    human_settings(client, monitor=original, capture_enabled=True)
    unchanged = agent(client, "source_select", SOURCE).json()
    assert unchanged["active"] and "undo_token" not in unchanged
    changed = agent(client, "source_select", {"mode": "monitor", "monitor": SECOND.id}).json()
    assert changed["monitor"] == SECOND.id and not changed["active"]
    restored = agent(client, "source_undo", {"undo_token": changed["undo_token"]}).json()
    assert restored["monitor"] == MONITOR.id and not restored["active"]
    again = agent(client, "source_undo", {"undo_token": changed["undo_token"]})
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    assert svc.engine._thread is None


def test_undo_cannot_overwrite_a_later_personal_activation(profile):
    client, svc = profile
    change = agent(client, "source_select", SOURCE).json()
    human_settings(client, capture_enabled=True)
    response = agent(client, "source_undo", {"undo_token": change["undo_token"]})
    assert response.status_code == 409 and response.json()["code"] == "conflict"
    assert svc.settings["capture_enabled"] and svc.settings["monitor"]["id"] == MONITOR.id
    assert svc.engine._thread is None


def test_scope_discards_a_live_result_after_revocation_or_reselection():
    state = {"mode": "overlay", "monitor": MONITOR.id, "active": True, "blocked": None, "revision": 1}
    svc = SimpleNamespace(sources=SimpleNamespace(status=lambda: dict(state)))
    source = SourceArgs(mode="overlay", monitor=MONITOR.id)

    def revoke():
        state["active"] = False
        return {"_image": "old bytes"}

    with pytest.raises(BairdError) as error:
        call_on_source(svc, source, revoke)
    assert error.value.code == "source_off"

    def cycle():
        state["revision"] += 2
        return {"_image": "old bytes"}

    state["active"] = True
    with pytest.raises(BairdError) as error:
        call_on_source(svc, source, cycle)
    assert error.value.code == "source_changed"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_authorized_mock_picture_and_expired_copilot_cache(tmp_path, monkeypatch):
    payload = tmp_path / "mock.ts"
    make_small_transport(payload, microphone=False, seconds=1.2)
    shim = tmp_path / "fake_worker.py"
    shim.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "print(json.dumps({'event':'ready','device':'lab','encoder':'mock',"
        "'input':{'width':160,'height':90}}),file=sys.stderr,flush=True)\n"
        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes());sys.stdout.buffer.flush()\n"
        "for line in sys.stdin:\n"
        "    if 'quit' in line: break\n", encoding="utf-8",
    )
    opens = []

    def producer(ffmpeg, selection, microphone, work_dir):
        opens.append(selection)
        return [sys.executable, "-B", str(shim), str(payload)]

    monkeypatch.setattr(capture, "IDLE_GRACE_S", 0.5)
    source = MonitorSource(FFMPEG, tmp_path, lambda: [MONITOR], producer)
    config = Config(data_dir=tmp_path / "profile", port=PORT, isolated_instance=True,
                    monitor_capture=True, background=False)
    svc = Services(config, monitor_source=source)
    svc.settings.update({"ocr_auto": False})
    with TestClient(create_app(config, svc), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        assert not agent(client, "source_select", SOURCE).json()["active"]
        assert agent(client, "copilot_look", {"source": SOURCE}).status_code == 403 and not opens
        human_settings(client, capture_enabled=True)
        result = agent(client, "copilot_look", {"source": SOURCE})
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["_image"]["mime"] == "image/jpeg" and data["picture"] == [160, 90] and opens
        kept = f"/copilot/look/{data['look']}.jpg"
        assert client.get(kept).status_code == 200
        human_settings(client, capture_enabled=False)
        assert client.get(kept).json()["code"] == "source_off"
        human_settings(client, capture_enabled=True)
        assert client.get(kept).status_code == 404


def test_monitor_producer_can_use_the_controller_selection(tmp_path):
    profile = Config(data_dir=tmp_path, port=PORT, monitor_capture=True, isolated_instance=True, background=False)
    chosen = {"id": SECOND.id, "layout": ""}

    def producer(ffmpeg, selection, microphone, work_dir):
        assert selection == chosen and not microphone
        return ["mock-producer"]

    source = MonitorSource("ffmpeg", tmp_path, lambda: [MONITOR, SECOND], producer)
    chosen = source.list_monitors()[1]["selection"]
    svc = Services(profile, monitor_source=source)
    assert svc.settings["monitor"] is None
    assert source.command_for(chosen, svc.settings) == ["mock-producer"]
    assert svc.engine._thread is None


def test_default_hdmi_profile_is_addressable_without_starting_a_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "ensure_helper", lambda: pytest.fail("native device was requested"))
    config = Config(data_dir=tmp_path, port=PORT, isolated_instance=True, background=False)
    svc = Services(config)
    with TestClient(create_app(config, svc), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        assert agent(client, "source_list").json()["modes"] == ["hdmi"]
        assert agent(client, "source_select", {"mode": "hdmi"}).json()["mode"] == "hdmi"
        assert agent(client, "capture_status", {"source": {"mode": "hdmi"}}).status_code == 200
        assert svc.engine._thread is None
