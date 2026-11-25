from pathlib import Path
from threading import Event
from types import SimpleNamespace

from baird_hoard.capture_feedback import CaptureFeedback
from baird_hoard.recording_search import RecordingSearch, Report


def page(name, start):
    lines = [[1, 0, 100, 12, name, [[1,0,100,12,name]]]]
    for i in range(8):
        n, y = start+i, 30+20*i
        word = f"identifier_{n}"
        lines.append([10,y,150,12,f"{n} {word}",[[10,y,12,12,str(n)],[50,y,100,12,word]]])
    return lines


def test_live_gap_recovers_only_after_missing_range_is_seen():
    feedback = CaptureFeedback()
    assert feedback.observe(page("src/a.py",1),0) is None
    assert "9–14" in feedback.observe(page("src/a.py",15),1)["text"]
    assert "13–14" in feedback.observe(page("src/a.py",5),2)["text"]
    assert feedback.observe(page("src/a.py",10),3)["kind"] == "recovered"
    assert feedback.observe(page("src/b.py",150),4) is None
    assert feedback.observe(page("src/a.py",10),5) is None


def test_partial_occlusion_with_gutter_still_visible():
    good = page("src/a.py",20)
    feedback = CaptureFeedback()
    assert feedback.observe(good,0) is None
    covered = [row if i not in (3,4,5) else [*row[:4],row[5][0][4],row[5][:1]] for i,row in enumerate(good)]
    assert feedback.observe(covered,1)["kind"] == "visibility"
    assert feedback.observe(good,2) is None


def test_index_existing_reports_links_video_and_handles_rename_and_pagination(tmp_path):
    library = SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path),
        folder=tmp_path,recorder=SimpleNamespace(is_writing=lambda p:False),
        describe=lambda p:{"name":p.name})
    movie = tmp_path/"demo #2.mp4"
    movie.touch()
    doc = tmp_path/"pages.html"
    doc.write_text('<p data-baird-video="demo #2.mp4">Demo</p>\n' +
                   "".join(f'<section><h2>00:{i:02d}.5</h2><pre>nuevo</pre><details><pre>café item_{i}</pre></details></section>\n'
                           for i in range(35)) +
                   '<h3>src/demo.py</h3><a href="#vista-00:03.5">Ver vista</a>',encoding="utf8")
    search = RecordingSearch(library)
    # Exercise the real persisted timeline without requesting OCR of the dummy video.
    search.stop_event.set()
    search.initialize()
    with search.connect() as db:
        search.inventory(db)
        report = Report.read(doc,Event())
        sig = db.execute("SELECT signature FROM assets WHERE path=?",(str(doc),)).fetchone()[0]
        search.save(db,doc,sig,[(s,i,t,"\n".join(report.paths[s])) for i,(s,t) in enumerate(report.frames)],
                    0,source=report.source,done=True)
    hits,_ = search.search(["cafe"],1)
    assert hits[0]["name"] == movie.name and hits[0]["moment_count"] == 35
    assert len(hits[0]["moments"]) == 30
    tail,_ = search.search(["cafe"],1,recording=movie.name,after=hits[0]["next_after"])
    assert [m["seconds"] for m in tail[0]["moments"]] == [30.5,31.5,32.5,33.5,34.5]
    replacement = tmp_path/"renamed.mp4"
    movie.rename(replacement)
    search.renamed(movie.name,replacement.name)
    hits,_ = search.search(["src/demo.py"],1)
    assert hits[0]["name"] == replacement.name and hits[0]["moments"][0]["seconds"]==3.5
    replacement.unlink()
    hits,_ = search.search(["item_34"],1)
    assert hits[0]["name"] == doc.name and hits[0]["moments"][0]["view"] == 34
    doc.write_text("<p>changed</p>")
    assert not search.search(["item_34"],1)[0]
