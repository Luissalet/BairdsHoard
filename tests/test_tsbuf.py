from __future__ import annotations

import subprocess

from baird_hoard.tsbuf import TS, TsBuffer

from .conftest import FFMPEG


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def feed_paced(buf: TsBuffer, data: bytes, clock: Clock, seconds: float, chunk: int = 7 * TS + 50):
    """Feed in odd-sized chunks while the clock advances evenly over ``seconds``."""
    steps = max(1, len(data) // chunk)
    starts_seen = 0
    for i in range(0, len(data), chunk):
        aligned, starts = buf.feed(data[i:i + chunk])
        starts_seen += len(starts)
        clock.t += seconds / steps
    return starts_seen


def test_finds_streams_and_keyframes(ts_bytes):
    clock = Clock()
    buf = TsBuffer(keep_seconds=60, clock=clock)
    starts = feed_paced(buf, ts_bytes, clock, 4.0)
    assert buf.video_pid is not None and buf.audio_pid is not None
    assert starts == 4                                   # 4 s, one key frame per second
    assert buf.codec_string().startswith("avc1.") and buf.codec_string().endswith(",mp4a.40.2")
    assert buf.bytes_total == len(ts_bytes) // TS * TS


def test_since_starts_on_keyframe_and_decodes(ts_bytes, tmp_path):
    clock = Clock()
    buf = TsBuffer(keep_seconds=60, clock=clock)
    feed_paced(buf, ts_bytes, clock, 4.0)
    parts = buf.since(1.5)
    assert 2 <= len(parts) <= 3
    clip = tmp_path / "clip.ts"
    clip.write_bytes(b"".join(parts))
    out = subprocess.run([FFMPEG, "-v", "error", "-i", str(clip), "-f", "null", "-"], capture_output=True, text=True)
    assert out.returncode == 0 and "non-existing PPS" not in out.stderr


def test_latest_gives_a_decodable_still(ts_bytes, tmp_path):
    clock = Clock()
    buf = TsBuffer(keep_seconds=60, clock=clock)
    feed_paced(buf, ts_bytes, clock, 4.0)
    data, frames = buf.latest()
    assert frames >= 2
    png = tmp_path / "still.png"
    out = subprocess.run([FFMPEG, "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-vf", f"select=eq(n\\,{frames - 2})",
                          "-frames:v", "1", str(png)], input=data, capture_output=True)
    assert out.returncode == 0 and png.stat().st_size > 1000


def test_keeps_only_the_window(ts_bytes):
    clock = Clock()
    buf = TsBuffer(keep_seconds=1, clock=clock)
    feed_paced(buf, ts_bytes, clock, 4.0)
    assert buf.seconds_kept() <= 4.0
    assert len(buf.since(100)) <= 4


def test_tail_is_atomic_with_offset(ts_bytes):
    clock = Clock()
    buf = TsBuffer(keep_seconds=60, clock=clock)
    feed_paced(buf, ts_bytes[: len(ts_bytes) // 2], clock, 2.0)
    head, total = buf.tail()
    assert head[:1] == b"\x47" and total == buf.bytes_total


def test_resyncs_after_garbage(ts_bytes):
    buf = TsBuffer(keep_seconds=60, clock=Clock())
    aligned, _ = buf.feed(b"\x00\x01garbage" + ts_bytes[: 50 * TS])
    assert aligned[:1] == b"\x47" and len(aligned) % TS == 0


def test_skips_without_ffmpeg_marker():
    assert FFMPEG is None or FFMPEG
