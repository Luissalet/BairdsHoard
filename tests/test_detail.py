import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from baird_hoard import detail_job
from baird_hoard.detail_worker import new_tail, views
from baird_hoard.document_review import original_view
from baird_hoard.errors import BairdError


def test_code_review_locates_disagreement_and_reads_old_line_warnings(tmp_path):
    from baird_hoard.detail_worker import conflict_area
    focus = conflict_area("safe_load", "save_load", 7, True, 0, 63, 21, 32, 2, 0)
    assert focus == [2, 2, 3, 14, 21, 7, 11]
    assert conflict_area("safe_load", "save_load", 7, False, 0, 63, 21, 32, 2, 0)[1:3] == [-1, -1]
    doc = tmp_path / "sample.html"
    frame = '<section><h2>00:00.0</h2><pre>x\n\nsafe_load</pre>{}<img src="data:image/jpeg;base64,/9j/"></section>\n'
    doc.write_text(frame.format('<span hidden data-ocr-areas="[[2,2,3,14,21,7,11],[9,2,3,14,21,7,11]]"></span>'), encoding="utf-8")
    assert original_view(doc)["areas"] == [focus]
    doc.write_text(frame.format('<p>Línea visible 2 (y=21 px). Revisar con la imagen.</p>'), encoding="utf-8")
    assert original_view(doc)["areas"] == [[2,-1,-1,0,21,0,22]]


def test_pitch_rejects_grid_harmonic_only_with_supporting_lines():
    np = pytest.importorskip("numpy")
    from baird_hoard.detail_worker import estimate_pitch
    frequencies = 1 / np.array([21., 7.])
    assert estimate_pitch([[1., .3]], frequencies, [6.9, 7., 7.1, 14.]) == (7., True)
    assert estimate_pitch([[1., .3]], frequencies, [6.9]) == (21., False)


def test_delimiter_recovery_needs_a_matching_glyph_in_another_row():
    np = pytest.importorskip("numpy")
    from baird_hoard.detail_worker import repeated_delimiters
    ink = np.zeros((60,100))
    ink[5:15,4:8] = ink[25:35,4:8] = 100
    ink[45:55,4] = 100
    rows = [(y,y+10,4,8,"","",True) for y in (5,25,45)]
    blocks = [("text", (40, 82, 80, 250), "}")]
    assert repeated_delimiters(ink, rows, blocks, (100,60), 7, 20) == {1: "}"}


def test_only_embedded_frames_with_baird_timestamps_are_read(tmp_path):
    doc = tmp_path / "sample.html"
    doc.write_text('<img src="https://example.invalid/any">\n<section><h2>00:01.5</h2><pre>1\n2</pre>'
                   '<details><img loading="lazy" src="data:image/jpeg;base64,/9j/"></details></section>\n'
                   '<img src="data:image/jpeg;base64,AAAA">\n', encoding="utf-8")
    assert list(views(doc)) == [("00:01.5", "/9j/")]


def test_detailed_export_only_deduplicates_contiguous_overlap():
    earlier = ["  x", "end", "", "  y", "end"]
    assert new_tail(earlier, ["  y", "end", "  z", "end"]) == ["  z", "end"]
    assert new_tail(earlier, ["end", "  new"]) == ["end", "  new"]
    assert new_tail(earlier, ["y", "end", "  z"]) == ["y", "end", "  z"]
    assert new_tail(earlier, earlier) == []


def test_review_reads_one_original_and_prefers_full_text(tmp_path):
    doc = tmp_path / "source.html"
    doc.write_text('<section><h2>00:01.0</h2><pre>added</pre><details><pre>  x &lt; y\nline</pre></details>'
                   '<img src="data:image/jpeg;base64,/9j/"></section>\n'
                   '<section><h2>00:02.5</h2><pre>  z</pre><p>Revisar<br>símbolos</p>'
                   '<img src="data:image/jpeg;base64,/9j/"></section>\n', encoding="utf-8")
    first, last = original_view(doc), original_view(doc, 1)
    assert first["text"] == "  x < y\nline"
    assert last["text"] == "  z" and last["notes"] == "Revisar\nsímbolos"
    assert first["stamps"] == ["00:01.0", "00:02.5"] and first["version"] == last["version"]
    with pytest.raises(BairdError):
        original_view(doc, 2)


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_rereading_publishes_only_finished_documents(tmp_path, monkeypatch, outcome):
    import sys
    config = SimpleNamespace(data_dir=tmp_path)
    library = SimpleNamespace(config=config, _new_path=lambda *args: tmp_path / "result.html")
    source = tmp_path / "original.html"
    source.write_text("original", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.safetensors").touch()
    with pytest.raises(BairdError):
        detail_job.DetailJob(library).start(source, "document")
    (tmp_path / "unlimited-ocr.json").write_text(json.dumps({"python": sys.executable, "model": str(model)}))
    job = detail_job.DetailJob(library)
    started, allow_exit, released = threading.Event(), threading.Event(), threading.Event()

    class Lease:
        def __init__(self, *args, **kwargs):
            self.gpu = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            released.set()

    class Process:
        def __init__(self, args, **kw):
            self.stdout = io.StringIO('{"views":1}\n')
            Path(args[-1]).write_text("second reading")
            started.set()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self):
            assert allow_exit.wait(5)
            return int(outcome != "done")

        def poll(self):
            return None if not allow_exit.is_set() else int(outcome != "done")

        def terminate(self):
            allow_exit.set()

    monkeypatch.setattr(detail_job, "Lease", Lease)
    monkeypatch.setattr(detail_job, "bind_to_app", lambda proc: None)
    monkeypatch.setattr(detail_job.subprocess, "Popen", Process)
    assert job.start(source, "code")["state"] == "running"
    assert started.wait(5)
    with pytest.raises(BairdError):
        job.start(source, "document")
    if outcome == "cancelled":
        job.cancel()
    allow_exit.set()
    deadline = time.monotonic() + 5
    while job.status()["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert job.status()["state"] == outcome
    assert released.is_set() and source.read_text() == "original"
    assert (tmp_path / "result.html").exists() == (outcome == "done")
    assert not list(tmp_path.glob("*.part"))
