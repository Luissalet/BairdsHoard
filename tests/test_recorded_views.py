import base64
import csv
import io
import re
import zipfile

from baird_hoard.source_rebuild import Reconstruction, numbered_slots, source_label


def test_require_file_evidence_in_breadcrumb_or_selected_url():
    assert source_label(["project > src > app > thing.py ⧉"]) == ("src/app/thing.py", "")
    assert source_label(["src > app > thing.py"]) == ("src/app/thing.py", "")
    assert source_label(["search foo.py and bar.py", "side.py", "other.py"]) == ("", "")
    assert source_label(["https://example.invalid/?selected=one%3Asrc%2Fapp%2Fthi..."]) == ("", "src/app/thi")


def test_gutter_tolerates_one_bad_digit_and_leaves_fold_boundary_unresolved():
    layout = {"step": 20, "center": 10, "box": (50,0,320,260), "gutter": (10,26)}
    printed = [21,22,23,84,25,26,27,80,81,82,83,84,85]
    words = [(10, i*20+3, 16, 14, str(n)) for i,n in enumerate(printed)]
    words += [(120, i*20+3, 16, 14, "99") for i in range(13)]
    numbered = numbered_slots(words, layout, 1, (0,0))
    assert numbered[3] == 24 and numbered[11] == 84
    assert set(numbered.values()).isdisjoint(range(28,80))


def test_export_overlapping_numbered_files_unseen_ranges_conflicts_and_provenance():
    rebuilt = Reconstruction()
    local = "src/app/a.py"
    rebuilt.add("00:00.0", 0, local, "", {1: ("def beginning():", True), 2: ("    return key", True), 3: ("", True)})
    rebuilt.add("00:01.5", 1.5, "", "", {1: ("def beginning():", True), 2: ("    return key", True), 4: ("end", True)})
    rebuilt.add("00:03.0", 3, local, "", {4: ("and", False), 9: ("tail", True)})
    rebuilt.add("00:04.5", 4.5, "src/b/a.py", "", {1: ("different", True)})
    rebuilt.add("00:06.0", 6, "", "", {1: ("new fragment", True)})
    buf = io.StringIO()
    rebuilt.export(buf, "a #1.mp4")
    result = buf.getvalue()
    assert '#vista-00:03.0' in result and 'a%20%231.mp4#t=3' in result
    encoded = re.search(r'data:application/zip;base64,([^"]+)', result)[1]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded))) as archive:
        assert archive.read(local+".txt").decode().splitlines() == [
            "def beginning():", "    return key", "", "end", "[Líneas 5–8 sin capturar]", "tail"]
        assert "src/b/a.py.txt" in archive.namelist()
        assert any(n.startswith("Fragmento sin ruta") for n in archive.namelist())
        entries = list(csv.DictReader(io.StringIO(archive.read("indice.tsv").decode("utf-8-sig")), delimiter="\t"))
        assert any(e["linea_inicial"] == "4" and e["estado"] == "lecturas distintas" for e in entries)
        assert any(e["linea_inicial"] == "5" and e["estado"] == "sin capturar" for e in entries)


def test_live_document_only_links_existing_sibling_recording(tmp_path):
    from baird_hoard.detail_worker import linked_video
    report = tmp_path/"document.html"
    (tmp_path/"My #1.mp4").touch()
    report.write_text('<p data-baird-video="My #1.mp4">source</p>')
    assert linked_video(report) == "My #1.mp4"
    report.write_text('<p data-baird-video="../My #1.mp4">source</p>')
    assert linked_video(report) is None
