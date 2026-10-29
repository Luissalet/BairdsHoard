from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")


def make_ts(path: Path, seconds: float = 4.0, gop: int = 30) -> bytes:
    """A transport stream like the device's: H.264 (key frame every ``gop`` frames at 30 fps) + AAC."""
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-f", "mpegts", str(path)], check=True)
    return path.read_bytes()


@pytest.fixture
def ts_bytes(tmp_path):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    return make_ts(tmp_path / "src.ts")
