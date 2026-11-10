"""The new features through the whole app in demo mode: zone capture, annotated copies, text search, the screen watcher,
sending to Faustus and the shortcut settings. The OCR, the Hub's notifications and Faustus are replaced by fakes."""

from __future__ import annotations

import base64
import time

import pytest

from .conftest import FFMPEG
from .test_features_unit import FakeFaustus

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")

OCR = {"text": "Servidor listo en el puerto 5232", "ok": True}


@pytest.fixture
def fake_faustus():
    f = FakeFaustus()
    yield f
    f.close()


@pytest.fixture
def env(tmp_path):
    from fastapi.testclient import TestClient

    from baird_hoard.config import Config
    from baird_hoard.main import create_app
    from baird_hoard.services import Services

    cfg = Config(data_dir=tmp_path / "data", port=5232, fake=True, data_dir_configured=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(
        '{"media_dir": "%s", "clip_seconds": 3, "hotkeys_enabled": false}' % str(tmp_path / "media").replace("\\", "\\\\"))
    svc = Services(cfg)
    svc.ocr.ocr_fn = lambda path: {"ok": OCR["ok"], "text": OCR["text"], "backend": "fake", "error": "" if OCR["ok"] else "no ocr"}
    notes, bus = [], []
    svc.watch.notify_fn = lambda t, b, k: notes.append((t, b)) or {"ok": True}
    svc.watch.emit_fn = lambda t, d: bus.append((t, d))
    OCR.update(text="Servidor listo en el puerto 5232", ok=True)
    with TestClient(create_app(cfg, svc), base_url="http://127.0.0.1:5232", client=("127.0.0.1", 50000)) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            st = c.get("/api/status").json()
            if st["capture"]["state"] == "running" and (st["capture"]["buffered_seconds"] or 0) >= 3.5:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"demo source did not start: {st}")
        c.notes, c.bus = notes, bus
        yield c


def agent(c, tool, /, **arguments):
    headers = {"Authorization": f"Bearer {c.app.state.services.token}"}
    return c.post("/api/agent/call", headers=headers, json={"name": tool, "arguments": arguments})


def test_zone_capture_by_fraction_and_by_pixels(env):
    shot = env.post("/api/snapshot", json={"region": {"x": 0.25, "y": 0.0, "w": 0.25, "h": 0.5, "unit": "fraction"}}).json()
    assert shot["name"].startswith("Captura zona") and (shot["width"], shot["height"]) == (320, 360)
    assert shot["origin"] == "zone"
    px = agent(env, "snapshot_save", format="jpg", region={"x": 100, "y": 50, "w": 200, "h": 100, "unit": "px"}).json()
    assert px["name"].endswith(".jpg") and (px["width"], px["height"]) == (200, 100)
    cut = agent(env, "snapshot_save", region={"x": 1200, "y": 700, "w": 500, "h": 500}).json()          # sticks out: cut to the picture
    assert (cut["width"], cut["height"]) == (80, 20)
    look = agent(env, "screen_look", region={"x": 0, "y": 0, "w": 0.5, "h": 0.5, "unit": "fraction"}, max_side=640).json()
    assert look["_image"]["mime"] == "image/jpeg" and (look["width"], look["height"]) == (640, 360)


def test_zone_capture_rejects_bad_rectangles(env):
    assert env.post("/api/snapshot", json={"region": {"x": 2, "y": 0, "w": 0.2, "h": 0.2, "unit": "fraction"}}).status_code == 400
    assert env.post("/api/snapshot", json={"region": {"x": 0, "y": 0, "w": 0, "h": 0.2}}).status_code == 400
    assert agent(env, "snapshot_save", region={"x": 5000, "y": 0, "w": 10, "h": 10}).status_code == 400
    assert env.get("/api/media").json()["items"] == []


