"""The monitor transport keeps its audio rate while the desktop stays still."""

from contextlib import contextmanager
import io
import struct
import threading
import time

from baird_hoard.monitor_input import feed_samples

from .test_monitor_privacy import HubDouble, MONITOR, guard


def test_static_desktop_does_not_hold_back_timed_pcm():
    fps, frames = 30, 36
    pcm_block = b"\x34\x12\x34\x12" * (48_000 // fps)
    picture = b"\x24\x7a\xcd\x00" * (8 * 8)
    acquired = []

    class ClockedTone:
        def read(self, size):
            assert size == len(pcm_block)
            if self.remaining == 0:
                return b""
            self.remaining -= 1
            deadline = epoch + (frames - self.remaining) / fps
            time.sleep(max(0, deadline - time.monotonic()))
            return pcm_block

    @contextmanager
    def static_monitor(_):
        def grab():
            acquired.append(time.monotonic())
            if len(acquired) == 1:
                return picture
            # Simulate a slow display pull that sees no newer desktop frame.
            time.sleep(0.067)
            return None

        yield grab

    tone = ClockedTone()
    tone.remaining = frames
    output = io.BytesIO()
    epoch = time.monotonic()
    feed_samples(output, tone, MONITOR, (8, 8), static_monitor, guard(HubDouble()), threading.Event(), fps, 2)
    elapsed = time.monotonic() - epoch
    assert abs(elapsed - frames / fps) < 0.4

    chunks = output.getvalue().split(b"movi", 1)[1]
    frame = b"00db" + struct.pack("<I", len(picture)) + picture
    audio = b"01wb" + struct.pack("<I", len(pcm_block)) + pcm_block
    assert chunks == (frame + audio) * frames
    assert len(acquired) > 1
