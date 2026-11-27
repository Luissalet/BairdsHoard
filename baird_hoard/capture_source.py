"""Process-backed producers for the capture engine's MPEG-TS input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings
from .errors import BairdError


class CaptureSource(Protocol):
    """Provide a command that streams H.264 and AAC in MPEG-TS to stdout.

    The engine opens the process on demand. The stream must repeat PAT/PMT
    and emit a decodable key frame about once per second. A producer can
    report runtime metadata as JSON lines on stderr (ready, signal, stats).
    """

    def command(self, settings: Settings) -> list[str]: ...

    def initial_state(self) -> dict[str, Any]: ...


# Solid patches provide known targets for pixel and copilot-coordinate tests.
SYNTHETIC_MARKS = (
    "drawbox=x=156:y=116:w=104:h=104:color=0xff3030:t=fill,"
    "drawbox=x=1016:y=500:w=108:h=108:color=0x40e06e:t=fill"
)


@dataclass(frozen=True)
class SyntheticSource:
    ffmpeg: str | None
    video: str = ""
    marked: bool = True

    def initial_state(self) -> dict[str, Any]:
        return {"device": "synthetic" if self.marked else "demo", "encoder": "ffmpeg",
                "input": {"width": 1280, "height": 720}, "audio": True}

    def command(self, settings: Settings) -> list[str]:
        if not self.ffmpeg:
            label = "synthetic" if self.marked else "demo"
            raise BairdError("unavailable", f"ffmpeg is needed for the {label} source.")
        start = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-re"]
        pattern = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30"]
        tone = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        video = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "30"]
        pixel_format_and_audio = ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k"]
        output = ["-f", "mpegts", "pipe:1"]

        if self.marked:
            return [*start, *pattern, *tone, "-vf", SYNTHETIC_MARKS, *video,
                    "-keyint_min", "30", "-sc_threshold", "0", "-profile:v", "high", "-level:v", "4.0",
                    "-x264-params", "8x8dct=1:cabac=1", *pixel_format_and_audio, "-ac", "2", *output]
        if self.video:
            return [*start, "-stream_loop", "-1", "-i", self.video, *tone, "-map", "0:v:0", "-map", "1:a:0",
                    "-vf", "scale=1280:720,fps=30", *video, "-b:v", "6M", "-maxrate", "7M", "-bufsize", "3M",
                    *pixel_format_and_audio, "-muxdelay", "0", "-muxpreload", "0", *output]
        return [*start, *pattern, *tone, *video, *pixel_format_and_audio, *output]
