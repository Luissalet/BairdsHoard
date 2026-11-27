"""Agent source contract and activation boundary with synthetic doubles."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from baird_hoard import agent_sources, capture, capture_approval
from baird_hoard.agent_sources import SourceArgs, call_on_source
from baird_hoard.config import Config, Settings
from baird_hoard.errors import BairdError
from baird_hoard.main import create_app
from baird_hoard.monitor_source import MonitorSource
from baird_hoard.screen_privacy import ScreenPolicy
from baird_hoard.services import Services

from .conftest import FFMPEG
from .test_monitor_source import (CAPTURE_CONTROL, MONITOR, SECOND, capture_control_headers, confirm_test_shortcut,
                                  make_small_transport, set_local_capture)

PORT = 17531
SOURCE = {"mode": "monitor", "monitor": MONITOR.id}


def agent(client, name, arguments=None, **body):
    return client.post("/api/agent/call", headers={"Authorization": f"Bearer {client.app.state.services.token}"},
                       json={"name": name, "arguments": arguments or {}, **body})


def human_settings(client, **body):
    enabled = body.pop("capture_enabled", None)
    if body:
        result = client.put("/api/settings", json=body)
        assert result.status_code == 200, result.text
    if enabled is not None:
        set_local_capture(client, enabled)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    def unexpected_device(*args, **kwargs):
        pytest.fail("an agent tried to open a native device")

    monkeypatch.setattr(capture, "ensure_helper", unexpected_device)
    source = MonitorSource(FFMPEG or "ffmpeg", tmp_path, lambda: [MONITOR, SECOND], unexpected_device)
    config = Config(data_dir=tmp_path / "profile", port=PORT, monitor_capture=True,
                    isolated_instance=True, background=False)
    svc = Services(config, monitor_source=source, confirm_capture=confirm_test_shortcut)
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
    local_client = client.put("/api/settings", json={"capture_enabled": True})
    assert local_client.status_code == 403 and local_client.json()["code"] == "source_activation_required"
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


def test_complete_http_request_cannot_replace_local_approval(profile, monkeypatch):
    client, svc = profile
    attempts = []

    def deny(key, cancelled, registered):
        attempts.append((key, cancelled.is_set()))
        return False

    monkeypatch.setattr(svc.sources, "_confirm", deny, raising=False)
    agent(client, "source_select", SOURCE)
    headers = capture_control_headers(client)
    ticket = client.post(CAPTURE_CONTROL, headers=headers).json()["ticket"]
    forged = client.put(CAPTURE_CONTROL, headers=headers, json={"ticket": ticket, "enabled": True, "approved": True})
    assert forged.status_code == 400 and not attempts
    reply = client.put(CAPTURE_CONTROL, headers=headers, json={"ticket": ticket, "enabled": True})
    assert reply.status_code == 403 and reply.json()["code"] == "source_approval_required"
    assert len(attempts) == 1 and not attempts[0][1]
    assert not svc.settings["capture_enabled"] and svc.engine._thread is None


def test_expired_dialog_shortcut_cannot_be_reissued_immediately(profile, monkeypatch):
    client, svc = profile
    agent(client, "source_select", SOURCE)
    headers = capture_control_headers(client)
    keys = []

    def deny(key, cancelled, registered):
        keys.append(key)
        registered.set()
        return False

    monkeypatch.setattr(agent_sources, "choice", min)
    monkeypatch.setattr(svc.sources, "_confirm", deny)
    for _ in range(2):
        ticket = client.post(CAPTURE_CONTROL, headers=headers).json()["ticket"]
        assert client.put(CAPTURE_CONTROL, headers=headers, json={"ticket": ticket, "enabled": True}).status_code == 403
    assert keys == list(capture_approval.APPROVAL_KEYS[:2]) and not svc.settings["capture_enabled"]


def test_windows_confirmation_requires_the_matching_message_and_unregisters(monkeypatch):
    native = SimpleNamespace(RegisterHotKey=Mock(return_value=1), UnregisterHotKey=Mock(return_value=1))
    events = []

    def peek(pointer, window, first, last, flags):
        assert window is None and first == last == 0x312
        if not flags:
            return 0
        _, identifier, modifiers, vk = native.RegisterHotKey.call_args.args
        events.append(identifier)
        pointer._obj.wParam = identifier if len(events) > 1 else identifier + 1
        pointer._obj.lParam = (vk << 16) | (modifiers & 7)
        return 1

    native.PeekMessageW = Mock(side_effect=peek)
    monkeypatch.setattr(capture_approval, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(capture_approval.ctypes, "WinDLL", lambda *a, **kw: native, raising=False)
    registered = threading.Event()
    assert capture_approval.confirm_with_windows(8, threading.Event(), registered) and registered.is_set()
    window, identifier, modifiers, vk = native.RegisterHotKey.call_args.args
    assert window is None and vk == 0x77 and modifiers == 0x4007
    assert events == [identifier, identifier]
    native.UnregisterHotKey.assert_called_once_with(None, identifier)


@pytest.mark.parametrize("change", ["selection", "layout", "cancel"])
def test_approval_cannot_activate_a_changed_or_cancelled_source(profile, monkeypatch, change):
    client, svc = profile
    agent(client, "source_select", SOURCE)
    headers = capture_control_headers(client)
    control = client.post(CAPTURE_CONTROL, headers=headers).json()
    args = {"ticket": control["ticket"], "enabled": True}
    waiting, registration, gesture = threading.Event(), threading.Event(), threading.Event()
    keys = []

    def confirm(key, cancelled, registered):
        keys.append(key)
        waiting.set()
        assert registration.wait(12)
        registered.set()
        assert gesture.wait(12)
        return True  # Exercise the race even if the listener already had a matching event.

    monkeypatch.setattr(svc.sources, "_confirm", confirm)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.put, CAPTURE_CONTROL, headers=headers, json=args)
        try:
            assert waiting.wait(12)
            prompt = CAPTURE_CONTROL + "/confirmation"
            probe = {"ticket": control["ticket"]}
            assert client.post(prompt, headers=headers, json=probe).json()["key"] is None
            registration.set()
            deadline = monotonic() + 8
            while client.post(prompt, headers=headers, json=probe).json()["key"] != keys[0]:
                assert monotonic() < deadline
                sleep(0.02)
            with closing(TestClient(client.app, base_url=str(client.base_url), client=("127.0.0.1", 50002))) as other:
                other.post(CAPTURE_CONTROL, headers=headers)
                assert other.post(prompt, headers=headers, json=probe).json()["key"] is None
            assert agent(client, "screen_look").json()["code"] == "source_off"
            if change == "selection":
                assert agent(client, "source_select", {"mode": "monitor", "monitor": SECOND.id}).status_code == 200
            elif change == "layout":
                svc.monitor_source = replace(svc.monitor_source, inventory=lambda: [replace(MONITOR, x=-500), SECOND])
            else:
                cancelled = client.request("DELETE", CAPTURE_CONTROL, headers=headers, json={"ticket": control["ticket"]})
                assert cancelled.status_code == 204
        finally:
            registration.set()
            gesture.set()
        reply = pending.result(timeout=12)
    assert reply.status_code == (409 if change == "layout" else 403)
    assert not svc.settings["capture_enabled"] and svc.engine._thread is None


def test_capture_ticket_belongs_to_its_browser_and_selection(profile):
    client, svc = profile
    agent(client, "source_select", SOURCE)
    headers = capture_control_headers(client)
    assert client.post(CAPTURE_CONTROL).status_code == 403
    issued = client.post(CAPTURE_CONTROL, headers=headers)
    ticket = issued.json()["ticket"]
    assert issued.json()["display"]["x"] == MONITOR.x
    assert "httponly" in issued.headers["set-cookie"].lower()
    assert issued.headers["cache-control"] == "no-store"

    args = {"ticket": ticket, "enabled": True}
    forged = client.put(CAPTURE_CONTROL, headers={**headers, "authorization": f"Bearer {svc.token}"}, json=args)
    assert forged.status_code == 403
    with closing(TestClient(client.app, base_url=str(client.base_url), client=("127.0.0.1", 50001))) as stranger:
        assert stranger.put(CAPTURE_CONTROL, headers=headers, json=args).status_code == 403
    agent(client, "source_select", {"mode": "monitor", "monitor": SECOND.id})
    assert client.put(CAPTURE_CONTROL, headers=headers, json=args).status_code == 403
    assert not svc.settings["capture_enabled"]

    next_ticket = client.post(CAPTURE_CONTROL, headers=headers).json()["ticket"]
    next_args = {"ticket": next_ticket, "enabled": True}
    assert client.put(CAPTURE_CONTROL, headers=headers, json=next_args).json()["active"]
    assert client.put(CAPTURE_CONTROL, headers=headers, json=next_args).status_code == 403
    assert svc.settings["monitor"]["id"] == SECOND.id and svc.settings["capture_enabled"]
    assert svc.engine._thread is None


def test_capture_ticket_rechecks_display_layout(profile):
    client, svc = profile
    agent(client, "source_select", SOURCE)
    headers = capture_control_headers(client)
    ticket = client.post(CAPTURE_CONTROL, headers=headers).json()["ticket"]
    svc.monitor_source = replace(svc.monitor_source, inventory=lambda: [replace(MONITOR, x=-500), SECOND])
    stale = client.put(CAPTURE_CONTROL, headers=headers, json={"ticket": ticket, "enabled": True})
    assert stale.status_code == 409 and stale.json()["code"] == "monitor_reselect"
    assert svc.engine._thread is None and not svc.settings["capture_enabled"]


def test_monitor_profile_restarts_with_capture_off(tmp_path):
    config = Config(data_dir=tmp_path, port=PORT, monitor_capture=True, isolated_instance=True, background=False)
    source = MonitorSource("unused-double", tmp_path, lambda: [MONITOR])
    settings = Settings(config.settings_path, isolated_instance=True)
    settings.update({"monitor": source.list_monitors()[0]["selection"], "capture_enabled": True})
    svc = Services(config, monitor_source=source)
    assert svc.settings["monitor"]["id"] == MONITOR.id and not svc.sources.status()["active"]
    assert not svc.settings["capture_enabled"] and svc.engine._thread is None


@pytest.fixture
def rendered_picture(profile, monkeypatch):
    client, svc = profile
    agent(client, "source_select", SOURCE)
    set_local_capture(client, True)
    monkeypatch.setattr(svc.engine, "request", nullcontext)
    monkeypatch.setattr(svc.engine, "wait_for_picture", lambda: None)
    monkeypatch.setattr(svc.library, "input_size", lambda: (160, 90))
    return client, svc


@pytest.mark.parametrize("reactivate", [False, True])
def test_pending_snapshot_never_reaches_library_or_ocr(rendered_picture, monkeypatch, reactivate):
    client, svc = rendered_picture
    monkeypatch.setattr(svc.buffer, "latest", lambda: (b"synthetic-transport", 1))
    staged, finish = threading.Event(), threading.Event()
    notified = []
    svc.library.on_new_image = notified.append

    def decode(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"synthetic-image")
        if not finish.is_set():
            staged.set()
            assert finish.wait(8), "snapshot never received permission to finish rendering"
        return SimpleNamespace(returncode=0, stderr=b"")

    finish.set()
    monkeypatch.setattr(svc.library, "_run", decode)
    published = agent(client, "snapshot_save").json()["name"]
    assert len(notified) == 1
    finish.clear()

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(agent, client, "snapshot_save")
        try:
            assert staged.wait(8)
            assert [row["name"] for row in svc.library.list(kind="image")] == [published]
            set_local_capture(client, False)
            if reactivate:
                set_local_capture(client, True)
        finally:
            finish.set()
        response = pending.result(timeout=8)

    assert response.status_code == (409 if reactivate else 403)
    assert response.json()["code"] == ("source_changed" if reactivate else "source_off")
    assert [row["name"] for row in svc.library.list(kind="image")] == [published]
    assert [path.name for path in svc.library.folder.iterdir()] == [published]
    assert set(svc.library._index) == {published} and len(notified) == 1


@pytest.mark.parametrize(("tool", "expected_status"), [("snapshot_save", 200), ("screen_look", 403)])
def test_snapshot_receipt_and_live_image_after_revocation(rendered_picture, monkeypatch, tool, expected_status):
    client, svc = rendered_picture
    published = []
    monkeypatch.setattr(svc.buffer, "latest", lambda: (b"synthetic-transport", 1))

    def render(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"synthetic-png")
        return SimpleNamespace(returncode=0, stderr=b"")

    def disarm_after_publication(path):
        published.append(path)
        svc.apply_settings({"capture_enabled": False})

    monkeypatch.setattr(svc.library, "_run", render)
    monkeypatch.setattr(svc.library, "on_new_image", disarm_after_publication)
    monkeypatch.setattr(svc.library, "image_for_model", lambda *args: {"mime": "image/png", "base64": "cGl4ZWxz"})
    response = agent(client, tool, {"source": SOURCE})
    assert response.status_code == expected_status, response.text
    assert len(published) == 1 and published[0].read_bytes() == b"synthetic-png"
    assert set(svc.library._index) == {published[0].name} and not svc.settings["capture_enabled"]
    if tool == "snapshot_save":
        assert response.json()["name"] == published[0].name
        assert Path(response.json()["path"]) == published[0]
    else:
        assert response.json()["code"] == "source_off" and "cGl4ZWxz" not in response.text


@pytest.mark.parametrize("pipeline", ["plain", "live-enhanced", "rendered", "fallback"])
def test_pending_clip_never_reaches_library(rendered_picture, monkeypatch, pipeline):
    client, svc = rendered_picture
    monkeypatch.setattr(svc.buffer, "since", lambda seconds: [b"synthetic-transport"])
    monkeypatch.setattr(svc.buffer, "seconds_kept", lambda: 1.0)
    monkeypatch.setattr(svc.library, "wants_enhanced", lambda: pipeline != "plain")
    live = svc.buffer if pipeline == "live-enhanced" else SimpleNamespace(seconds_kept=lambda: 0)
    monkeypatch.setattr(svc.library, "stage", SimpleNamespace(buffer=live))
    monkeypatch.setattr(svc.library, "_probe", lambda path: {"duration": 1.0})
    staged, finish = threading.Event(), threading.Event()

    def hold():
        staged.set()
        assert finish.wait(12)

    def mux(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"synthetic-mp4")
        if pipeline in ("plain", "live-enhanced") and not finish.is_set():
            hold()
        return SimpleNamespace(returncode=0, stderr=b"")

    def enhance(raw, draft, fx):
        assert raw.read_bytes() == b"synthetic-mp4" and not fx["hdr"]
        draft.write_bytes(b"synthetic-enhanced")
        if not finish.is_set():
            hold()
        if pipeline == "fallback":
            raise BairdError("unavailable", "synthetic enhancement error")

    monkeypatch.setattr(svc.library, "_run", mux)
    monkeypatch.setattr(svc.library, "enhancer", SimpleNamespace(enhance_file=enhance))
    finish.set()
    approved = agent(client, "clip_save", {"seconds": 1})
    assert approved.status_code == 200, approved.text
    saved = approved.json()
    assert saved["enhanced"] == (pipeline in ("live-enhanced", "rendered"))
    filename = saved["name"]
    original = Path(saved["path"]).read_bytes()
    finish.clear()

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(agent, client, "clip_save", {"seconds": 1})
        try:
            assert staged.wait(12)
            assert [item["name"] for item in svc.library.list(kind="video")] == [filename]
            set_local_capture(client, False)
        finally:
            finish.set()
        reply = pending.result(timeout=12)

    assert reply.status_code == 403 and reply.json()["code"] == "source_off"
    assert [item.name for item in svc.library.folder.iterdir()] == [filename]
    assert set(svc.library._index) == {filename} and Path(saved["path"]).read_bytes() == original
    assert not list(svc.config.work_dir.glob("clip-*.ts"))


def test_clip_committed_before_revocation_returns_the_saved_clip(rendered_picture, monkeypatch):
    client, svc = rendered_picture
    monkeypatch.setattr(svc.buffer, "since", lambda seconds: [b"synthetic-transport"])
    monkeypatch.setattr(svc.buffer, "seconds_kept", lambda: 1.0)
    monkeypatch.setattr(svc.library, "wants_enhanced", lambda: False)
    monkeypatch.setattr(svc.library, "_probe", lambda path: {"duration": 1.0})

    def mux(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"synthetic-mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    clip = svc.library.clip

    def revoke_after_publication(seconds):
        saved = clip(seconds)
        svc.apply_settings({"capture_enabled": False})
        return saved

    monkeypatch.setattr(svc.library, "_run", mux)
    monkeypatch.setattr(svc.library, "clip", revoke_after_publication)
    response = agent(client, "clip_save", {"seconds": 1})
    assert response.status_code == 200, response.text
    assert Path(response.json()["path"]).read_bytes() == b"synthetic-mp4"
    assert not svc.settings["capture_enabled"]


@pytest.mark.parametrize("url", [None, "/copilot/look.jpg"])
def test_pending_look_never_repopulates_revoked_cache(rendered_picture, monkeypatch, url):
    client, svc = rendered_picture
    render, finish = threading.Event(), threading.Event()

    def grab(*args, **kwargs):
        render.set()
        assert finish.wait(8), "look never received permission to finish rendering"
        return b"synthetic-jpeg", 160, 90

    monkeypatch.setattr(svc.library, "grab_jpeg", grab)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.get, url) if url else executor.submit(agent, client, "copilot_look")
        try:
            assert render.wait(8)
            set_local_capture(client, False)
        finally:
            finish.set()
        response = pending.result(timeout=8)

    assert response.status_code == 403 and response.json()["code"] == "source_off"
    assert not svc.copilot._looks and svc.copilot._last_look is None


@pytest.mark.parametrize(("reason", "status"), [("privacy_blocked", 409), ("policy_unavailable", 503)])
def test_producer_privacy_block_clears_agent_look_and_allows_human_disarm(rendered_picture, monkeypatch, reason, status):
    client, svc = rendered_picture
    monkeypatch.setattr(svc.library, "grab_jpeg", lambda *args: (b"synthetic-jpeg", 160, 90))
    visible = agent(client, "copilot_look")
    assert visible.status_code == 200 and svc.copilot._looks
    url = f"/copilot/look/{visible.json()['look']}.jpg"
    proc = object()
    svc.engine._proc = proc
    try:
        svc.engine._block_for_privacy(proc, reason, "Stopped by the producer")
    finally:
        svc.engine._proc = None
    assert agent(client, "source_list").json()["blocked"] == reason
    assert not svc.copilot._looks
    assert client.get(url).status_code == status
    blocked = agent(client, "screen_look")
    assert blocked.status_code == status and blocked.json()["code"] == reason
    set_local_capture(client, False)
    assert agent(client, "screen_look").json()["code"] == "source_off"


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
    svc = Services(config, monitor_source=source, confirm_capture=confirm_test_shortcut)
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


def test_persistent_local_features_require_a_gesture_and_apply_live(tmp_path, monkeypatch):
    def unexpected_device(*args, **kwargs):
        pytest.fail("a feature switch tried to open a capture device")

    def privacy():
        return ScreenPolicy(False, ())

    monkeypatch.setattr(capture, "ensure_helper", unexpected_device)
    config = Config(data_dir=tmp_path / "profile", port=PORT, isolated_instance=True, background=False)
    source = MonitorSource(FFMPEG or "ffmpeg", tmp_path, lambda: [MONITOR], unexpected_device)
    client_options = {"base_url": f"http://127.0.0.1:{PORT}", "client": ("127.0.0.1", 50000)}
    monitor = CAPTURE_CONTROL + "?target=monitor_capture"
    overlay = CAPTURE_CONTROL + "?target=overlay_mode"
    svc = Services(config, monitor_source=source, read_screen_policy=privacy, confirm_capture=confirm_test_shortcut)

    def change(client, control, enabled):
        headers = capture_control_headers(client)
        ticket = client.post(control, headers=headers).json()["ticket"]
        assert ticket
        response = client.put(control, headers=headers, json={"ticket": ticket, "enabled": enabled})
        assert response.status_code == 200, response.text
        assert response.json()["active"] is enabled

    with TestClient(create_app(config, svc), **client_options) as client:
        initial = agent(client, "source_list").json()
        assert initial["modes"] == ["hdmi"] and initial["monitors"] == []
        assert initial["local_features"] == {"monitor_capture": False, "overlay_mode": False}
        assert client.get("/api/monitors").json() == {"enabled": False, "monitors": []}
        headers = capture_control_headers(client)
        assert client.post(monitor).status_code == 403
        assert client.post(monitor, headers={**headers, "origin": "http://elsewhere.test"}).status_code == 403
        assert client.post(monitor, headers={**headers, "Authorization": f"Bearer {svc.token}"}).status_code == 403
        for target in initial["local_features"]:
            assert client.put("/api/settings", json={target: True}).status_code == 403
            rejected = agent(client, "settings_set", {target: True})
            assert rejected.status_code == 400 and rejected.json()["code"] == "invalid"
        assert client.post(overlay, headers=headers).json()["ticket"] is None

        ticket = client.post(monitor, headers=headers).json()["ticket"]
        assert client.put(overlay, headers=headers, json={"ticket": ticket, "enabled": True}).status_code == 403
        monkeypatch.setattr(svc.sources, "_confirm", lambda *_: False)
        denied = client.put(monitor, headers=headers, json={"ticket": ticket, "enabled": True})
        assert denied.status_code == 403 and denied.json()["code"] == "source_approval_required"
        assert not svc.settings["monitor_capture"] and svc.monitor_source is None
        assert client.put(monitor, headers=headers, json={"ticket": ticket, "enabled": True}).status_code == 403

        monkeypatch.setattr(svc.sources, "_confirm", confirm_test_shortcut)
        change(client, monitor, True)
        after = agent(client, "source_list").json()
        assert after["mode"] == "hdmi" and after["modes"] == ["hdmi", "monitor"]
        assert [screen["id"] for screen in after["monitors"]] == [MONITOR.id]
        assert agent(client, "source_select", {**SOURCE, "mode": "overlay"}).status_code == 503
        change(client, overlay, True)
        after = agent(client, "source_list").json()
        assert after["modes"] == ["hdmi", "monitor", "overlay"]
        assert after["local_features"] == {"monitor_capture": True, "overlay_mode": True}
        selected = agent(client, "source_select", {**SOURCE, "mode": "overlay"}).json()
        assert not selected["active"] and svc.engine._thread is None

    restarted = Services(config, monitor_source=source, read_screen_policy=privacy, confirm_capture=confirm_test_shortcut)
    with TestClient(create_app(config, restarted), **client_options) as client:
        after = agent(client, "source_list").json()
        assert after["mode"] == "overlay" and not after["active"] and after["monitor"] == MONITOR.id
        assert agent(client, "screen_look", {"source": {**SOURCE, "mode": "overlay"}, "save": False}).status_code == 403
        set_local_capture(client, True)
        assert restarted.engine.source_enabled()
        change(client, overlay, False)
        after = agent(client, "source_list").json()
        assert after["mode"] == "monitor" and after["modes"] == ["hdmi", "monitor"] and not after["active"]
        change(client, monitor, False)
        after = agent(client, "source_list").json()
        assert after["mode"] == "hdmi" and after["modes"] == ["hdmi"] and not after["active"]
        assert after["monitors"] == [] and after["local_features"] == initial["local_features"]
        stored = Settings(config.settings_path).get()
        assert not stored["monitor_capture"] and not stored["overlay_mode"]


def test_default_hdmi_profile_is_addressable_without_starting_a_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "ensure_helper", lambda: pytest.fail("native device was requested"))
    config = Config(data_dir=tmp_path, port=PORT, isolated_instance=True, background=False)
    svc = Services(config)
    with TestClient(create_app(config, svc), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        assert agent(client, "source_list").json()["modes"] == ["hdmi"]
        assert agent(client, "source_select", {"mode": "hdmi"}).json()["mode"] == "hdmi"
        assert agent(client, "capture_status", {"source": {"mode": "hdmi"}}).status_code == 200
        assert svc.engine._thread is None
