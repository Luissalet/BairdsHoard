"""Run the desktop and WASAPI producer, with one encoder per MPEG-TS session."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from .errors import BairdError
from .monitor_source import DesktopMonitor, RESELECT, normalize_selection, selected_monitor, windows_inventory
from .native import NATIVE_DIR, NO_WINDOW, bind_to_app, csc_path
from .tsbuf import TsBuffer

FPS = 30
SOFTWARE = "libx264"
HARDWARE = {0x10DE: "h264_nvenc", 0x1002: "h264_amf", 0x8086: "h264_qsv"}
LAYOUT_POLL_S = 0.75
START_TIMEOUT_S = 9.0


class EncoderStartError(RuntimeError):
    """A producer failed before the first usable frame was exposed."""


def emit(kind: str, **data):
    sys.stderr.write(json.dumps({"event": kind, **data}, ensure_ascii=True) + "\n")
    sys.stderr.flush()


def encoder_order(monitor: DesktopMonitor, available: set[str]) -> list[str]:
    hardware = HARDWARE.get(monitor.vendor)
    return [hardware, SOFTWARE] if hardware in available else [SOFTWARE]


def available_encoders(ffmpeg: str) -> set[str]:
    # Listing compiled encoders does not initialize a GPU.
    res = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                         creationflags=NO_WINDOW, timeout=4, check=True)
    return {cols[1] for line in res.stdout.splitlines() if len(cols := line.split()) >= 2 and cols[0].startswith("V")}


def video_graph(monitor: DesktopMonitor, encoder: str) -> str:
    source = f"ddagrab=output_idx={monitor.output}:framerate={FPS}:dup_frames=1"
    rotations = {2: "transpose=clock", 3: "hflip,vflip", 4: "transpose=cclock"}
    rotate = rotations.get(monitor.rotation)
    convert = encoder in (SOFTWARE, "h264_qsv") or rotate is not None
    filters = [source, "setpts=PTS-STARTPTS"]
    if convert:
        filters.extend(("hwdownload", "format=bgra"))
        if rotate:
            filters.append(rotate)
        filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
        if encoder == SOFTWARE:
            filters.append("format=yuv420p")
        elif encoder == "h264_qsv":
            filters.extend(("format=nv12", "hwupload=extra_hw_frames=16:derive_device=qsv"))
        else:
            filters.extend(("format=bgra", "hwupload=extra_hw_frames=16"))
    return ",".join(filters) + "[desktop]"


def ffmpeg_command(ffmpeg: str, monitor: DesktopMonitor, encoder: str, microphone: bool, *,
                   video: str | None = None, device: bool = True) -> list[str]:
    """Build the common PCM/TS pipeline. The video graph is injectable for device-free tests."""
    graph = video or video_graph(monitor, encoder)
    if microphone:
        graph += (";[0:a]asplit=2[loop][voice];[loop]pan=stereo|c0=c0|c1=c1[system];"
                  "[voice]pan=stereo|c0=c2|c1=c3[mic]")
        audio = ["-map", "[system]", "-map", "[mic]", "-disposition:a:0", "default", "-disposition:a:1", "0"]
    else:
        audio = ["-map", "0:a:0"]
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-filter_complex_threads", "1"]
    if device:
        args += ["-init_hw_device", f"d3d11va=display:{monitor.adapter}", "-filter_hw_device", "display"]
    args += ["-f", "s16le", "-ar", "48000", "-ac", "4" if microphone else "2", "-i", "pipe:0",
             "-filter_complex", graph, "-map", "[desktop]", *audio,
             "-r", str(FPS), "-c:v", encoder, "-g", str(FPS), "-bf", "0"]
    if encoder == SOFTWARE:
        args += ["-preset", "ultrafast", "-tune", "zerolatency", "-crf", "21", "-threads:v", "2",
                 "-sc_threshold", "0", "-keyint_min", str(FPS), "-profile:v", "high",
                 "-x264-params", "8x8dct=1:cabac=1"]
    else:
        args += ["-b:v", "8M", "-maxrate", "10M", "-bufsize", "5M", "-force_key_frames", "expr:gte(t,n_forced)"]
        if encoder == "h264_nvenc":
            args += ["-forced-idr", "1", "-preset", "p3", "-tune", "ll", "-zerolatency", "1", "-no-scenecut", "1"]
        elif encoder == "h264_amf":
            args += ["-forced_idr", "1", "-usage", "lowlatency", "-async_depth", "1"]
        else:
            args += ["-forced_idr", "1", "-async_depth", "1", "-adaptive_i", "0"]
    return [*args, "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", "pipe:1"]


def audio_helper(work_dir: Path) -> Path:
    """Build the small Windows companion on demand with the compiler already used by Baird."""
    source = NATIVE_DIR / "bairdaudio.cs"
    work_dir.mkdir(parents=True, exist_ok=True)
    exe = work_dir / "bairdaudio.exe"
    if exe.is_file() and exe.stat().st_mtime >= source.stat().st_mtime:
        return exe
    compiler = csc_path()
    if compiler is None:
        raise RuntimeError("The Windows .NET Framework C# compiler is required for system audio capture.")
    next_exe = exe.with_suffix(f".{os.getpid()}.exe")
    try:
        proc = subprocess.run([str(compiler), "/nologo", "/optimize+", "/platform:x64",
                               f"/out:{next_exe}", str(source)], capture_output=True, text=True,
                              creationflags=NO_WINDOW, timeout=12)
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout + proc.stderr)[-500:])
        os.replace(next_exe, exe)
    finally:
        next_exe.unlink(missing_ok=True)
    return exe


def _terminate(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _drain(lines: BinaryIO, tail: deque, levels: dict | None = None):
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if levels is not None and line.startswith('{"event":"levels"'):
            try:
                data = json.loads(line)
                levels.update(audio_level=data["audio_level"], microphone_level=data["microphone_level"])
                continue
            except (KeyError, ValueError):
                pass
        tail.append(line)


class EncoderAttempt:
    def __init__(self, audio, video):
        self.audio = audio
        self.video = video
        self.stop = threading.Event()
        self.chunks = queue.Queue(maxsize=8)
        self.audio_errors = deque(maxlen=4)
        self.video_errors = deque(maxlen=4)
        self.levels = {}
        self.readers = [
            threading.Thread(target=self._read, daemon=True),
            threading.Thread(target=_drain, args=(audio.stderr, self.audio_errors, self.levels), daemon=True),
            threading.Thread(target=_drain, args=(video.stderr, self.video_errors), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def _read(self):
        try:
            while not self.stop.is_set():
                chunk = self.video.stdout.read(32768)
                while not self.stop.is_set():
                    try:
                        self.chunks.put(chunk, timeout=0.2)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    return
        except OSError:
            return

    def chunk(self, timeout: float) -> bytes | None:
        if self.audio.poll() not in (None, 0):
            raise RuntimeError("System audio capture stopped. " + " ".join(self.audio_errors))
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return b"" if self.video.poll() is not None else None

    def failure(self) -> str:
        return " ".join(self.video_errors)[-450:] or "The encoder did not deliver a complete key frame."

    def close(self):
        self.stop.set()
        for child in (self.video, self.audio):
            try:
                _terminate(child)
            except (OSError, subprocess.TimeoutExpired):
                pass  # The worker job closes remaining handles on exit.
        for reader in self.readers:
            reader.join(timeout=0.5)
        self.video.stdout.close()
        self.video.stderr.close()
        self.audio.stderr.close()


@contextmanager
def open_encoder(audio_command: list[str], command: list[str]):
    audio = subprocess.Popen(audio_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=0, creationflags=NO_WINDOW)
    bind_to_app(audio)
    try:
        video = subprocess.Popen(command, stdin=audio.stdout, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, bufsize=0, creationflags=NO_WINDOW)
    except OSError:
        _terminate(audio)
        audio.stderr.close()
        raise
    finally:
        audio.stdout.close()
    bind_to_app(video)
    session = EncoderAttempt(audio, video)
    try:
        yield session
    finally:
        session.close()


def _same_output(expected: DesktopMonitor, resolve) -> None:
    if resolve() != expected:
        raise BairdError("monitor_reselect", RESELECT, status=409)


def stream_attempt(session, monitor: DesktopMonitor, encoder: str, resolve, stop, sink, event) -> None:
    # Keep failed hardware attempts private. A retry starts an entirely new stream.
    pending = bytearray()
    initial = TsBuffer()
    started = False
    now = time.monotonic()
    deadline, next_check, next_stats, bytes_sent = now + START_TIMEOUT_S, now + LAYOUT_POLL_S, now + 1.0, 0
    while not stop.is_set():
        chunk = session.chunk(0.15)
        now = time.monotonic()
        if now >= next_check:
            _same_output(monitor, resolve)
            next_check = now + LAYOUT_POLL_S
        if chunk == b"":
            if started:
                raise RuntimeError(session.failure())
            raise EncoderStartError(session.failure())
        if chunk:
            if not started:
                initial.feed(chunk)
                pending.extend(chunk)
                if initial.latest()[1] and initial.audio_pid is not None:
                    _same_output(monitor, resolve)
                    event("ready", device=monitor.name, encoder=encoder,
                          input={"width": monitor.width, "height": monitor.height}, fps=FPS)
                    sink.write(pending)
                    bytes_sent += len(pending)
                    pending.clear()
                    started = True
                    initial.reset()
            else:
                sink.write(chunk)
                bytes_sent += len(chunk)
        if started:
            if chunk:
                sink.flush()
            if now >= next_stats:
                elapsed = now - (next_stats - 1.0)
                event("stats", kbps=round(bytes_sent * 8 / (elapsed * 1000)), **session.levels)
                next_stats, bytes_sent = now + 1.0, 0
        elif now >= deadline or len(pending) >= 4 * 1024 * 1024:
            raise EncoderStartError(session.failure())


def run_capture(resolve, available, open_attempt, microphone, stop, sink, event=emit):
    monitor = resolve()
    encoders = encoder_order(monitor, available)
    for encoder in encoders:
        if stop.is_set():
            return
        _same_output(monitor, resolve)
        try:
            with open_attempt(monitor, encoder, microphone) as session:
                stream_attempt(session, monitor, encoder, resolve, stop, sink, event)
            return
        except EncoderStartError as exc:
            if encoder == SOFTWARE:
                raise
            event("warning", message=f"{encoder} could not start; trying software encoding.", detail=str(exc))


def _listen_for_quit(stop):
    while True:
        data = os.read(sys.stdin.fileno(), 256)
        if not data or b"quit" in data:
            stop.set()
            return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--selection", required=True)
    parser.add_argument("--microphone", action="store_true")
    args = parser.parse_args()
    stop = threading.Event()
    threading.Thread(target=_listen_for_quit, args=(stop,), daemon=True).start()
    try:
        selected = normalize_selection(json.loads(args.selection))

        def resolve():
            return selected_monitor(selected, windows_inventory())

        resolve()
        audio = [str(audio_helper(args.work_dir))]
        if args.microphone:
            audio.append("--microphone")

        def open_attempt(monitor, encoder, microphone):
            return open_encoder(audio, ffmpeg_command(args.ffmpeg, monitor, encoder, microphone))

        run_capture(resolve, available_encoders(args.ffmpeg), open_attempt, args.microphone, stop, sys.stdout.buffer)
        return 0
    except BairdError as exc:
        emit("error", code=exc.code, message=exc.message)
    except BrokenPipeError:
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        emit("error", code="monitor_failed", message=str(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
