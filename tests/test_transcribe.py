import base64
import html
import re
import threading
import time
from pathlib import Path

import pytest

from baird_hoard.transcribe import extract_frames, new_lines
from .conftest import FFMPEG, make_ts
from .test_copilot_app import agent, env


def test_overlap_keeps_new_text_and_repetitions():
    assert new_lines(["Uno", "Dos", "Tres"], ["Dos", "Tres", "Dos", "Cuatro"]) == ["Dos", "Cuatro"]
    assert new_lines(["def createID():"], ["def createId():"]) == ["def createId():"]
    assert new_lines(["ABC = 1"], ["ABC = 2"]) == ["ABC = 2"]
    assert new_lines(["x", "A", "x", "B"], ["x", "B", "x", "C"]) == ["x", "C"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg missing")
def test_frames_drain_the_recording_before_finishing(tmp_path):
    video = tmp_path / "document.ts"
    make_ts(video, 2.5)
    finished = threading.Event()
    finished.set()
    with_region = {"x": 0, "y": 0, "w": 160, "h": 90, "unit": "px"}
    from baird_hoard.media import jpeg_size
    frames = [(int(p.stem), jpeg_size(p.read_bytes())) for p in extract_frames(FFMPEG, video, finished, tmp_path, with_region)]
    assert frames[0] == (0, (160, 90))
    assert frames[-1][0] >= 3


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg missing")
def test_document_and_recording_reach_the_library(env, monkeypatch):
    from baird_hoard import transcribe
    from baird_hoard.errors import BairdError

    class Reader:
        calls = 0

        def read(self, frame, lang):
            self.calls += 1
            if self.calls == 2:
                raise BairdError("unavailable", "test OCR outage")
            return {"lines": [[0, 0, 80, 20, "A < B & C"]]}

        def stop(self):
            pass

    monkeypatch.setattr(transcribe, "TextReader", Reader)
    assert agent(env, "transcribe_start", region={"x": 0, "y": 0, "w": 640, "h": 360}).is_success
    assert agent(env, "transcribe_start").status_code == 409
    assert agent(env, "record_start").status_code == 409
    timeout = time.monotonic() + 25
    while env.get("/api/status").json()["transcription"]["views"] < 2:
        assert time.monotonic() < timeout
        time.sleep(.2)
    assert env.post("/api/ui/call", json={"name": "transcribe_stop"}).is_success
    timeout = time.monotonic() + 25
    while (report := env.get("/api/status").json())["recording"]["state"] != "idle":
        assert time.monotonic() < timeout
        time.sleep(.2)
    result = report["transcription"]
    assert result["state"] == "partial" and result["unread"] == 1
    assert Path(result["path"]).is_file()
    assert Path(report["recording"]["last"]["path"]).is_file()
    docs = env.get("/api/media?kind=document").json()["items"]
    assert [d["name"] for d in docs] == [result["name"]]
    response = env.get(docs[0]["url"])
    assert "<script" not in response.text and "transcripcion.txt" in response.text
    assert html.escape("A < B & C") in response.text and "OCR no disponible" in response.text
    pictures = re.findall(r'data:image/jpeg;base64,([^"]+)', response.text)
    assert len(pictures) == result["views"]
    assert all(base64.b64decode(p).startswith(b"\xff\xd8") for p in pictures)
    assert agent(env, "transcribe_stop").status_code == 409
