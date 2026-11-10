"""Zone rectangles, shortcuts, the screen watcher's rules, the text index and the Faustus hand-off, without a device or ffmpeg."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from baird_hoard.errors import BairdError
from baird_hoard.hotkeys import normalize_hotkey, parse_hotkey, call_app
from baird_hoard.ocr import OcrIndex, fold, snippet
from baird_hoard.region import crop_filter, parse_region
from baird_hoard.watch import WatchEngine, changed_percent, clean_rule


# ---------------------------------------------------------------- regions

def test_region_fraction_and_px():
    r = parse_region({"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.5, "unit": "fraction"})
    assert crop_filter(r) == "crop=w=iw*0.5:h=ih*0.5:x=iw*0.1:y=ih*0.2"
    px = parse_region({"x": 100, "y": 50, "w": 5000, "h": 200}, (1920, 1080))
    assert px == {"x": 100, "y": 50, "w": 1820, "h": 200, "unit": "px"}      # cut to the picture
    assert "min(1820\\,iw-100)" in crop_filter(px)
    assert parse_region(None) is None


@pytest.mark.parametrize("bad", [
    {"x": 0, "y": 0, "w": 0, "h": 10}, {"x": 5000, "y": 0, "w": 10, "h": 10, "unit": "px"}, {"x": "a", "y": 0, "w": 1, "h": 1},
    {"x": 0.5, "y": 0.5, "w": 0.0001, "h": 0.5, "unit": "fraction"}, {"x": 0, "y": 0, "w": 1, "h": 1, "unit": "cm"}, "nope"])
def test_region_rejects(bad):
    with pytest.raises(BairdError):
        parse_region(bad, (1920, 1080))


# ---------------------------------------------------------------- shortcuts

def test_hotkey_parsing():
    assert parse_hotkey("ctrl+alt+c") == (0x2 | 0x1, ord("C"))
    assert parse_hotkey("Ctrl + Shift + F9")[1] == 0x6F + 9
    assert parse_hotkey("printscreen")[1] == 0x2C
    assert normalize_hotkey("Alt+CTRL+B") == "ctrl+alt+b"
    for bad in ("", "c", "ctrl+", "ctrl+alt+ñ", "hyper+c", "shift+c", "ctrl+f99"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)


def test_hotkey_helper_calls_the_app():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            body = json.dumps({"name": "Captura x.png"}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call_app(srv.server_address[1], "/api/snapshot") == {"ok": True, "name": "Captura x.png"}
        assert seen == ["/api/snapshot"]
    finally:
        srv.shutdown()
    assert call_app(1, "/api/snapshot", timeout=2)["ok"] is False


# ---------------------------------------------------------------- the watcher

class FakeEngine:
    def snapshot(self):
        return {"state": "running"}

    def require_picture(self):
        pass


class FakeLibrary:
    def __init__(self):
        self.frames: list[bytes] = []
        self.shots: list[dict] = []

    def input_size(self):
        return (1280, 720)

    def grab_gray(self, region):
        return self.frames.pop(0)

    def grab_file(self, region, dest):
        Path(dest).write_bytes(b"png")
        return dest

    def snapshot(self, fmt, region, prefix=None, origin=None):
        self.shots.append({"region": region, "prefix": prefix, "origin": origin})
        return {"name": f"{prefix} {len(self.shots)}.png"}


def make_watch(tmp_path, ocr=None, notified=None):
    clock = [1000.0]
    lib = FakeLibrary()
    bus = []
    notes = [] if notified is None else notified
    eng = WatchEngine(tmp_path / "watch.json", lib, FakeEngine(), tmp_path, ocr_fn=ocr or (lambda p: {"ok": True, "text": ""}),
                      notify_fn=lambda t, b, k: notes.append((t, b)) or {"ok": True},
                      emit_fn=lambda t, d: bus.append((t, d)), clock=lambda: clock[0])
    return eng, lib, clock, bus, notes


def grey(percent_changed: int, base: int = 100) -> bytes:
    return bytes([200] * percent_changed + [base] * (100 - percent_changed))


def test_changed_percent():
    assert changed_percent(grey(0), grey(10)) == 10.0
    assert changed_percent(bytes([100] * 10), bytes([120] * 10)) == 0.0          # under the step
    assert changed_percent(b"", b"") == 0.0


def test_clean_rule_defaults_and_errors():
    r = clean_rule({"kind": "change", "region": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3, "unit": "fraction"}})
    assert r["mode"] == "change" and r["threshold"] == 5.0 and r["interval_s"] == 2.0 and r["name"]
    assert clean_rule({"kind": "text", "text": "Listo", "mode": "disappears"})["name"] == "Desaparece «Listo»"
    for bad in ({"kind": "text"}, {"kind": "change", "mode": "appears"}, {"kind": "video"}, {"threshold": 500},
                {"kind": "change", "interval_s": 0}):
        with pytest.raises(BairdError):
            clean_rule(bad)


def test_change_rule_fires_once_per_cooldown(tmp_path):
    eng, lib, clock, bus, notes = make_watch(tmp_path)
    rule = eng.add({"kind": "change", "threshold": 5, "name": "Barra"})
    lib.frames = [grey(0), grey(0), grey(10), grey(30)]
    assert eng.check(rule["id"])["fired"] is False            # first look: the starting point
    assert eng.check(rule["id"])["value"] == 0.0
    clock[0] += 5
    res = eng.check(rule["id"])
    assert res["fired"] and "10" in res["detail"]
    assert eng.check(rule["id"])["fired"] is False            # inside the 60 s cooldown
    events = eng.events()
    assert len(events) == 1 and events[0]["image"] == "Aviso 1.png" and lib.shots[0]["origin"] == "watch"
    assert bus[0][0] == "baird.watch" and bus[0][1]["rule"] == "Barra"
    for _ in range(50):
        if notes:
            break
        threading.Event().wait(0.05)
    assert notes and notes[0][0] == "Baird: Barra"
    assert eng.summary()["last_event"]["rule"] == "Barra"


def test_still_rule_fires_when_activity_stops(tmp_path):
    eng, lib, clock, _bus, _notes = make_watch(tmp_path)
    rule = eng.add({"kind": "change", "mode": "still", "still_seconds": 20, "threshold": 5})
    lib.frames = [grey(0), grey(0), grey(20), grey(20), grey(20), grey(20), grey(20)]
    rid = rule["id"]
    assert not eng.check(rid)["fired"]                         # baseline
    assert not eng.check(rid)["fired"]                         # never moved: nothing to wait for
    clock[0] += 5; assert not eng.check(rid)["fired"]          # moving
    clock[0] += 5; lib.frames[0] = grey(20); assert not eng.check(rid)["fired"]      # quiet starts (frame identical)
    clock[0] += 10; assert not eng.check(rid)["fired"]
    clock[0] += 15
    res = eng.check(rid)
    assert res["fired"] and "sin cambiar" in res["detail"]


def test_text_rule_appears_and_disappears(tmp_path):
    shown = {"text": "compilando..."}
    eng, _lib, clock, _bus, _n = make_watch(tmp_path, ocr=lambda p: {"ok": True, "text": shown["text"]})
    appear = eng.add({"kind": "text", "text": "TERMINÓ", "interval_s": 5})
    gone = eng.add({"kind": "text", "text": "compilando", "mode": "disappears", "interval_s": 5})
    assert not eng.check(appear["id"])["fired"] and not eng.check(gone["id"])["fired"]      # starting points
    shown["text"] = "Build termino sin errores"            # accents are ignored
    clock[0] += 10
    a, g = eng.check(appear["id"]), eng.check(gone["id"])
    assert a["fired"] and "TERMINÓ" in a["detail"]
    assert g["fired"] and g["detail"].startswith("Desapareció")


def test_text_rule_without_ocr_reports_the_reason(tmp_path):
    eng, *_ = make_watch(tmp_path, ocr=lambda p: {"ok": False, "error": "hub unreachable"})
    rule = eng.add({"kind": "text", "text": "x"})
    res = eng.check(rule["id"])
    assert res["fired"] is False and "OCR not available" in res["error"]
    assert eng.list()["rules"][0]["status"]["error"]


def test_rules_persist_and_update(tmp_path):
    eng, *_ = make_watch(tmp_path)
    rule = eng.add({"kind": "change", "name": "A"})
    eng.update(rule["id"], {"enabled": False, "threshold": 12})
    again, *_ = make_watch(tmp_path)
    saved = again.list()["rules"][0]
    assert saved["enabled"] is False and saved["threshold"] == 12 and saved["name"] == "A" and again.summary()["active"] == 0
    again.remove(rule["id"])
    with pytest.raises(BairdError):
        again.remove(rule["id"])


# ---------------------------------------------------------------- text index

class FakeIndexLibrary:
    def __init__(self, folder: Path):
        self.folder = folder

    def list(self, kind="all", limit=200):
        out = []
        for p in sorted(self.folder.glob("*.png")):
            out.append({"name": p.name, "path": str(p), "kind": "image", "modified": "", "size": p.stat().st_size})
        return out

    def path_of(self, name):
        p = self.folder / name
        if not p.is_file():
            raise BairdError("not_found", "no file")
        return p


class Opts(dict):
    pass


def test_fold_and_snippet():
    assert fold("  ÁRBOL   Compilación ") == "arbol compilacion"
    assert "Compilación" in snippet("Línea uno.\nEl log de la Compilación terminó bien", ["compilacion"])


def test_ocr_index_reads_searches_and_follows_renames(tmp_path):
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(b"png" + name.encode())
    texts = {"a.png": "Error de compilación en línea 4", "b.png": "Todo correcto"}
    calls = []

    def fake(path):
        calls.append(path)
        return {"ok": True, "text": texts[Path(path).name], "backend": "fake"}

    settings = Opts(ocr_auto=True)
    idx = OcrIndex(tmp_path / "ocr.json", FakeIndexLibrary(tmp_path), settings, fake, background=False)
    assert idx.read_now("a.png")["text"].startswith("Error")
    assert idx.search("COMPILACION error")["items"][0]["name"] == "a.png"
    assert idx.search("compilacion")["items"][0]["snippet"].startswith("Error de compilación")
    assert idx.search("error")["unread"] == 1                       # b.png is not read yet
    idx.read_now("b.png")
    assert idx.search("todo")["items"][0]["name"] == "b.png" and idx.search("todo")["unread"] == 0
    assert idx.search("nada que ver")["items"] == []
    with pytest.raises(BairdError):
        idx.search("   ")
    idx.renamed("a.png", "c.png")
    (tmp_path / "a.png").rename(tmp_path / "c.png")
    assert idx.search("error")["items"][0]["name"] == "c.png"
    idx.deleted("c.png")
    assert idx.search("error")["items"] == []
    again = OcrIndex(tmp_path / "ocr.json", FakeIndexLibrary(tmp_path), settings, fake, background=False)
    assert again.text_of("a.png") is None and again.text_of("c.png") is None and again.text_of("b.png") == "Todo correcto"


def test_ocr_failure_is_reported_and_retried(tmp_path):
    (tmp_path / "a.png").write_bytes(b"png")
    state = {"ok": False}

    def fake(path):
        return {"ok": True, "text": "hola"} if state["ok"] else {"ok": False, "error": "hub unreachable", "kind": "hub_down"}

    idx = OcrIndex(tmp_path / "ocr.json", FakeIndexLibrary(tmp_path), Opts(ocr_auto=True), fake, background=False)
    with pytest.raises(BairdError) as exc:
        idx.read_now("a.png")
    assert exc.value.code == "unavailable" and idx.status()["error"]
    state["ok"] = True
    assert idx.read_now("a.png")["text"] == "hola"
    assert idx.status()["indexed"] == 1 and idx.status()["error"] == ""


# ---------------------------------------------------------------- Faustus hand-off

class FakeFaustus:
    def __init__(self):
        self.calls: list[tuple[str, str, bytes]] = []
        owner = self

        class H(BaseHTTPRequestHandler):
            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                owner.calls.append(("GET", self.path, b""))
                self._send(200, {"status": "healthy"})

            def do_POST(self):
                body = self._read()
                owner.calls.append(("POST", self.path, body))
                if self.path == "/api/hoard-copilot/conversations":
                    return self._send(owner.conversation_code, {"session_id": "sess-1"} if owner.conversation_code < 400 else {"detail": "no model"})
                if self.path == "/api/session":
                    return self._send(200, {"id": "sess-2"})
                if self.path == "/api/upload":
                    return self._send(200, {"files": [{"id": "file-9", "name": "x"}]})
                self._send(404, {})

            def do_PUT(self):
                owner.calls.append(("PUT", self.path, self._read()))
                self._send(200, {"ok": True})

            def log_message(self, *a):
                pass

        self.conversation_code = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_faustus():
    f = FakeFaustus()
    yield f
    f.close()


def test_send_file_creates_chat_uploads_and_fills_the_draft(tmp_path, fake_faustus):
    from baird_hoard import faustus
    f = tmp_path / "Captura 1.png"
    f.write_bytes(b"\x89PNG-data")
    res = faustus.send_file(f, "mira esto", base_url=fake_faustus.url)
    assert res["session_id"] == "sess-1" and res["url"] == f"{fake_faustus.url}/studio?s=sess-1"
    paths = [(m, p) for m, p, _ in fake_faustus.calls]
    assert paths == [("POST", "/api/hoard-copilot/conversations"), ("POST", "/api/upload"), ("PUT", "/api/sessions/sess-1/draft")]
    upload = fake_faustus.calls[1][2]
    assert b'filename="Captura 1.png"' in upload and b"sess-1" in upload and b"\x89PNG-data" in upload
    assert json.loads(fake_faustus.calls[2][2]) == {"text": "mira esto", "attachment_ids": ["file-9"]}
    assert json.loads(fake_faustus.calls[0][2])["context"]["origin"]["app_id"] == "baird"


def test_send_file_falls_back_to_a_plain_session(tmp_path, fake_faustus):
    from baird_hoard import faustus
    fake_faustus.conversation_code = 500
    f = tmp_path / "c.png"
    f.write_bytes(b"x" * 100)
    res = faustus.send_file(f, base_url=fake_faustus.url)
    assert res["session_id"] == "sess-2"
    assert ("POST", "/api/session") in [(m, p) for m, p, _ in fake_faustus.calls]


def test_find_faustus_uses_the_hub_placement_and_skips_dead_ones(monkeypatch, fake_faustus):
    from baird_hoard import faustus
    monkeypatch.delenv("BAIRD_FAUSTUS_URL", raising=False)
    monkeypatch.delenv("HOARD_FAUSTUS_URL", raising=False)
    hub = lambda url, **kw: (200, {"location": "sparks", "job": {"running": False}, "urls": {"faustus": "https://spark.example/"}})
    urls = faustus.faustus_candidates(hub)
    assert urls[0] == "https://spark.example" and urls[-2:] == list(faustus.LOCAL_URLS)
    moving = lambda url, **kw: (200, {"location": "sparks", "job": {"running": True}, "urls": {"faustus": "https://spark.example"}})
    assert "https://spark.example" not in faustus.faustus_candidates(moving)
    assert faustus.find_faustus(["http://127.0.0.1:9", fake_faustus.url]) == fake_faustus.url
    with pytest.raises(BairdError) as exc:
        faustus.find_faustus(["http://127.0.0.1:9"])
    assert exc.value.code == "unavailable"


def test_send_file_refuses_huge_files(tmp_path, monkeypatch):
    from baird_hoard import faustus
    f = tmp_path / "big.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(faustus, "MAX_UPLOAD", 0)
    with pytest.raises(BairdError) as exc:
        faustus.send_file(f, base_url="http://127.0.0.1:9")
    assert exc.value.code == "invalid"
