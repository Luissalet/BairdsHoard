"""The whole app in demo mode (an ffmpeg test pattern instead of the device): live WebSocket, screenshot, clip,
recording, the library and the agent tools."""

from __future__ import annotations

import time

import pytest

from .conftest import FFMPEG

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from baird_hoard.config import Config
    from baird_hoard.main import create_app

    cfg = Config(data_dir=tmp_path / "data", port=5232, fake=True, data_dir_configured=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text('{"media_dir": "%s", "clip_seconds": 3}' % str(tmp_path / "media").replace("\\", "\\\\"))
    app = create_app(cfg)
    with TestClient(app, base_url="http://127.0.0.1:5232", client=("127.0.0.1", 50000)) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            st = c.get("/api/status").json()
            if st["capture"]["state"] == "running" and (st["capture"]["buffered_seconds"] or 0) >= 3.5:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"demo source did not start: {st}")
        yield c


def test_health(client):
    body = client.get("/api/health").json()
    assert body["service"] == "bairds-hoard" and body["capture"] == "running"


def test_snapshot_clip_and_library(client):
    shot = client.post("/api/snapshot", json={}).json()
    assert shot["kind"] == "image" and shot["name"].startswith("Captura") and shot["size"] > 1000
    clip = client.post("/api/clip", json={"seconds": 2}).json()
    assert clip["kind"] == "video" and 1.5 <= (clip["duration"] or 0) <= 4.5 and clip["audio"]
    items = client.get("/api/media").json()["items"]
    assert {shot["name"], clip["name"]} <= {i["name"] for i in items}
    assert client.get(f"/media/{shot['name']}").headers["content-type"] == "image/png"
    assert client.get(f"/api/thumb/{clip['name']}").status_code == 200
    renamed = client.post(f"/api/media/{shot['name']}/rename", json={"new_name": "pantalla del portatil"}).json()
    assert renamed["name"] == "pantalla del portatil.png"
    assert client.delete(f"/api/media/{renamed['name']}").status_code == 200
    assert client.get("/media/../settings.json").status_code in (400, 404)


def test_recording(client):
    assert client.post("/api/record/start").json()["state"] == "recording"
    assert client.post("/api/record/start").status_code == 409
    time.sleep(2.5)
    client.post("/api/record/stop")
    deadline = time.time() + 20
    while time.time() < deadline:
        rec = client.get("/api/status").json()["recording"]
        if rec["state"] == "idle" and rec["last"]:
            break
        time.sleep(0.3)
    assert rec["last"]["name"].startswith("Grabación") and rec["last"]["duration"] >= 2


def test_live_websocket_sends_init_and_fragments(client):
    with client.websocket_connect("ws://127.0.0.1:5232/api/live") as ws:
        first = ws.receive_json()
        assert first["type"] == "init" and first["mime"].startswith('video/mp4; codecs="avc1.')
        init = ws.receive_bytes()
        assert init[4:8] == b"ftyp"
        seen = []
        for _ in range(12):
            msg = ws.receive()
            seen.append(msg.get("text") or (msg.get("bytes") or b"")[4:8])
            if msg.get("bytes", b"")[4:8] == b"moof":
                break
        assert seen[-1] == b"moof", seen
        assert not any(isinstance(m, str) and "reset" in m for m in seen), seen


def test_agent_tools(client):
    svc = client.app.state.services
    headers = {"Authorization": f"Bearer {svc.token}"}
    tools = {t["name"] for t in client.get("/api/agent/tools", headers=headers).json()["tools"]}
    assert {"screen_look", "clip_save", "record_start", "media_list", "media_copy"} <= tools
    look = client.post("/api/agent/call", headers=headers, json={"name": "screen_look", "arguments": {"max_side": 640}}).json()
    assert look["_image"]["mime"] == "image/jpeg" and len(look["_image"]["data"]) > 1000 and "truncated" not in look
    listed = client.post("/api/agent/call", headers=headers, json={"name": "media_list", "arguments": {}}).json()
    assert listed["items"][0]["name"] == look["name"]
    refused = client.post("/api/agent/call", headers=headers, json={"name": "media_delete", "arguments": {"name": look["name"]}})
    assert refused.status_code == 400
    assert client.post("/api/agent/call", json={"name": "media_list", "arguments": {}}).status_code == 401


def test_settings_validation(client):
    assert client.put("/api/settings", json={"snapshot_format": "gif"}).status_code == 400
    s = client.put("/api/settings", json={"clip_seconds": 5}).json()
    assert s["clip_seconds"] == 5
