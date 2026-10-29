"""The live view: the device's H.264/AAC is copied (never re-encoded) from the transport stream into fragmented MP4
by ffmpeg and pushed to the browsers over a WebSocket, where Media Source Extensions play it with the GPU decoder.

``empty_moov`` is deliberately not used: it writes the moov before the first AAC packet, so the audio track lacks its
AudioSpecificConfig and Chrome's MSE refuses the stream; without it ffmpeg writes the moov with the first fragment.

One remuxer serves every viewer and runs only while someone watches. A viewer gets ``{"type":"init","mime":...}``,
then the MP4 init segment (ftyp+moov) as a binary message, then every fragment (moof+mdat). The browser drops
frames until the first key frame (the MSE rules), and the remuxer is primed with the newest key-frame segment, so the
picture appears at once.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import subprocess
import threading
import time
from typing import Any, Optional

from .capture import CaptureEngine
from .native import NO_WINDOW

log = logging.getLogger("baird.live")

MAX_QUEUE = 120          # fragments a slow viewer may lag behind before it is dropped


class _Viewer:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self.dropped = False

    def push(self, item: Any) -> None:
        def put() -> None:
            if self.queue.qsize() > MAX_QUEUE:
                self.dropped = True
                self.queue.put_nowait(None)
                return
            self.queue.put_nowait(item)
        try:
            self.loop.call_soon_threadsafe(put)
        except RuntimeError:          # loop closed
            self.dropped = True


class LiveRemux:
    def __init__(self, engine: CaptureEngine, ffmpeg: Optional[str]):
        self.engine = engine
        self.ffmpeg = ffmpeg
        self._lock = threading.RLock()
        self._viewers: list[_Viewer] = []
        self._proc: Optional[subprocess.Popen] = None
        self._init: bytes = b""
        self._mime: str = ""
        self._generation = 0
        self._idle_timer: Optional[threading.Timer] = None
        self._primed = False
        self._primed_at = 0
        self._fails = 0
        self._started_at = 0.0
        engine.add_listener(self._on_data)

    # ------------------------------------------------------------------ viewers

    def attach(self, loop: asyncio.AbstractEventLoop) -> _Viewer:
        viewer = _Viewer(loop)
        with self._lock:
            self._viewers.append(viewer)
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            elif self._init:
                viewer.push({"type": "init", "mime": self._mime})
                viewer.push(self._init)
        return viewer

    def detach(self, viewer: _Viewer) -> None:
        with self._lock:
            if viewer in self._viewers:
                self._viewers.remove(viewer)
            if not self._viewers and self._idle_timer is None:
                self._idle_timer = threading.Timer(15.0, self._stop_if_idle)
                self._idle_timer.daemon = True
                self._idle_timer.start()

    @property
    def viewers(self) -> int:
        with self._lock:
            return len(self._viewers)

    def _broadcast(self, item: Any) -> None:
        with self._lock:
            viewers = list(self._viewers)
        for v in viewers:
            v.push(item)

    # ------------------------------------------------------------------ remuxer

    def _start(self) -> None:
        if not self.ffmpeg:
            self._broadcast({"type": "error", "message": "ffmpeg was not found; the live view needs it."})
            return
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+nobuffer+discardcorrupt", "-flags", "low_delay",
               "-probesize", "200000", "-analyzeduration", "500000", "-f", "mpegts", "-i", "pipe:0",
               "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
               "-f", "mp4", "-movflags", "frag_keyframe+default_base_moof", "-frag_duration", "100000", "pipe:1"]
        self._generation += 1
        self._started_at = time.time()
        gen = self._generation
        self._init = b""
        self._primed = False
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                      creationflags=NO_WINDOW)
        threading.Thread(target=self._read_boxes, args=(self._proc, gen), name="baird-live-out", daemon=True).start()
        threading.Thread(target=self._drain_err, args=(self._proc,), name="baird-live-err", daemon=True).start()
        # Prime with the newest key-frame segment so the first picture comes at once.
        head, self._primed_at = self.engine.buffer.tail()
        if head:
            self._write(head)
            self._primed = True
        log.info("live remuxer started (%d viewers)", len(self._viewers))

    def _stop_if_idle(self) -> None:
        with self._lock:
            self._idle_timer = None
            if self._viewers:
                return
            self._stop_proc()

    def _stop_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.info("live remuxer stopped")

    def restart(self) -> None:
        """The source changed (new capture session): viewers get a reset and a new init segment."""
        with self._lock:
            self._stop_proc()
            if self._viewers:
                self._broadcast({"type": "reset"})
                self._start()

    def stop(self) -> None:
        with self._lock:
            if self._idle_timer:
                self._idle_timer.cancel()
            self._stop_proc()
            self._broadcast(None)

    def _write(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(data)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _on_data(self, aligned: bytes, starts: list[int], end_total: int) -> None:
        with self._lock:
            if self._proc is None or end_total <= self._primed_at:
                return
            if not self._primed:
                if not starts:
                    return
                self._write(self.engine.buffer.header + aligned[starts[0]:])
                self._primed = True
                return
        self._write(aligned)

    def _drain_err(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                log.debug("live ffmpeg: %s", line[:300])

    def _read_boxes(self, proc: subprocess.Popen, gen: int) -> None:
        out = proc.stdout
        if out is None:
            return
        buf = b""
        init_parts: list[bytes] = []
        frag: list[bytes] = []

        def read_exact(n: int) -> Optional[bytes]:
            nonlocal buf
            while len(buf) < n:
                chunk = out.read(65536) if not hasattr(out, "read1") else out.read1(65536)  # type: ignore[union-attr]
                if not chunk:
                    return None
                buf += chunk
            piece, buf = buf[:n], buf[n:]
            return piece

        while True:
            head = read_exact(8)
            if head is None:
                break
            size, kind = struct.unpack(">I4s", head)
            if size == 1:
                ext = read_exact(8)
                if ext is None:
                    break
                size = struct.unpack(">Q", ext)[0]
                body = read_exact(size - 16)
                box = head + ext + (body or b"")
            else:
                body = read_exact(max(0, size - 8))
                if body is None:
                    break
                box = head + body
            name = kind.decode("latin-1")
            if gen != self._generation:
                break
            if name in ("ftyp", "moov"):
                init_parts.append(box)
                if name == "moov":
                    with self._lock:
                        self._init = b"".join(init_parts)
                        self._mime = 'video/mp4; codecs="%s"' % self.engine.buffer.codec_string()
                    self._broadcast({"type": "init", "mime": self._mime})
                    self._broadcast(self._init)
            elif name == "moof":
                frag = [box]
            elif name == "mdat":
                frag.append(box)
                self._broadcast(b"".join(frag))
                frag = []
        log.debug("live remuxer output ended (gen %d)", gen)
        with self._lock:
            if gen == self._generation and self._viewers and self.engine.snapshot()["state"] == "running":
                # ffmpeg died while people watch (corrupt input after a replug): start it again, but give up after
                # repeated quick failures instead of spinning.
                quick = time.time() - self._started_at < 5
                self._fails = self._fails + 1 if quick else 0
                self._proc = None
                if self._fails >= 4:
                    log.error("live remuxer keeps failing; live view stopped")
                    self._broadcast({"type": "error", "message": "The live view failed repeatedly; reload the page."})
                    return
                time.sleep(0.5 * (1 + self._fails))
                self._broadcast({"type": "reset"})
                self._start()