def test_annotated_copy_is_a_new_file_and_the_original_is_untouched(env):
    shot = env.post("/api/snapshot", json={}).json()
    original = open(shot["path"], "rb").read()
    drawn = original + b"\x00"                                      # the browser sends its own PNG; any PNG will do
    data = "data:image/png;base64," + base64.b64encode(drawn).decode()
    note = env.post(f"/api/media/{shot['name']}/annotate", json={"data": data}).json()
    assert note["name"].endswith("(anotada).png") and note["origin"] == "annotated" and note["width"] == 1280
    assert open(shot["path"], "rb").read() == original
    again = env.post(f"/api/media/{shot['name']}/annotate", json={"data": data}).json()
    assert again["name"].endswith("(anotada 2).png")
    assert env.post(f"/api/media/{shot['name']}/annotate", json={"data": "data:image/png;base64," + base64.b64encode(b"not a png at all" * 8).decode()}).status_code == 400
    assert env.post("/api/media/nope.png/annotate", json={"data": data}).status_code == 404
    clip = env.post("/api/clip", json={"seconds": 2}).json()
    assert env.post(f"/api/media/{clip['name']}/annotate", json={"data": data}).status_code == 400
    assert len([i for i in env.get("/api/media").json()["items"] if i["kind"] == "image"]) == 3


def test_text_search_over_the_screenshots(env):
    first = env.post("/api/snapshot", json={}).json()
    deadline = time.time() + 20
    while time.time() < deadline and not env.get("/api/search", params={"q": "puerto"}).json()["items"]:
        time.sleep(0.3)                                             # the background reader picks the new screenshot up
    hit = env.get("/api/search", params={"q": "PUERTO listo"}).json()
    assert [i["name"] for i in hit["items"]] == [first["name"]] and "puerto 5232" in hit["items"][0]["snippet"]
    OCR["text"] = "Error: disco lleno"
    second = env.post("/api/snapshot", json={}).json()
    deadline = time.time() + 20
    while time.time() < deadline and not agent(env, "media_search", query="disco").json().get("items"):
        time.sleep(0.3)
    found = agent(env, "media_search", query="disco").json()
    assert [i["name"] for i in found["items"]] == [second["name"]]
    assert env.post(f"/api/media/{second['name']}/ocr").json()["text"] == "Error: disco lleno"
    assert env.get("/api/search", params={"q": "inexistente"}).json()["items"] == []
    assert env.get("/api/search", params={"q": " "}).status_code == 400
    status = env.get("/api/ocr/status").json()
    assert status["indexed"] == 2 and status["unread"] == 0
    env.post(f"/api/media/{first['name']}/rename", json={"new_name": "renombrada"})
    assert env.get("/api/search", params={"q": "puerto"}).json()["items"][0]["name"] == "renombrada.png"
    env.delete(f"/api/media/{second['name']}")
    assert env.get("/api/search", params={"q": "disco"}).json()["items"] == []


def test_ocr_unavailable_is_an_explained_error(env):
    OCR["ok"] = False
    shot = env.post("/api/snapshot", json={}).json()
    res = env.post(f"/api/media/{shot['name']}/ocr")
    assert res.status_code == 503 and "Kafka" in res.json()["hint"]


def test_watch_change_rule_alerts_with_a_picture(env):
    rule = env.post("/api/watch", json={"kind": "change", "name": "Pantalla viva", "threshold": 0.1, "cooldown_s": 0,
                                        "enabled": False, "region": {"x": 0, "y": 0, "w": 1, "h": 1, "unit": "fraction"}}).json()
    assert rule["id"] and rule["interval_s"] == 2.0
    fired = None
    for _ in range(12):
        res = env.post(f"/api/watch/{rule['id']}/check").json()
        if res["fired"]:
            fired = res
            break
        time.sleep(1.0)
    assert fired, "the demo picture moves, so a 0.1 % rule must fire"
    events = env.get("/api/watch/events").json()["events"]
    assert events[0]["rule"] == "Pantalla viva" and events[0]["image"].startswith("Aviso")
    assert any(i["name"] == events[0]["image"] for i in env.get("/api/media").json()["items"])
    deadline = time.time() + 5
    while time.time() < deadline and not env.notes:
        time.sleep(0.1)
    assert env.notes and env.notes[0][0] == "Baird: Pantalla viva" and env.bus[0][0] == "baird.watch"
    st = env.get("/api/status").json()["watch"]
    assert st["rules"] == 1 and st["active"] == 0 and st["last_event"]["rule"] == "Pantalla viva"
    env.put(f"/api/watch/{rule['id']}", json={"enabled": True, "threshold": 50})
    assert env.get("/api/watch").json()["rules"][0]["threshold"] == 50
    assert env.delete(f"/api/watch/{rule['id']}").status_code == 200
    assert env.get("/api/watch").json()["rules"] == []


