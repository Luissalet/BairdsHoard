"""The copilot through the whole app in demo mode: the page an assistant reads, the HTTP calls, the tools the MCP
bridge offers, the page's WebSocket and buttons, and the real frame tap on the demo picture. The text reader is a
fake, except in the last test, which reads a drawn picture with the Windows OCR where there is one."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from .conftest import FFMPEG

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")

PICTURE = (1280, 720)          # the demo source


class Reader:
    """Stands in for the Windows OCR: three labels on the demo picture, at the size the picture was enlarged to."""

    def __init__(self, library):
        self.library = library

    def available(self) -> bool:
        return True

    def read(self, path, lang=""):
        k = 2.0
        labels = [("Guardar", 600, 340), ("Cancelar", 720, 340), ("Servidor listo", 40, 40)]
        lines = []
        for text, x, y in labels:
            words, cx = [], x
            for word in text.split():
                words.append([cx * k, y * k, 9 * len(word) * k, 16 * k, word])
                cx += 9 * len(word) + 6
            lines.append([x * k, y * k, (cx - 6 - x) * k, 16 * k, text, words])
        return {"w": PICTURE[0] * k, "h": PICTURE[1] * k, "lines": lines}

    def stop(self) -> None:
        pass


@pytest.fixture
def env(tmp_path):
    from fastapi.testclient import TestClient

    from baird_hoard.config import Config
    from baird_hoard.main import create_app
    from baird_hoard.services import Services

    cfg = Config(data_dir=tmp_path / "data", port=5232, fake=True, data_dir_configured=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(
        '{"media_dir": "%s", "capture_always_on": true, "ocr_auto": false, "hotkeys_enabled": false}' % str(tmp_path / "media").replace("\\", "\\\\"))
    svc = Services(cfg)
    svc.copilot.reader = Reader(svc.library)
    with TestClient(create_app(cfg, svc), base_url="http://127.0.0.1:5232", client=("127.0.0.1", 50000)) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            st = c.get("/api/status").json()
            if st["capture"]["state"] == "running" and (st["capture"]["buffered_seconds"] or 0) >= 2.5:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"demo source did not start: {st}")
        c.svc = svc
        yield c


def agent(c, tool, /, **arguments):
    headers = {"Authorization": f"Bearer {c.app.state.services.token}"}
    return c.post("/api/agent/call", headers=headers, json={"name": tool, "arguments": arguments})


def test_the_page_an_assistant_reads_says_where_things_stand_and_how_to_drive_it(env):
    res = env.get("/copilot")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/plain")
    text = res.text
    assert "capture running (1280x720), no session." in text
    for needle in ("POST http://127.0.0.1:5232/copilot/show", "GET  http://127.0.0.1:5232/copilot/look.jpg", "copilot_show", '"click":"Archivo"',
                   "anchor_missing", "still_waiting"):
        assert needle in text, needle
    assert len(text) < 5500                                 # short enough to read whole
    env.post("/copilot/show", json={"agent": "Ana", "steps": ["Respira"]})
    assert "session active, step 1/1: Respira" in env.get("/copilot").text


def test_guiding_over_http_from_the_first_step_to_the_end(env):
    assert env.get("/copilot/state").json() == {"enabled": True, "rev": 0, "reads_text": True, "session": None}
    body = {"agent": "Ana", "goal": "Publicar la rama", "steps": [{"say": "Pulsa aquí", "click": [640, 360], "until": "manual"},
                                                                 {"say": "Escribe el nombre", "type": "informe", "at": [300, 200, 200, 40], "until": "manual"},
                                                                 {"say": "Guarda", "keys": "Ctrl+S", "until": "manual"}]}
    out = env.post("/copilot/show", json=body).json()
    assert out == {"session": "active", "step": 1, "of": 3, "say": "Pulsa aquí", "ends_on": "manual", "space": [1280, 720]}
    state = env.get("/copilot/state").json()["session"]
    assert state["agent"] == "Ana" and state["goal"] == "Publicar la rama" and state["count"] == 3
    assert state["step"]["marks"] == [{"kind": "click", "x": 0.5, "y": 0.5, "box": None, "to": None, "text": "", "find": None, "state": "ok", "id": "m0"}]
    assert env.get("/api/status").json()["copilot"]["session"] == "active"
    assert env.get("/copilot/wait", params={"wait_s": 0}).json()["still_waiting"] is True
    box = env.post("/copilot/user", json={"action": "next"}).json()["session"]["step"]["marks"][0]["box"]
    assert box == pytest.approx([300 / 1280, 200 / 720, 200 / 1280, 40 / 720], abs=1e-4)
    env.post("/copilot/user", json={"action": "help", "text": "¿dónde está?"})
    out = env.post("/copilot/wait", json={"wait_s": 5}).json()
    assert [e["event"] for e in out["events"]] == ["advanced", "help"] and out["events"][1]["text"] == "¿dónde está?"
    assert "_image" not in out and out["look_url"] == f"http://127.0.0.1:5232/copilot/look/{out['look']}.jpg"
    picture = env.get(out["look_url"])
    assert picture.headers["content-type"] == "image/jpeg" and picture.content[:2] == b"\xff\xd8"
    assert out["space"] == [1024, 576]
    env.post("/copilot/user", json={"action": "next"})
    env.post("/copilot/user", json={"action": "next"})
    out = env.post("/copilot/wait", json={"wait_s": 5, "look": False}).json()
    assert out["session"] == "done" and out["events"][-1] == {"event": "done", "steps": 3} and "look" not in out
    assert env.post("/copilot/stop", json={"message": "Listo"}).json() == {"session": "stopped", "by": "agent"}
    assert env.get("/copilot/state").json()["session"]["message"] == "Listo"
    assert env.get("/copilot/status").json()["session"] == "stopped"


def test_a_body_that_is_not_utf8_or_not_json_is_still_understood_or_explained(env):
    out = env.post("/copilot/show", content='{"steps": [{"say": "Pulsa aquí en el menú"}]}'.encode("cp1252"),
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    state = env.get("/copilot/state")
    assert out.status_code == 200 and state.json()["session"]["step"]["say"] == "Pulsa aquí en el menú"
    assert "charset=utf-8" in state.headers["content-type"] and "charset=utf-8" in out.headers["content-type"]
    bad = env.post("/copilot/show", content=b"say=hola")
    assert bad.status_code == 400 and "JSON" in bad.json()["error"] and "steps" in bad.json()["hint"]
    bad = env.post("/copilot/show", json={"steps": [{"say": "x", "click": [1, 2, 3]}]})
    assert bad.status_code == 400 and "text on screen" in bad.json()["hint"]
    assert env.post("/copilot/show", json={}).status_code == 400
    assert env.post("/copilot/say", json={"message": "Te leo."}).status_code == 200
    assert env.get("/copilot/state").json()["session"]["log"][-1] == {"id": 2, "who": "agent", "text": "Te leo."}
    assert env.post("/copilot/user", json={"action": "dance"}).status_code == 400
    for wrong in ({"steps": [{"say": "x", "marks": 5}]}, {"steps": [{"say": "x", "click": {"find": "a", "dx": {}}}]}):
        assert env.post("/copilot/show", json=wrong).status_code == 400
    fine = env.post("/copilot/show", json={"steps": [{"say": "x", "click": [10, 10]}], "look": "never", "wait_s": 0})
    assert fine.status_code == 200 and fine.json()["session"] == "active"
    quick = env.post("/copilot/wait", content=b'{"wait_s": NaN}')
    assert quick.status_code == 200 and quick.json()["session"] == "active"
    assert env.get("/copilot/wait", params={"wait_s": "nan"}).json()["session"] == "active"


def test_other_sites_cannot_draw(env):
    res = env.post("/copilot/show", json={"steps": ["hola"]}, headers={"Origin": "https://example.com", "Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403 and env.get("/copilot/state").json()["session"] is None


def test_looking_and_reading_the_screen(env):
    res = env.get("/copilot/look.jpg", params={"w": 640})
    assert res.headers["content-type"] == "image/jpeg" and res.headers["x-copilot-space"] == "640x360"
    from baird_hoard.media import jpeg_size
    assert jpeg_size(res.content) == (640, 360)
    assert env.get("/api/media").json()["items"] == []            # looking leaves nothing in the library
    text = env.get("/copilot/text").text.splitlines()
    assert text[0] == "# coordinates in a 640x360 picture" and "316,174 Guardar" in text
    found = env.get("/copilot/text", params={"q": "guardar", "format": "json"}).json()
    assert found["count"] == 1 and found["matches"][0]["at"] == [316, 174]
    assert env.get("/copilot/text", params={"q": "inexistente"}).text.splitlines()[1] == "# not on screen"
    zoom = env.get("/copilot/look.jpg", params={"region": "320,180,320,180"})        # the lower right quarter
    assert zoom.headers["x-copilot-space"] == "640x360"          # its own pixels: a zoom is never blown up
    env.post("/copilot/show", json={"steps": [{"say": "x", "click": [320, 180], "until": "manual"}]})
    mark = env.get("/copilot/state").json()["session"]["step"]["marks"][0]
    assert (mark["x"], mark["y"]) == (0.75, 0.75)                # the middle of what was zoomed into
    assert env.get("/copilot/look/nope.jpg").status_code == 404


def test_marks_that_name_a_text_are_placed_where_it_is(env):
    out = env.post("/copilot/show", json={"steps": [{"say": "Guarda", "click": "Guardar", "until": "manual"},
                                                    {"say": "No está", "click": "Descargar", "until": "manual"}]}).json()
    assert "searching" not in out
    mark = env.get("/copilot/state").json()["session"]["step"]["marks"][0]
    assert mark["find"] == "Guardar" and mark["state"] == "ok"
    assert mark["x"] == pytest.approx((600 + 31.5) / 1280, abs=0.005) and mark["y"] == pytest.approx(348 / 720, abs=0.005)
    env.post("/copilot/user", json={"action": "next"})
    deadline = time.time() + 20
    while time.time() < deadline:
        out = env.post("/copilot/wait", json={"wait_s": 2, "look": "never"}).json()
        if any(e["event"] == "anchor_missing" for e in out["events"]):
            break
    else:
        pytest.fail("the missing text was never reported")
    assert out["searching"] == ["Descargar"]
    assert env.get("/copilot/state").json()["session"]["step"]["marks"][0]["state"] == "searching"


def test_the_page_gets_every_change_over_its_socket(env):
    with env.websocket_connect("ws://127.0.0.1:5232/copilot/ws") as ws:
        assert ws.receive_json()["session"] is None
        env.post("/copilot/show", json={"agent": "Ana", "steps": ["Uno", "Dos"]})
        seen = ws.receive_json()
        while seen["session"] is None:
            seen = ws.receive_json()
        assert seen["session"]["step"]["say"] == "Uno"
        env.post("/copilot/user", json={"action": "next"})
        while seen["session"]["index"] != 1:
            seen = ws.receive_json()
        assert seen["session"]["step"]["say"] == "Dos"
        env.post("/copilot/user", json={"action": "stop"})
        while seen["session"]["status"] != "stopped":
            seen = ws.receive_json()
        assert seen["session"]["ended_by"] == "user"


def test_the_tools_an_assistant_gets_over_mcp(env):
    headers = {"Authorization": f"Bearer {env.svc.token}"}
    tools = {t["name"]: t for t in env.get("/api/agent/tools", headers=headers).json()["tools"]}
    assert {"copilot_show", "copilot_wait", "copilot_look", "copilot_find", "copilot_status", "copilot_stop"} <= set(tools)
    assert "required" not in tools["copilot_show"]["inputSchema"] and "message" in tools["copilot_show"]["inputSchema"]["properties"]
    said = agent(env, "copilot_show", agent="Ana", message="Hola, empezamos enseguida.", wait_s=0).json()
    assert said["session"] == "done" and env.get("/copilot/state").json()["session"]["log"][0]["text"] == "Hola, empezamos enseguida."
    look = agent(env, "copilot_look", max_side=640, text=True).json()
    assert look["_image"]["mime"] == "image/jpeg" and look["space"] == [640, 360] and "316,174 Guardar" in look["text"]
    cheap = agent(env, "copilot_look", image=False, text=True).json()
    assert "_image" not in cheap and cheap["space"] == [1024, 576] and any(t.endswith(",278 Guardar") for t in cheap["text"])
    out = agent(env, "copilot_show", agent="Ana", steps=[{"say": "Guarda", "click": "Guardar", "until": "manual"}, "Fin"], wait_s=0).json()
    assert out["session"] == "active" and out["of"] == 2
    assert agent(env, "copilot_find", text="cancelar").json()["matches"][0]["text"] == "Cancelar"
    assert agent(env, "copilot_status").json()["say"] == "Guarda"
    env.post("/copilot/user", json={"action": "next"})
    env.post("/copilot/user", json={"action": "next"})
    out = agent(env, "copilot_wait", wait_s=5).json()
    assert out["session"] == "done" and out["_image"]["data"] and out["events"][-1]["event"] == "done"
    assert agent(env, "copilot_stop", message="Listo").json() == {"session": "stopped", "by": "agent"}
    bad = agent(env, "copilot_show", steps=[{"say": "x", "colour": "red"}])
    assert bad.status_code == 400 and "Unknown step field" in bad.json()["error"]


def test_turning_it_off_ends_the_session_and_refuses_new_ones(env):
    env.post("/copilot/show", json={"steps": ["Uno"]})
    assert env.put("/api/settings", json={"copilot": {"enabled": False}}).json()["copilot"]["enabled"] is False
    state = env.get("/copilot/state").json()
    assert state["enabled"] is False and state["session"]["status"] == "stopped" and state["session"]["ended_by"] == "user"
    res = env.post("/copilot/show", json={"steps": ["Dos"]})
    assert res.status_code == 409 and "turn it on" in res.json()["hint"]
    assert "COPILOT MODE IS TURNED OFF" in env.get("/copilot").text
    assert env.put("/api/settings", json={"copilot": {"enabled": True, "look_side": 99999}}).json()["copilot"] == {
        "enabled": True, "look_side": 2048, "idle_minutes": 15.0, "ocr_lang": ""}
    assert env.post("/copilot/show", json={"steps": ["Dos"]}).status_code == 200


def test_the_frame_tap_watches_the_demo_picture_and_knows_it_never_goes_still(env):
    from baird_hoard.copilot import TAP_H, TAP_W
    env.post("/copilot/show", json={"steps": [{"say": "Pulsa", "click": [640, 360]}]})
    deadline = time.time() + 20
    while time.time() < deadline:
        step = env.get("/copilot/state").json()["session"]["step"]
        if step is not None and step["auto"] is False:
            break
        time.sleep(0.3)
    else:
        pytest.fail("the moving demo picture was never recognised as such")
    frame = env.svc.copilot.frames.latest()
    assert frame is not None and len(frame[1]) == TAP_W * TAP_H
    assert env.get("/copilot/status").json()["ends_on"] == "manual"
    env.post("/copilot/stop", json={})
    deadline = time.time() + 5
    while time.time() < deadline and env.svc.copilot.frames.latest() is not None:
        time.sleep(0.2)
    assert env.svc.copilot.frames.latest() is None            # no session, no tap


@pytest.mark.skipif(os.name != "nt", reason="the text reader is the OCR of Windows")
def test_the_windows_reader_finds_a_label_on_a_drawn_picture(tmp_path):
    from baird_hoard.errors import BairdError
    from baird_hoard.textmap import TextReader, find_text

    font = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf").replace("\\", "/").replace(":", "\\:")
    picture = tmp_path / "pantalla.bmp"
    draw = (f"drawtext=fontfile='{font}':text='Archivo  Editar  Ver':x=20:y=20:fontsize=26:fontcolor=white,"
            f"drawtext=fontfile='{font}':text='Publicar rama':x=1500:y=140:fontsize=28:fontcolor=white,"
            f"drawtext=fontfile='{font}':text='Guardar':x=900:y=620:fontsize=30:fontcolor=white")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1f2430:s=1920x1080", "-vf", draw,
                    "-frames:v", "1", str(picture)], check=True)
    reader = TextReader()
    try:
        try:
            result = reader.read(picture, "")
        except BairdError as exc:
            pytest.skip(f"Windows OCR not usable here: {exc}")
        assert (result["w"], result["h"]) == (1920, 1080) and result["lang"]
        found = find_text(result["lines"], "publicar rama")
        assert found and found[0]["exact"]
        x, y, w, h = found[0]["box"]
        assert 1480 <= x <= 1520 and 130 <= y <= 170 and w > 100
        assert find_text(result["lines"], "Guardar")[0]["box"][0] == pytest.approx(900, abs=20)
        assert reader.read(picture)["lines"]                    # the helper stays up between readings
    finally:
        reader.stop()
