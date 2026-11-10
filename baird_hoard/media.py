"""Screenshots, clips, recordings and the files they leave: the media folder is the library.

* A **screenshot** decodes the newest key-frame segment kept in memory to its last complete frame (exact pixels, PNG
  by default): what is on the other screen within a tenth of a second of the click.
* A **clip** writes the key-frame segments of the last N seconds to MP4 (stream copy, about a second).
* A **recording** appends the live stream to a transport-stream file and turns it into MP4 when it stops; a
  recording also starts from the key frame before the click.
* A **light copy** re-encodes a video smaller (720p, ~2.5 Mb/s, the GPU encoder when there is one) for chats with
  upload limits.

Every file can be put on the Windows clipboard (paste it into a chat), shown in Explorer, renamed or sent to the
Recycle Bin.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .capture import CaptureEngine
from .config import Config, Settings
from .errors import BairdError
from .hoard_link.atomic import read_json, write_json_atomic
from .native import NO_WINDOW
from .region import crop_filter, parse_region
from .tsbuf import TsBuffer

log = logging.getLogger("baird.media")

IMAGE_EXT = {".png", ".jpg", ".jpeg"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".ts"}
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H.%M.%S")


class Library:
    def __init__(self, config: Config, settings: Settings, buffer: TsBuffer, engine: CaptureEngine,
                 ffmpeg: Optional[str], ffprobe: Optional[str]):
        self.config = config
        self.settings = settings
        self.buffer = buffer
        self.engine = engine
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] = read_json(config.index_path, default={}) or {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self.recorder = Recorder(self)
        # Hooks the services set: a new screenshot exists / a file was renamed / a file was deleted.
        self.on_new_image: Optional[Any] = None
        self.on_rename: Optional[Any] = None
        self.on_delete: Optional[Any] = None
        config.thumbs_dir.mkdir(parents=True, exist_ok=True)
        config.work_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ folder and names

    @property
    def folder(self) -> Path:
        path = Path(self.settings.media_dir())
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_of(self, name: str) -> Path:
        name = str(name or "").strip()
        if not name or _BAD_NAME.search(name) or name in (".", ".."):
            raise BairdError("invalid", f"Not a file name of the library: {name!r}")
        path = self.folder / name
        if not path.is_file():
            raise BairdError("not_found", f"There is no file called {name!r} in the library.", "media_list shows the names.")
        return path

    def _new_path(self, prefix: str, ext: str, suffix: str = "") -> Path:
        base = f"{prefix} {_stamp()}{suffix}"
        path = self.folder / f"{base}{ext}"
        n = 2
        while path.exists():
            path = self.folder / f"{base} ({n}){ext}"
            n += 1
        return path

    def _need_ffmpeg(self) -> str:
        if not self.ffmpeg:
            raise BairdError("unavailable", "ffmpeg was not found.", "Install it (winget install Gyan.FFmpeg) and restart the app.")
        return self.ffmpeg

    def _run(self, args: list[str], *, data: Optional[bytes] = None, timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(args, input=data, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)

    # ------------------------------------------------------------------ index

    def _remember(self, path: Path, **meta: Any) -> dict[str, Any]:
        with self._lock:
            entry = dict(self._index.get(path.name, {}))
            entry.update({k: v for k, v in meta.items() if v is not None})
            self._index[path.name] = entry
            write_json_atomic(self.config.index_path, self._index)
        return self.describe(path)

    def _probe(self, path: Path) -> dict[str, Any]:
        if not self.ffprobe:
            return {}
        try:
            proc = self._run([self.ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                              "-of", "json", str(path)], timeout=20)
            info = json.loads(proc.stdout or b"{}")
        except (subprocess.SubprocessError, ValueError, OSError):
            return {}
        out: dict[str, Any] = {}
        for st in info.get("streams", []):
            if st.get("codec_type") == "video" and st.get("width"):
                out["width"], out["height"] = st["width"], st["height"]
            if st.get("codec_type") == "audio":
                out["audio"] = True
        dur = (info.get("format") or {}).get("duration")
        if dur:
            try:
                out["duration"] = round(float(dur), 2)
            except ValueError:
                pass
        return out

    def describe(self, path: Path) -> dict[str, Any]:
        st = path.stat()
        ext = path.suffix.lower()
        kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else "file"
        with self._lock:
            entry = dict(self._index.get(path.name, {}))
        if kind == "video" and "duration" not in entry and not entry.get("probed"):
            entry.update(self._probe(path))
            entry["probed"] = True
            with self._lock:
                self._index[path.name] = entry
        return {"name": path.name, "kind": kind, "path": str(path), "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "width": entry.get("width"), "height": entry.get("height"), "duration": entry.get("duration"),
                "origin": entry.get("origin", "file"), "audio": entry.get("audio"),
                "url": f"/media/{path.name}", "thumb": f"/api/thumb/{path.name}?v={int(st.st_mtime)}"}

    def list(self, kind: str = "all", limit: int = 200) -> list[dict[str, Any]]:
        folder = self.folder
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT | VIDEO_EXT]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files:
            if self.recorder.is_writing(p):
                continue
            d = self.describe(p)
            if kind != "all" and d["kind"] != kind:
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ thumbnails

    def thumbnail(self, name: str) -> Path:
        path = self.path_of(name)
        st = path.stat()
        key = hashlib.sha1(f"{path.name}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
        thumb = self.config.thumbs_dir / f"{key}.jpg"
        if thumb.is_file():
            return thumb
        ff = self._need_ffmpeg()
        args = [ff, "-hide_banner", "-loglevel", "error", "-y"]
        if path.suffix.lower() in VIDEO_EXT:
            dur = self.describe(path).get("duration") or 0
            args += ["-ss", f"{min(1.0, max(0.0, dur / 2)):.2f}"]
        args += ["-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(thumb)]
        proc = self._run(args, timeout=30)
        if proc.returncode != 0 or not thumb.is_file():
            raise BairdError("failed", "Could not make a thumbnail.", proc.stderr.decode("utf-8", "replace")[-300:])
        return thumb

    # ------------------------------------------------------------------ capture actions

    def input_size(self) -> Optional[tuple[int, int]]:
        inp = self.engine.snapshot().get("input") or {}
        if inp.get("width") and inp.get("height"):
            return int(inp["width"]), int(inp["height"])
        return None

    def snapshot(self, fmt: Optional[str] = None, region: Optional[dict[str, Any]] = None, *,
                 prefix: Optional[str] = None, origin: Optional[str] = None) -> dict[str, Any]:
        """Save the current picture (or only ``region`` of it); returns its description."""
        self.engine.require_picture()
        ff = self._need_ffmpeg()
        fmt = (fmt or self.settings["snapshot_format"]).lower()
        if fmt not in ("png", "jpg"):
            raise BairdError("invalid", "fmt must be png or jpg")
        region = parse_region(region, self.input_size())
        data, frames = self.buffer.latest()
        if not data:
            raise BairdError("no_signal", "No picture has arrived yet.", "Wait a second and try again.")
        out = self._new_path(prefix or ("Captura zona" if region else "Captura"), "." + fmt)
        enc = ["-c:v", "png"] if fmt == "png" else ["-q:v", "2"]
        last_err = ""
        for index in (max(0, frames - 2), 0):
            vf = f"select=eq(n\\,{index})" + ("," + crop_filter(region) if region else "")
            args = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", "pipe:0", "-an",
                    "-vf", vf, "-fps_mode", "passthrough", "-frames:v", "1", *enc, str(out)]
            proc = self._run(args, data=data, timeout=20)
            if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
                inp = self.engine.snapshot().get("input") or {}
                size = self._probe(out) if region else {"width": inp.get("width"), "height": inp.get("height")}
                desc = self._remember(out, origin=origin or ("zone" if region else "snapshot"), width=size.get("width"),
                                      height=size.get("height"), taken=time.time(), region=region)
                self._fire("on_new_image", out)
                return desc
            out.unlink(missing_ok=True)
            last_err = proc.stderr.decode("utf-8", "replace")[-300:]
        raise BairdError("failed", "Could not decode the picture.", last_err)

    def grab_gray(self, region: Optional[dict[str, Any]] = None, size: tuple[int, int] = (96, 54)) -> bytes:
        """The newest picture (or ``region`` of it) shrunk to ``size`` in 8-bit grey: what change detection compares."""
        ff = self._need_ffmpeg()
        data, frames = self.buffer.latest()
        if not data:
            raise BairdError("no_signal", "No picture has arrived yet.")
        region = parse_region(region, self.input_size())
        want = size[0] * size[1]
        for index in (max(0, frames - 2), 0):
            vf = f"select=eq(n\\,{index})," + (crop_filter(region) + "," if region else "") + \
                 f"scale={size[0]}:{size[1]}:flags=area,format=gray"
            proc = self._run([ff, "-hide_banner", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0", "-an", "-vf", vf,
                              "-fps_mode", "passthrough", "-frames:v", "1", "-f", "rawvideo", "pipe:1"], data=data, timeout=20)
            if proc.returncode == 0 and len(proc.stdout) == want:
                return proc.stdout
        raise BairdError("failed", "Could not decode the picture.")

    def grab_file(self, region: Optional[dict[str, Any]], dest: Path) -> Path:
        """The newest picture (or ``region``) as a PNG at ``dest``, outside the library (OCR input)."""
        ff = self._need_ffmpeg()
        data, frames = self.buffer.latest()
        if not data:
            raise BairdError("no_signal", "No picture has arrived yet.")
        region = parse_region(region, self.input_size())
        for index in (max(0, frames - 2), 0):
            vf = f"select=eq(n\\,{index})" + ("," + crop_filter(region) if region else "")
            proc = self._run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", "pipe:0", "-an", "-vf", vf,
                              "-fps_mode", "passthrough", "-frames:v", "1", "-c:v", "png", str(dest)], data=data, timeout=20)
            if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
                return dest
        raise BairdError("failed", "Could not decode the picture.")

    def save_annotated(self, name: str, png: bytes) -> dict[str, Any]:
        """Keep an annotated copy of the picture ``name`` as a new file; the original is not touched. ``png`` is the
        image the browser drew (the picture with arrows, boxes and text)."""
        src = self.path_of(name)
        if src.suffix.lower() not in IMAGE_EXT:
            raise BairdError("invalid", "Only screenshots can be annotated.")
        if len(png) < 64 or png[:8] != b"\x89PNG\r\n\x1a\n":
            raise BairdError("invalid", "The annotated picture must be a PNG.")
        if len(png) > 80_000_000:
            raise BairdError("invalid", "The annotated picture is too large.")
        out = src.with_name(f"{src.stem} (anotada).png")
        n = 2
        while out.exists():
            out = src.with_name(f"{src.stem} (anotada {n}).png")
            n += 1
        out.write_bytes(png)
        width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        desc = self._remember(out, origin="annotated", source=src.name, width=width, height=height)
        self._fire("on_new_image", out)
        return desc

    def _fire(self, hook: str, *args: Any) -> None:
        fn = getattr(self, hook, None)
        if fn is not None:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - a hook must never break a capture
                log.debug("%s failed", hook, exc_info=True)

    def clip(self, seconds: Optional[float] = None) -> dict[str, Any]:
        """Save the last ``seconds`` (default: the clip setting) as MP4."""
        self.engine.require_picture()
        ff = self._need_ffmpeg()
        secs = float(seconds or self.settings["clip_seconds"])
        kept = self.buffer.seconds_kept()
        if secs > self.settings["buffer_seconds"]:
            raise BairdError("invalid", f"At most {self.settings['buffer_seconds']} s are kept in memory.",
                             "Raise buffer_seconds in the settings for longer clips, or record.")
        parts = self.buffer.since(secs)
        if not parts:
            raise BairdError("no_signal", "Nothing is in memory yet.")
        tmp = self.config.work_dir / f"clip-{uuid.uuid4().hex[:8]}.ts"
        tmp.write_bytes(b"".join(parts))
        out = self._new_path("Clip", ".mp4", f" ({int(round(min(secs, kept)))} s)")
        try:
            proc = self._run([ff, "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-i", str(tmp), "-map", "0:v:0",
                              "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)], timeout=120)
            if proc.returncode != 0 or not out.is_file():
                raise BairdError("failed", "Could not write the clip.", proc.stderr.decode("utf-8", "replace")[-300:])
        finally:
            tmp.unlink(missing_ok=True)
        return self._remember(out, origin="clip", **self._probe(out), probed=True)

    def light_copy(self, name: str) -> dict[str, Any]:
        """Start a smaller copy of a video for chats (background job); returns the job."""
        src = self.path_of(name)
        if src.suffix.lower() not in VIDEO_EXT:
            raise BairdError("invalid", "Only videos have a light copy.")
        ff = self._need_ffmpeg()
        h, k = self.settings["light_height"], self.settings["light_kbps"]
        out = src.with_name(f"{src.stem} (ligero).mp4")
        n = 2
        while out.exists():
            out = src.with_name(f"{src.stem} (ligero {n}).mp4")
            n += 1
        job = {"id": uuid.uuid4().hex[:10], "kind": "light_copy", "source": src.name, "target": out.name, "state": "running",
               "started": time.time(), "message": ""}
        with self._lock:
            self._jobs[job["id"]] = job

        def work() -> None:
            common_in = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", f"scale=-2:{h}"]
            tail = ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
            tries = [["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", f"{k}k", "-maxrate", f"{int(k * 1.5)}k", "-bufsize", f"{k * 2}k"],
                     ["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{k}k", "-maxrate", f"{int(k * 1.5)}k", "-bufsize", f"{k * 2}k"]]
            err = ""
            for codec in tries:
                proc = self._run(common_in + codec + tail, timeout=3600)
                if proc.returncode == 0 and out.is_file():
                    self._remember(out, origin="light", **self._probe(out), probed=True)
                    job.update(state="done", finished=time.time(), encoder=codec[1])
                    return
                err = proc.stderr.decode("utf-8", "replace")[-300:]
                out.unlink(missing_ok=True)
            job.update(state="failed", finished=time.time(), message=err)

        threading.Thread(target=work, name="baird-light", daemon=True).start()
        return dict(job)

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["started"], reverse=True)
            return [dict(j) for j in jobs[:20]]

    # ------------------------------------------------------------------ file actions

    def delete(self, name: str) -> dict[str, Any]:
        path = self.path_of(name)
        to_bin = send_to_recycle_bin(path)
        if not to_bin and path.exists():
            path.unlink()
        with self._lock:
            self._index.pop(path.name, None)
            write_json_atomic(self.config.index_path, self._index)
        self._fire("on_delete", path.name)
        return {"deleted": path.name, "recycle_bin": to_bin}

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        path = self.path_of(name)
        new_name = str(new_name or "").strip()
        if not new_name.lower().endswith(path.suffix.lower()):
            new_name += path.suffix
        if _BAD_NAME.search(new_name) or new_name.startswith("."):
            raise BairdError("invalid", "The new name has characters Windows does not allow.")
        target = path.with_name(new_name)
        if target.exists():
            raise BairdError("conflict", f"There is already a file called {new_name!r}.")
        path.rename(target)
        with self._lock:
            entry = self._index.pop(path.name, None)
            if entry is not None:
                self._index[target.name] = entry
            write_json_atomic(self.config.index_path, self._index)
        self._fire("on_rename", path.name, target.name)
        return self.describe(target)

    def copy_to_clipboard(self, name: str) -> dict[str, Any]:
        """Put the file on the Windows clipboard (as a file: Ctrl+V pastes it into a chat, a mail or Explorer)."""
        path = self.path_of(name)
        if os.name != "nt":
            raise BairdError("unavailable", "The clipboard is only handled on Windows.")
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$c = New-Object System.Collections.Specialized.StringCollection; [void]$c.Add($env:BAIRD_CLIP_PATH);"
                  "[System.Windows.Forms.Clipboard]::SetFileDropList($c)")
        env = dict(os.environ, BAIRD_CLIP_PATH=str(path))
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script], env=env,
                              capture_output=True, timeout=20, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise BairdError("failed", "Could not use the clipboard.", proc.stderr.decode("utf-8", "replace")[-300:])
        return {"copied": path.name, "path": str(path)}

    def reveal(self, name: Optional[str] = None) -> dict[str, Any]:
        """Open the media folder in Explorer, with ``name`` selected."""
        if os.name != "nt":
            raise BairdError("unavailable", "Explorer is Windows only.")
        if name:
            path = self.path_of(name)
            subprocess.Popen(["explorer", f"/select,{path}"], creationflags=NO_WINDOW)
            return {"shown": str(path)}
        subprocess.Popen(["explorer", str(self.folder)], creationflags=NO_WINDOW)
        return {"shown": str(self.folder)}

    def image_for_model(self, path: Path, max_side: int = 1568) -> dict[str, str]:
        """A JPEG of ``path`` no larger than ``max_side`` (what an assistant sees), base64."""
        import base64
        ff = self._need_ffmpeg()
        proc = self._run([ff, "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-vf",
                          f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
                          "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"], timeout=20)
        if proc.returncode != 0 or not proc.stdout:
            raise BairdError("failed", "Could not prepare the picture.", proc.stderr.decode("utf-8", "replace")[-300:])
        return {"data": base64.b64encode(proc.stdout).decode("ascii"), "mime": "image/jpeg"}


class Recorder:
    """Appends the live transport stream to ``<data>/work/rec-*.ts`` and turns it into MP4 in the library on stop."""

    def __init__(self, lib: Library):
        self.lib = lib
        self._lock = threading.RLock()
        self._file = None
        self._tmp: Optional[Path] = None
        self._target: Optional[Path] = None
        self._started = 0.0
        self._from = 0
        self._bytes = 0
        self.last: Optional[dict[str, Any]] = None
        self.state = "idle"            # idle | recording | finishing
        self.message = ""
        lib.engine.add_listener(self._on_data)

    def is_writing(self, path: Path) -> bool:
        return self._target is not None and path == self._target

    def status(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {"state": self.state, "message": self.message, "last": self.last}
            if self.state == "recording":
                out.update(seconds=round(time.time() - self._started, 1), bytes=self._bytes,
                           target=self._target.name if self._target else None)
            return out

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.state == "recording":
                raise BairdError("busy", "Already recording.", "record_stop ends it.")
            if self.state == "finishing":
                raise BairdError("busy", "The previous recording is still being written; try again in a moment.")
            self.lib.engine.require_picture()
            self.lib._need_ffmpeg()
            head, self._from = self.lib.buffer.tail()
            self._tmp = self.lib.config.work_dir / f"rec-{uuid.uuid4().hex[:8]}.ts"
            self._target = self.lib._new_path("Grabación", ".mp4")
            self._file = open(self._tmp, "wb")
            self._file.write(head)
            self._bytes = len(head)
            self._started = time.time()
            self.state, self.message = "recording", ""
            return self.status()

    def _on_data(self, aligned: bytes, starts: list[int], end_total: int) -> None:
        with self._lock:
            if self.state != "recording" or self._file is None or end_total <= self._from:
                return
            try:
                self._file.write(aligned)
                self._bytes += len(aligned)
            except OSError as exc:
                self.message = f"Writing failed: {exc}"
                self._finish_locked()

    def on_capture_state(self, state: str) -> None:
        """The picture went away while recording: keep what was recorded."""
        with self._lock:
            if self.state == "recording" and state not in ("running", "starting"):
                self.message = "The recording stopped because the picture was lost."
                self._finish_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self.state != "recording":
                raise BairdError("conflict", "Nothing is being recorded.")
            self._finish_locked()
            return self.status()

    def _finish_locked(self) -> None:
        f, tmp, target = self._file, self._tmp, self._target
        self._file = None
        if f:
            f.close()
        self.state = "finishing"
        seconds = round(time.time() - self._started, 1)

        def work() -> None:
            ff = self.lib.ffmpeg or "ffmpeg"
            result: dict[str, Any]
            try:
                proc = self.lib._run([ff, "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-i", str(tmp),
                                      "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(target)],
                                     timeout=3600)
                if proc.returncode == 0 and target is not None and target.is_file():
                    with self._lock:
                        self._target = None
                    result = self.lib._remember(target, origin="recording", **self.lib._probe(target), probed=True)
                    if tmp:
                        tmp.unlink(missing_ok=True)
                else:
                    # Keep the transport stream: nothing recorded is lost.
                    keep = target.with_suffix(".ts") if target else None
                    if tmp and keep:
                        os.replace(tmp, keep)
                    result = {"name": keep.name if keep else "", "error": proc.stderr.decode("utf-8", "replace")[-300:]}
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)[:300]}
            with self._lock:
                self._target = None
                self.last = {**result, "seconds": seconds}
                self.state = "idle"

        threading.Thread(target=work, name="baird-record-finish", daemon=True).start()


def send_to_recycle_bin(path: Path) -> bool:
    """Move ``path`` to the Recycle Bin (Windows). False when it could not (the caller deletes it then)."""
    if os.name != "nt":
        return False

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None, FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                         0, None, None)
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    return rc == 0 and not path.exists()
