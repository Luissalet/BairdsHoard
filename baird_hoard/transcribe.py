"""Transcribe a recording as it grows. Frames queue on disk so a slow OCR never drops part of a scroll."""
from __future__ import annotations

import base64
import difflib
import html
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .detail_job import DetailJob
from .capture_feedback import CaptureFeedback
from .errors import BairdError
from .media import Library, jpeg_size
from .native import NO_WINDOW
from .region import crop_filter, parse_region
from .textmap import TextReader

log = logging.getLogger("baird.transcribe")
FPS = 2

HEADER = """<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transcripción de pantalla</title>
<style>body{font:17px/1.5 system-ui,sans-serif;max-width:62rem;margin:auto;padding:2rem}
h1{font-size:1.6rem}h2{font:600 1rem system-ui;color:#485566}section{border-top:1px solid #ccc;margin-top:2rem}
img{max-width:100%;height:auto}pre{font:0.93rem/1.5 ui-monospace,monospace;white-space:pre-wrap}details{margin:1rem 0}
@media print{section{break-before:page}details{break-inside:avoid}}</style>
<h1>Transcripción de pantalla</h1>
"""


def new_lines(previous: list[str], current: list[str]) -> list[str]:
    """Keep new text in reading order. Match runs, so a repeated word elsewhere still makes it into the document."""
    def keys(lines):
        return [s.rstrip() for s in lines]

    matches = difflib.SequenceMatcher(None, keys(previous), keys(current), autojunk=False)
    covered = set()
    for a, b, length in matches.get_matching_blocks():
        if length >= 2 or (length == 1 and (a == b or (a + 1 == len(previous) and b == 0))):
            covered.update(range(b, b + length))
    return [line for i, line in enumerate(current) if i not in covered]


def extract_frames(ffmpeg: str, source: Path, finished: threading.Event, folder: Path, region: dict | None):
    """Feed a growing transport stream until the recorder closes it. Atomic JPEG names are half-second timestamps."""
    filters = [crop_filter(region)] if region else []
    filters += [f"fps={FPS}", "mpdecimate=hi=4096:lo=128:frac=0.003"]
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "128k", "-analyzeduration", "300000", "-threads", "1", "-f", "mpegts", "-i", "pipe:0", "-an",
            "-vf", ",".join(filters), "-fps_mode", "passthrough", "-enc_time_base", f"1:{FPS}",
            "-c:v", "mjpeg", "-threads", "1", "-q:v", "2", "-frame_pts", "1", "-atomic_writing", "1", str(folder / "%012d.jpg")]
    with (folder / "decoder.log").open("w+b") as errors, subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors, creationflags=NO_WINDOW
    ) as proc:
        cancel = threading.Event()
        feed_errors = []

        def feed():
            try:
                with source.open("rb") as stream, proc.stdin as pipe:
                    while not cancel.is_set():
                        # Check before reading: once the event is set, EOF is definitive.
                        closed = finished.is_set()
                        data = stream.read(262144)
                        if data:
                            pipe.write(data)
                        elif closed:
                            break
                        else:
                            finished.wait(0.08)
            except (OSError, ValueError) as exc:
                feed_errors.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=feed, name="baird-document-input", daemon=True)
        feeder.start()
        try:
            while True:
                ended = proc.poll() is not None
                for frame in sorted(folder.glob("*.jpg")):
                    yield frame
                    frame.unlink()
                if ended:
                    break
                time.sleep(0.1)
            feeder.join()
            if proc.returncode or feed_errors:
                errors.seek(0)
                message = errors.read()[-500:].decode("utf-8", "replace")
                raise RuntimeError(message or str(feed_errors))
        finally:
            cancel.set()
            if proc.poll() is None:
                proc.kill()
            feeder.join()


