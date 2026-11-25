"""A local timeline index for saved reports and recordings. Search never waits for video OCR."""
from collections import defaultdict
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote

from .media import VIDEO_EXT
from .native import NO_WINDOW, bind_to_app
from .ocr import fold, snippet
from .source_rebuild import source_label
from .textmap import TextReader

STAMP = re.compile(r"^(\d+):([0-5]\d(?:\.\d+)?)$")
PAYLOAD = re.compile(r'(data:[^",\s]*;base64,)[A-Za-z0-9+/=]+')
LIMIT = 32 * 1024 * 1024


def timestamp(label):
    m = STAMP.fullmatch(label.strip())
    return 60*int(m[1])+float(m[2]) if m else None


class Report(HTMLParser):
    """Read only timestamped content and the source ranges emitted by Baird."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.source = ""
        self.frames = []
        self.paths = defaultdict(set)
        self.stamp = None
        self.section = False
        self.field = ""
        self.parts = []
        self.body = ""
        self.path = ""
        self.video_links = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.section, self.stamp, self.body = True, None, ""
        if tag == "p" and attrs.get("data-baird-video"):
            self.source = attrs["data-baird-video"]
        if ((self.section and tag in ("h2", "pre")) or
                (not self.section and (tag == "h3" or (tag == "p" and not self.frames)))):
            self.field, self.parts = tag, []
        if tag == "a" and not self.section and self.path:
            target = attrs.get("href") or ""
            if target.startswith("#vista-") and (sec := timestamp(target[7:])) is not None:
                self.paths[sec].add(self.path)
            name, marker, fragment = target.partition("#t=")
            name = unquote(name)
            if (marker and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",fragment) and
                    not any(x in name for x in ("/","\\",":")) and Path(name).suffix.lower() in VIDEO_EXT):
                self.video_links.add(name)

    def handle_data(self, data):
        if self.field:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.field == tag:
            value = "".join(self.parts).strip()
            self.field, self.parts = "", []
            if tag == "h2":
                self.stamp = timestamp(value)
            elif tag == "pre":
                self.body = value  # Older capture reports put the full view after the differential.
            elif tag == "h3":
                self.path = value
            elif tag == "p" and not self.source:
                if value.startswith("Segunda lectura local con UnlimitedOCR · "):
                    self.source = value.split(" · ", 1)[1]
                elif value.startswith("Grabación "):
                    self.source = value if Path(value).suffix.lower() in VIDEO_EXT else value + ".mp4"
        if tag == "section" and self.section:
            if self.stamp is not None:
                self.frames.append((self.stamp, self.body))
            self.section = False

    @classmethod
    def read(cls, path, stop):
        parsed = cls()
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            while line := stream.readline(LIMIT):
                if stop.is_set():
                    raise InterruptedError()
                if len(line) == LIMIT:
                    raise ValueError("Vista de transcripción demasiado grande")
                parsed.feed(PAYLOAD.sub("", line))
        parsed.close()
        if len(parsed.video_links) == 1 and Path(parsed.source).suffix.lower() not in VIDEO_EXT:
            parsed.source = parsed.video_links.pop()
        return parsed


def jpeg_frames(proc):
    buf = bytearray()
    start = 0
    while chunk := proc.stdout.read(65536):
        buf.extend(chunk)
        while (end := buf.find(b"\xff\xd9", start)) >= 0:
            if not buf.startswith(b"\xff\xd8"):
                raise ValueError("Fotograma JPEG no válido")
            yield bytes(buf[:end+2])
            del buf[:end+2]
            start = 0
        if len(buf) > LIMIT:
            raise ValueError("Fotograma demasiado grande")
        start = max(0,len(buf)-1)
    if buf or proc.wait():
        raise ValueError("No se pudo decodificar el vídeo")


class RecordingSearch:
    def __init__(self, library):
        self.library = library
        self.db = library.config.data_dir / "recording-search.sqlite"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.decoder = None
        self.initialized = False
        self.fts = False
        self.busy = ""

    def connect(self):
        db = sqlite3.connect(self.db, timeout=12)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialize(self):
        if not self.initialized:
            with closing(self.connect()) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS assets (
                        path TEXT PRIMARY KEY, signature TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL DEFAULT 'pending', tick INTEGER NOT NULL DEFAULT 0, retry REAL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS frames (
                        path TEXT REFERENCES assets(path) ON UPDATE CASCADE ON DELETE CASCADE,
                        sec REAL, ordinal INTEGER, body TEXT, context TEXT, normalized TEXT,
                        PRIMARY KEY(path, sec)
                    );
                """)
                existing = db.execute("SELECT 1 FROM sqlite_master WHERE name='frame_text'").fetchone()
                try:
                    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS frame_text USING fts5(normalized,content='frames',content_rowid='rowid',tokenize='trigram')")
                except sqlite3.OperationalError as exc:
                    if 'no such' not in str(exc).lower():
                        raise
                else:
                    db.executescript("""
                        CREATE TRIGGER IF NOT EXISTS frame_add AFTER INSERT ON frames BEGIN
                          INSERT INTO frame_text(rowid,normalized) VALUES(new.rowid,new.normalized);
                        END;
                        CREATE TRIGGER IF NOT EXISTS frame_remove AFTER DELETE ON frames BEGIN
                          INSERT INTO frame_text(frame_text,rowid,normalized) VALUES('delete',old.rowid,old.normalized);
                        END;
                        CREATE TRIGGER IF NOT EXISTS frame_change AFTER UPDATE OF normalized ON frames BEGIN
                          INSERT INTO frame_text(frame_text,rowid,normalized) VALUES('delete',old.rowid,old.normalized);
                          INSERT INTO frame_text(rowid,normalized) VALUES(new.rowid,new.normalized);
                        END;
                    """)
                    if not existing:
                        db.execute("INSERT INTO frame_text(frame_text) VALUES('rebuild')")
                        db.commit()
                    self.fts = True
            self.initialized = True

    def inventory(self, db):
        folder = self.library.folder
        paths = {}
        for p in folder.iterdir():
            if p.suffix.lower() not in VIDEO_EXT | {".html"} or self.library.recorder.is_writing(p):
                continue
            try:
                if p.is_file():
                    st = p.stat()
                    paths[str(p)] = f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
            except FileNotFoundError:
                continue
        old = dict(db.execute("SELECT path,signature FROM assets"))
        for p in old.keys()-paths.keys():
            db.execute("DELETE FROM assets WHERE path=?", (p,))
        for p, sig in sorted(paths.items(),key=lambda pair:int(pair[1].rsplit(":",1)[1])):
            if sig != old.get(p):
                db.execute("DELETE FROM assets WHERE path=?", (p,))
                db.execute("INSERT INTO assets(path,signature) VALUES(?,?)",(p,sig))
        db.commit()

    def renamed(self, old, new):
        with self.lock:
            if not self.db.exists():
                return
            self.initialize()
            parent = self.library.folder
            with closing(self.connect()) as db, db:
                db.execute("UPDATE OR IGNORE assets SET path=? WHERE path=?", (str(parent/new),str(parent/old)))
                db.execute("UPDATE assets SET source=? WHERE source=? AND substr(path,1,?)=?",
                           (new,old,len(str(parent)+os.sep),str(parent)+os.sep))

    def stop(self):
        with self.lock:
            self.stop_event.set()
            self.wake.set()
            if self.decoder is not None and self.decoder.poll() is None:
                self.decoder.terminate()
            thread = self.thread
        if thread:
            thread.join(timeout=4)

    def start_if_needed(self):
        if self.stop_event.is_set():
            return
        if self.thread and self.thread.is_alive():
            if not self.busy:
                self.wake.set()
            return
        self.thread = threading.Thread(target=self.work, daemon=True, name="baird-recording-search")
        self.thread.start()

    def resume(self):
        if self.db.exists():
            with self.lock, closing(self.connect()) as db:
                self.initialize()
                self.inventory(db)
                self.start_if_needed()

    def deleted(self, name):
        with self.lock:
            if self.db.exists():
                self.initialize()
                with closing(self.connect()) as db, db:
                    db.execute("DELETE FROM assets WHERE path=?",(str(self.library.folder/name),))

    def work(self):
        reader = TextReader()
        try:
            with tempfile.TemporaryDirectory() as folder, closing(self.connect()) as db:
                try:
                    while not self.stop_event.is_set():
                        row = db.execute("""SELECT path,signature,tick FROM assets WHERE state!='done' AND retry<=?
                                            ORDER BY (lower(path) LIKE '%.html') DESC, rowid DESC LIMIT 1""",
                                         (time.time(),)).fetchone()
                        if not row:
                            self.busy = ""
                            reader.stop()
                            self.wake.wait(30)
                            self.wake.clear()
                            if not self.stop_event.is_set():
                                with self.lock:
                                    self.inventory(db)
                            continue
                        path, sig, tick = Path(row[0]),row[1],row[2]
                        self.busy = path.name
                        try:
                            if path.suffix.lower() == ".html":
                                report = Report.read(path, self.stop_event)
                                source = report.source
                                if not source or Path(source).name != source or "/" in source or "\\" in source:
                                    source = ""
                                rows = [(sec,i,body,"\n".join(sorted(report.paths[sec])))
                                        for i,(sec,body) in enumerate(report.frames)]
                                self.save(db,path,sig,rows,0,source=source,done=True)
                            else:
                                self.video(db,path,sig,tick,reader,Path(folder))
                        except InterruptedError:
                            if self.stop_event.is_set():
                                return
                        except Exception:
                            db.rollback()
                            with db:
                                db.execute("UPDATE assets SET state='error',retry=? WHERE path=? AND signature=?",
                                           (time.time()+120,str(path),sig))
                finally:
                    reader.stop()
        finally:
            self.busy = ""

    def save(self, db, path, sig, rows, tick, *, source="", done=False):
        with db:
            changed = db.execute("UPDATE assets SET tick=?,source=?,state=?,retry=0 WHERE path=? AND signature=?",
                                 (tick,source,"done" if done else "pending",str(path),sig)).rowcount
            if not changed:
                raise InterruptedError()
            db.executemany("""INSERT INTO frames VALUES (?,?,?,?,?,?)
                              ON CONFLICT(path,sec) DO UPDATE SET ordinal=excluded.ordinal,body=excluded.body,
                              context=excluded.context,normalized=excluded.normalized""",
                           ((str(path),sec,i,body,context,fold(context+" "+body))
                            for sec,i,body,context in rows))

    def video(self, db, path, sig, tick, reader, folder):
        # Sample on the playback clock. Work in short resumable ranges so new reports can take priority.
        count = 40
        start = max(0,tick-1)  # Repeat a committed sample on resume, including after a crash at EOF.
        cmd = [self.library._need_ffmpeg(), "-nostdin","-loglevel","error","-protocol_whitelist","file,pipe",
               "-ss",str(start/2),"-threads","1","-i",str(path),"-map","0:v:0","-an","-sn","-dn",
               "-vf","fps=2:start_time=0:round=up:eof_action=pass,scale=w='min(3000,iw*2)':h='min(3000,ih*2)':force_original_aspect_ratio=decrease",
               "-frames:v",str(count+1),"-threads","1","-c:v","mjpeg","-q:v","3","-f","image2pipe","pipe:1"]
        rows = []
        previous = None
        last = db.execute("SELECT body FROM frames WHERE path=? AND sec<? ORDER BY sec DESC LIMIT 1",
                          (str(path),start/2)).fetchone()
        previous_body = last[0] if last else None
        n = 0
        with subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=NO_WINDOW) as proc:
            bind_to_app(proc)
            with self.lock:
                self.decoder = proc
            try:
                for n, data in enumerate(jpeg_frames(proc),1):
                    if self.stop_event.is_set():
                        raise InterruptedError()
                    if n > count:
                        continue  # Look ahead to establish whether another range exists.
                    if previous != data:
                        with tempfile.NamedTemporaryFile(dir=folder,suffix=".jpg",delete=False) as f:
                            f.write(data)
                            frame = Path(f.name)
                        try:
                            body = "\n".join(line[4] for line in reader.read(frame)["lines"])
                        finally:
                            try:
                                frame.unlink(missing_ok=True)
                            except PermissionError:
                                pass
                        if body != previous_body:
                            context, _ = source_label(body.splitlines())
                            rows.append(((start+n-1)/2,start+n-1,body,context))
                        previous_body = body
                    previous = data
                    if n%4 == 0:
                        self.save(db,path,sig,rows,start+n)
                        rows.clear()
                if not n:
                    raise ValueError("La grabación no contiene fotogramas.")
                self.save(db,path,sig,rows,start+min(n,count),done=n<=count)
            finally:
                with self.lock:
                    self.decoder = None
                if proc.poll() is None:
                    proc.kill()
                proc.stdout.close()

    def search(self, terms, limit, *, recording=None, after=None):
        with self.lock, closing(self.connect()) as db:
            self.initialize()
            self.inventory(db)
            assets = {Path(p).name:(p,source,state) for p,source,state in
                      db.execute("SELECT path,source,state FROM assets")}
            self.start_if_needed()
        def target(name):
            seen = set()
            while name in assets and name not in seen:
                if Path(name).suffix.lower() in VIDEO_EXT:
                    return name
                seen.add(name)
                name = assets[name][1]
            return None
        hits = defaultdict(list)
        counts = {}
        with closing(self.connect()) as db:
            db.execute("CREATE TEMP TABLE destinations(path TEXT PRIMARY KEY, name TEXT, report INTEGER)")
            for name,(path,_,_) in assets.items():
                dest = target(name) or name
                if not recording or dest == recording:
                    db.execute("INSERT INTO destinations VALUES(?,?,?)",(path,dest,int(Path(path).suffix.lower()==".html")))
            condition = " AND ".join("instr(normalized,?)>0" for _ in terms)
            args = list(terms)
            indexed = [t for t in terms if len(t)>=3]
            if self.fts and indexed:
                condition += " AND f.rowid IN (SELECT rowid FROM frame_text WHERE frame_text MATCH ?)"
                args.append(" AND ".join('"'+t.replace('"','""')+'"' for t in indexed))
            if after is not None:
                condition += " AND sec>?"
                args.append(after)
            sql = """WITH candidates AS (
                     SELECT d.name,f.sec,f.rowid AS id,
                            row_number() OVER(PARTITION BY d.name,f.sec ORDER BY d.report DESC,
                                              (f.context!='') DESC,f.rowid DESC) AS preferred
                     FROM frames f JOIN destinations d ON d.path=f.path WHERE """+condition+"""
                   ), ordered AS (
                     SELECT name,id,sec,count(*) OVER(PARTITION BY name) AS total,
                            row_number() OVER(PARTITION BY name ORDER BY sec) AS pos
                     FROM candidates WHERE preferred=1
                   )
                   SELECT o.name,o.sec,f.ordinal,f.body,f.context,o.total FROM ordered o
                   JOIN frames f ON f.rowid=o.id WHERE o.pos<=30 ORDER BY o.name,o.sec"""
            for name,sec,i,body,context,total in db.execute(sql,args):
                hits[name].append({"seconds":sec,"view":i,"snippet":snippet(body,terms),"context":context})
                counts[name] = total
        items = []
        for name in sorted(assets, key=lambda n: (not bool(hits.get(n)),n.casefold())):
            if recording and name != recording:
                continue
            ordered = hits.get(name,[])
            if not ordered and (after is not None or not all(t in fold(name) for t in terms)):
                continue
            try:
                item = self.library.describe(Path(assets[name][0]))
            except FileNotFoundError:
                continue
            total = counts.get(name,0)
            items.append({**item,"moments":ordered,"moment_count":total,
                          "next_after":ordered[-1]["seconds"] if total > len(ordered) else None,
                          "snippet":ordered[0]["snippet"] if ordered else ""})
            if len(items)>=limit:
                break
        return items, {"pending":sum(a[2]=="pending" for a in assets.values()),
                       "failed":sum(a[2]=="error" for a in assets.values()),"current":self.busy}
