"""Feed private, pull-controlled inputs to the encoder over its stdin."""

from __future__ import annotations

import json
import queue
import struct
import threading
import time
from contextlib import ExitStack


AUDIO_RATE = 48_000
AUDIO_DEADLINE = 1.0


def _chunk(tag: bytes, content: bytes) -> bytes:
    return tag + struct.pack("<I", len(content)) + content + b"\0" * (len(content) % 2)


def _stream(kind, handler, scale, rate, sample_size, fmt, width=0, height=0):
    header = struct.pack("<4s4sIHH8I4h", kind, handler, 0, 0, 0, 0, scale, rate, 0, 0, 0, 0xFFFFFFFF,
                         sample_size, 0, 0, min(width, 32767), min(height, 32767))
    return _chunk(b"LIST", b"strl" + _chunk(b"strh", header) + _chunk(b"strf", fmt))


def avi_header(width: int, height: int, fps: int, channels: int) -> bytes:
    """Streaming AVI with top-down BGR0 video and interleaved PCM."""
    block = channels * 2
    main = struct.pack("<14I", 1_000_000 // fps, 0, 0, 0x100, 0, 0, 2, 0, width, height, 0, 0, 0, 0)
    image = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
    sound = struct.pack("<HHIIHHH", 1, channels, AUDIO_RATE, AUDIO_RATE * block, block, 16, 0)
    video = _stream(b"vids", b"\0" * 4, 1, fps, 0, image, width, height)
    audio = _stream(b"auds", b"\0" * 4, block, AUDIO_RATE * block, block, sound)
    # FFmpeg reads the live stream until EOF. Neither list can be patched on a pipe.
    unbounded = struct.pack("<I", 0xFFFFFFFF)
    return b"RIFF" + unbounded + b"AVI " + _chunk(b"LIST", b"hdrl" + _chunk(b"avih", main) + video + audio) + (
        b"LIST" + unbounded + b"movi"
    )


class DesktopSampler:
    """Keep the latest desktop frame independently of the PCM clock."""

    def __init__(self, monitor, frame_bytes, frames, gate, fps):
        self.packet = _chunk(b"00db", bytes(frame_bytes))
        self.error: Exception | None = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, args=(monitor, frame_bytes, frames, gate, fps),
                                       name="baird-desktop-sampler", daemon=True)

    def _sample(self, monitor, frame_bytes, frames, gate, fps):
        try:
            with ExitStack() as owned:
                grab = gate.acquire("video", lambda: owned.enter_context(frames(monitor)))
                period = 1 / fps
                while not self.stop.is_set():
                    started = time.monotonic()
                    picture = gate.acquire("video", grab)
                    if picture is not None:
                        if len(picture) != frame_bytes:
                            raise ValueError("The desktop frame has an unexpected size.")
                        self.packet = _chunk(b"00db", picture)
                    self.ready.set()
                    # Slow pulls must leave room for the audio helper to acquire its own permits.
                    self.stop.wait(period if picture is None else max(0, period - (time.monotonic() - started)))
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=0.5)


def feed_samples(sink, pcm, monitor, size, frames, gate, stop, fps, channels):
    width, height = size
    frame_bytes = width * height * 4
    samples = AUDIO_RATE // fps * channels * 2
    desktop = DesktopSampler(monitor, frame_bytes, frames, gate, fps)
    desktop.thread.start()
    try:
        while not desktop.ready.wait(0.05):
            if stop.is_set():
                return
        sink.write(avi_header(width, height, fps, channels))
        while not stop.is_set():
            audio = pcm.read(samples)
            if desktop.error is not None:
                raise desktop.error
            if len(audio) != samples:
                return
            # The helper emits clocked, continuous PCM, including source silence.
            # Pair every block with the last frame, even when DXGI has no update.
            sink.write(desktop.packet)
            sink.write(_chunk(b"01wb", audio))
            sink.flush()
    finally:
        desktop.close()


class AudioGrants:
    """Hold the policy lease from the helper's request through its native-call acknowledgement."""

    def __init__(self, audio, gate):
        self.audio = audio
        self.gate = gate
        self.messages: queue.Queue[bytes] = queue.Queue()
        self.levels: dict = {}
        self.failure: Exception | None = None
        self.threads = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._serve, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _read(self):
        try:
            for line in self.audio.stderr:
                message = line.strip()
                if message in (b"@acquire", b"@complete"):
                    self.messages.put(message)
                elif message.startswith(b'{"event":"levels"'):
                    event = json.loads(message)
                    self.levels.update(audio_level=event["audio_level"], microphone_level=event["microphone_level"])
        except (OSError, ValueError, KeyError):
            pass
        finally:
            self.messages.put(b"")

    def _settle_issued_grant(self):
        try:
            if self.audio.poll() is None:
                self.audio.kill()
        except OSError:
            pass
        while True:
            try:
                if self.audio.poll() is not None:
                    return
            except OSError:
                pass
            try:
                if self.messages.get(timeout=0.05) == b"@complete":
                    return
            except queue.Empty:
                pass

    def _serve(self):
        def native_call():
            try:
                self.audio.stdin.write(b"1")
                self.audio.stdin.flush()
                if self.messages.get(timeout=AUDIO_DEADLINE) != b"@complete":
                    raise RuntimeError("The audio capture did not acknowledge its acquisition.")
            except (OSError, RuntimeError, queue.Empty):
                # A permission byte may already be in the helper. Keep the lease
                # even if we cannot terminate it and until we know it is harmless.
                self._settle_issued_grant()
                raise RuntimeError("The audio capture did not acknowledge its acquisition.") from None

        try:
            for message in iter(self.messages.get, b""):
                if message != b"@acquire":
                    raise RuntimeError("Unexpected audio acquisition response.")
                self.gate.acquire("audio", native_call)
        except Exception as error:
            self.failure = error
        finally:
            try:
                self.audio.stdin.close()
            except OSError:
                pass