def test_watch_text_rule_through_the_agent_tools(env):
    OCR["text"] = "compilando"
    added = agent(env, "watch_add", kind="text", text="terminado", interval_s=5, cooldown_s=0, enabled=False,
                  region={"x": 0.0, "y": 0.5, "w": 1.0, "h": 0.5, "unit": "fraction"}).json()
    assert agent(env, "watch_check", id=added["id"]).json()["fired"] is False
    OCR["text"] = "Proceso Terminado con éxito"
    res = agent(env, "watch_check", id=added["id"]).json()
    assert res["fired"] and "terminado" in res["detail"]
    listed = agent(env, "watch_list").json()
    assert listed["rules"][0]["status"]["value"] is True and listed["events"][0]["rule"] == "Aparece «terminado»"
    assert agent(env, "watch_events", limit=5).json()["events"][0]["image"]
    assert agent(env, "watch_add", kind="text").status_code == 400
    assert agent(env, "watch_remove", id="zzzz").status_code == 404
    OCR["ok"] = False
    again = agent(env, "watch_check", id=added["id"]).json()
    assert again["fired"] is False and "OCR" in again["error"]


def test_send_to_faustus(env, fake_faustus, monkeypatch):
    monkeypatch.setenv("BAIRD_FAUSTUS_URL", fake_faustus.url)
    shot = env.post("/api/snapshot", json={}).json()
    sent = env.post(f"/api/media/{shot['name']}/faustus", json={"message": "¿Qué error es este?"}).json()
    assert sent["url"] == f"{fake_faustus.url}/studio?s=sess-1" and sent["file"] == shot["name"]
    assert [m for m, _, _ in fake_faustus.calls if m != "GET"] == ["POST", "POST", "PUT"]
    via_tool = agent(env, "send_to_faustus", name=shot["name"]).json()
    assert via_tool["session_id"] == "sess-1"
    assert env.post("/api/media/nada.png/faustus", json={}).status_code == 404


def test_send_to_faustus_when_nothing_answers(env, monkeypatch):
    monkeypatch.setenv("BAIRD_FAUSTUS_URL", "http://127.0.0.1:9")
    monkeypatch.setattr("baird_hoard.faustus.faustus_candidates", lambda *a, **k: ["http://127.0.0.1:9"])
    shot = env.post("/api/snapshot", json={}).json()
    res = env.post(f"/api/media/{shot['name']}/faustus", json={})
    assert res.status_code == 503 and "Faustus" in res.json()["error"]


def test_shortcut_settings_and_status(env):
    s = env.get("/api/settings").json()
    assert s["hotkey_shot"] == "ctrl+alt+c" and s["hotkey_clip"] == "ctrl+alt+x" and s["ocr_auto"] is True
    assert env.put("/api/settings", json={"hotkey_shot": "Ctrl+Shift+S", "hotkey_clip": "alt+f9"}).json()["hotkey_shot"] == "ctrl+shift+s"
    assert env.put("/api/settings", json={"hotkey_shot": "c"}).status_code == 400
    assert env.put("/api/settings", json={"hotkey_clip": "ctrl+shift+s"}).status_code == 400
    hk = env.get("/api/hotkeys").json()
    assert hk["enabled"] is False and hk["running"] is False
    assert agent(env, "hotkeys_status").json()["enabled"] is False
    assert agent(env, "settings_set", hotkey_shot="ctrl+alt+x").json()["hotkey_shot"] == "ctrl+alt+x"


def test_catalog_lists_the_new_tools(env):
    headers = {"Authorization": f"Bearer {env.app.state.services.token}"}
    tools = {t["name"] for t in env.get("/api/agent/tools", headers=headers).json()["tools"]}
    assert {"media_search", "media_ocr", "ocr_reindex", "send_to_faustus", "watch_list", "watch_add", "watch_update",
            "watch_remove", "watch_check", "watch_events", "hotkeys_status"} <= tools