class Transcriber:
    def __init__(self, library: Library):
        self.library = library
        self.detail = DetailJob(library)
        self._lock = threading.RLock()
        self._report = {"state": "idle", "views": 0}
        self._finished: threading.Event | None = None

    def status(self):
        with self._lock:
            result = self._report.copy()
            if result["state"] == "reading" and self._finished and self._finished.is_set():
                result["state"] = "finishing"
            result["detail"] = self.detail.status()
            return result

    def start(self, region=None, detail="auto"):
        status = self.detail.status()
        if detail == "auto" and (not status["available"] or status["state"] == "running"):
            detail = "off"
        if detail != "off" and not status["available"]:
            raise BairdError("unavailable", "UnlimitedOCR local no está configurado.")
        if detail != "off" and status["state"] == "running":
            raise BairdError("busy", "Hay una segunda lectura en curso.")
        region = parse_region(region, self.library.input_size())
        with self._lock:
            if self._report["state"] in ("starting", "reading"):
                raise BairdError("busy", "Ya hay una transcripción en curso.")
            previous = self._report
            self._report = {"state": "starting", "views": 0}
            self._finished = None
        try:
            def run(source, video, finished):
                self._run(source, video, finished, region, detail)

            started = self.library.recorder.start(read_stream=run)
        except Exception:
            with self._lock:
                self._report = previous
            raise
        with self._lock:
            self._report.setdefault("video", started["target"])
            return self.status()

    def stop(self):
        with self._lock:
            video = self._report.get("video")
            if not video or self.status()["state"] not in ("reading", "starting"):
                raise BairdError("conflict", "No hay una transcripción en curso.")
        self.library.recorder.stop(target=video)
        return self.status()

    def _run(self, source, video, finished, region, detail="off"):
        with self._lock:
            self._finished = finished
            self._report.update(state="reading", video=video.name)
        reader = TextReader()
        feedback = CaptureFeedback()
        views = unread = 0
        error = ""
        path = video.with_name(video.stem.replace("Grabación", "Transcripción", 1) + ".html")
        if path.exists():
            path = self.library._new_path("Transcripción", ".html")
        partial = None
        try:
            previous = []
            lang = self.library.settings["copilot"]["ocr_lang"]
            with tempfile.TemporaryDirectory(dir=self.library.config.work_dir) as tmp, tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=path.stem + "-", suffix=".html.part", delete=False
            ) as out:
                partial = Path(out.name)
                transcript = []
                out.write(HEADER + '<p data-baird-video="' + html.escape(video.name, quote=True) + '">' +
                          html.escape(video.stem) + '</p><p><a href="#texto">Ir a la descarga de texto</a></p>\n')
                frames = extract_frames(self.library.ffmpeg, source, finished, Path(tmp), region)
                try:
                    for frame in frames:
                        elapsed = int(frame.stem) / FPS
                        warning = ""
                        original = frame.read_bytes()
                        gray = b""
                        mapped = []
                        enlarged = Path(tmp) / (frame.stem + "-ocr.png")
                        try:
                            ocr_input = frame
                            longest = max(jpeg_size(original))
                            cmd = [self.library.ffmpeg, "-loglevel", "error", "-y", "-i", str(frame)]
                            if 0 < longest <= 2000:
                                cmd += ["-map", "0:v", "-vf", "scale=iw*2:ih*2:flags=lanczos", "-threads", "1", str(enlarged)]
                            resized = self.library._run(cmd + ["-map", "0:v", "-vf", "scale=320:180", "-pix_fmt", "gray",
                                                               "-threads", "1", "-f", "rawvideo", "pipe:1"])
                            if resized.returncode == 0:
                                gray = resized.stdout
                                if enlarged.is_file():
                                    ocr_input = enlarged
                            mapped = reader.read(ocr_input, lang)["lines"]
                            lines = [line[4] for line in mapped]
                        except BairdError as exc:
                            unread += 1
                            warning = "OCR no disponible para esta vista."
                            error = str(exc)
                            lines = []
                        finally:
                            try:
                                enlarged.unlink(missing_ok=True)
                            except PermissionError:
                                pass
                        alert = feedback.observe(mapped, elapsed, gray, failed=bool(warning))
                        if not finished.is_set():
                            newest = max((int(p.stem) for p in Path(tmp).glob("*.jpg") if p.stem.isdecimal()),
                                         default=int(frame.stem))
                            delay = (newest-int(frame.stem))/FPS
                            if delay >= 4:
                                alert = {"kind":"lag", "seconds":elapsed,
                                         "text":f"El OCR lleva {int(delay)} s de retraso. Pausa para que alcance la vista actual."}
                        added = new_lines(previous, lines)
                        previous = lines
                        transcript.extend(added)
                        image = base64.b64encode(original).decode("ascii")
                        label = f"{int(elapsed // 60):02}:{elapsed % 60:04.1f}"
                        text = html.escape("\n".join(added))
                        out.write(f'<section><h2>{label}</h2><pre>{text}</pre>{warning}'
                                  f'<details><summary>Texto completo de esta vista · OCR de Windows</summary><pre>{html.escape(chr(10).join(lines))}</pre></details>'
                                  f'<details open><summary>Vista del documento</summary>'
                                  f'<img loading="lazy" alt="Pantalla en {label}" src="data:image/jpeg;base64,{image}">'
                                  '</details></section>\n')
                        out.flush()
                        views += 1
                        with self._lock:
                            self._report.update(views=views, preview="\n".join(added)[-250:] or self._report.get("preview", ""),
                                                unread=unread, feedback=alert)
                finally:
                    frames.close()
                    reader.stop()
                    plain = base64.b64encode("\n".join(transcript).encode("utf-8")).decode("ascii")
                    out.write(f'<p id="texto"><a download="transcripcion.txt" href="data:text/plain;charset=utf-8;base64,{plain}">'
                              'Descargar solo el texto (.txt)</a></p></html>')
        except Exception as exc:
            log.exception("Transcribing the recording")
            error = str(exc)
            if not finished.is_set():
                try:
                    self.library.recorder.stop(target=video.name)
                except BairdError:
                    pass  # The recording already stopped.
        finally:
            reader.stop()
        try:
            if partial is not None:
                if views:
                    partial.rename(path)
                else:
                    partial.unlink(missing_ok=True)
        except OSError as exc:
            error = str(exc)
        with self._lock:
            document = str(path) if views and partial and not partial.exists() and path.is_file() else ""
            self._report.update(state="partial" if error and document else "done" if document else "failed",
                                views=views, path=document, name=path.name if document else "",
                                error=error or ("" if document else "No se pudo leer ninguna vista."))
            if document and detail != "off":
                try:
                    self.detail.start(path, detail)
                except BairdError as exc:
                    self._report["detail_error"] = str(exc)
