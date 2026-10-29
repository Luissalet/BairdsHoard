"""MPEG-2 transport stream bookkeeping: packet alignment, PAT/PMT, key frames and the in-memory past.

The device sends H.264 (stream type 0x1B) and AAC (0x0F) in a transport stream with a key frame every second. The
stream is cut into *segments* at every video access unit that carries an SPS or an IDR slice, and each segment is
stored with the latest PAT and PMT in front of it, so any run of consecutive segments is a valid stream that starts
on a key frame: a clip is just "the segments since T" written to a file, a still is "the last segment" decoded to
its last frame.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

TS = 188
SYNC = 0x47
H264_TYPES = (0x1B,)
AUDIO_TYPES = (0x0F, 0x11, 0x03, 0x04, 0x81)


@dataclass
class Segment:
    """A key frame and what follows it up to the next one. ``data`` starts with PAT + PMT."""

    t0: float                       # wall clock (time.time) of its first packet
    data: bytearray = field(default_factory=bytearray)
    frames: int = 0                 # video access units that started in it
    t1: float = 0.0                 # wall clock of its last packet

    @property
    def duration(self) -> float:
        return max(0.0, (self.t1 or self.t0) - self.t0)


def _payload(pkt: bytes) -> bytes:
    afc = (pkt[3] >> 4) & 0x3
    if afc in (0, 2):
        return b""
    start = 4
    if afc == 3:
        start = 5 + pkt[4]
    return pkt[start:] if start < TS else b""


def _has_keyframe_nal(payload: bytes) -> bool:
    """Whether a PES payload (or the start of one) holds an SPS (7) or an IDR slice (5)."""
    i = 0
    n = len(payload)
    while True:
        i = payload.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= n:
            return False
        nal = payload[i + 3] & 0x1F
        if nal in (5, 7):
            return True
        i += 3


def _sps_of(payload: bytes) -> Optional[bytes]:
    i = payload.find(b"\x00\x00\x01\x67")
    if i < 0:
        i = payload.find(b"\x00\x00\x01\x27")
    if i < 0 or i + 7 > len(payload):
        return None
    return payload[i + 4:i + 7]


class TsBuffer:
    """Feed it raw bytes; it keeps the last ``keep_seconds`` as key-frame segments and tells listeners about each chunk.

    ``feed`` returns the aligned bytes it consumed (whole packets) and whether a new segment started inside them, which
    is all a recorder needs: begin writing at a segment start, with the PAT/PMT in front."""

    def __init__(self, keep_seconds: float = 120.0, clock=time.time):
        self.keep_seconds = float(keep_seconds)
        self.clock = clock
        self._lock = threading.RLock()
        self._rest = b""
        self._segments: deque[Segment] = deque()
        self._current: Optional[Segment] = None
        self._pat: bytes = b""
        self._pmt: bytes = b""
        self._pmt_pid: Optional[int] = None
        self.video_pid: Optional[int] = None
        self.audio_pid: Optional[int] = None
        self.sps: Optional[bytes] = None      # profile_idc, constraint flags, level_idc
        self.bytes_total = 0
        self.last_data = 0.0

    # ------------------------------------------------------------------ input

    def reset(self) -> None:
        with self._lock:
            self._rest = b""
            self._segments.clear()
            self._current = None
            self._pat = self._pmt = b""
            self._pmt_pid = self.video_pid = self.audio_pid = None

    def feed(self, data: bytes) -> tuple[bytes, list[int]]:
        """Consume ``data``; returns ``(aligned, starts)``: the whole packets taken and the offsets inside them where a
        new key-frame segment begins."""
        now = self.clock()
        with self._lock:
            buf = self._rest + data
            # Re-synchronise on the sync byte if the stream ever slipped.
            pos = 0
            if buf and buf[0] != SYNC:
                pos = buf.find(bytes([SYNC]))
                if pos < 0:
                    self._rest = b""
                    return b"", []
            end = pos + ((len(buf) - pos) // TS) * TS
            aligned = bytes(buf[pos:end])
            self._rest = bytes(buf[end:])
            starts: list[int] = []
            for off in range(0, len(aligned), TS):
                pkt = aligned[off:off + TS]
                if pkt[0] != SYNC:
                    continue
                pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                pusi = bool(pkt[1] & 0x40)
                if pid == 0 and pusi:
                    self._pat = pkt
                    self._parse_pat(pkt)
                elif self._pmt_pid is not None and pid == self._pmt_pid and pusi:
                    self._pmt = pkt
                    self._parse_pmt(pkt)
                if pid == self.video_pid and pusi:
                    payload = _payload(pkt)
                    key = _has_keyframe_nal(payload)
                    if key:
                        if self.sps is None:
                            self.sps = _sps_of(payload)
                        self._start_segment(now)
                        starts.append(off)
                    if self._current is not None:
                        self._current.frames += 1
                if self._current is not None:
                    self._current.data += pkt
                    self._current.t1 = now
            self.bytes_total += len(aligned)
            if aligned:
                self.last_data = now
            self._trim(now)
            return aligned, starts

    def _parse_pat(self, pkt: bytes) -> None:
        p = _payload(pkt)
        if not p:
            return
        p = p[1 + p[0]:]                       # pointer field
        if len(p) < 12:
            return
        section_len = ((p[1] & 0x0F) << 8) | p[2]
        body = p[8:3 + section_len - 4]
        for i in range(0, len(body) - 3, 4):
            program = (body[i] << 8) | body[i + 1]
            pid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
            if program != 0:
                self._pmt_pid = pid
                return

    def _parse_pmt(self, pkt: bytes) -> None:
        p = _payload(pkt)
        if not p:
            return
        p = p[1 + p[0]:]
        if len(p) < 16:
            return
        section_len = ((p[1] & 0x0F) << 8) | p[2]
        info_len = ((p[10] & 0x0F) << 8) | p[11]
        i = 12 + info_len
        end = min(len(p), 3 + section_len - 4)
        while i + 5 <= end:
            stype = p[i]
            pid = ((p[i + 1] & 0x1F) << 8) | p[i + 2]
            es_len = ((p[i + 3] & 0x0F) << 8) | p[i + 4]
            if stype in H264_TYPES and self.video_pid is None:
                self.video_pid = pid
            elif stype in AUDIO_TYPES and self.audio_pid is None:
                self.audio_pid = pid
            i += 5 + es_len

    def _start_segment(self, now: float) -> None:
        if self._current is not None and self._current.frames:
            self._segments.append(self._current)
        self._current = Segment(t0=now, data=bytearray(self._pat + self._pmt), t1=now)

    def _trim(self, now: float) -> None:
        while self._segments and now - self._segments[0].t0 > self.keep_seconds + 2:
            self._segments.popleft()

    # ------------------------------------------------------------------ output

    @property
    def header(self) -> bytes:
        with self._lock:
            return self._pat + self._pmt

    def codec_string(self) -> str:
        """``avc1.PPCCLL`` for Media Source Extensions (High 4.0 when the SPS has not been seen yet)."""
        sps = self.sps or bytes([0x64, 0x00, 0x28])
        video = "avc1.%02x%02x%02x" % (sps[0], sps[1], sps[2])
        return video + (",mp4a.40.2" if self.audio_pid is not None else "")

    def since(self, seconds: float) -> list[bytes]:
        """The segments that cover the last ``seconds`` (from the key frame at or before ``now - seconds``), the one
        in progress included."""
        with self._lock:
            now = self.clock()
            segs = list(self._segments)
            if self._current is not None and self._current.frames:
                segs.append(self._current)
            if not segs:
                return []
            cut = now - float(seconds)
            first = 0
            for i, s in enumerate(segs):
                if s.t0 <= cut:
                    first = i
            return [bytes(s.data) for s in segs[first:]]

    def tail(self) -> tuple[bytes, int]:
        """The segment in progress (from its key frame) and the stream offset right after it, read atomically: a live
        remuxer starts with these bytes and then takes every chunk that ends after the offset."""
        with self._lock:
            seg = self._current.data if self._current is not None and self._current.frames else b""
            return bytes(seg), self.bytes_total

    def latest(self) -> tuple[bytes, int]:
        """The newest segment with at least two frames (bytes, frame count) for a still; ``(b"", 0)`` with no picture."""
        with self._lock:
            cands = []
            if self._current is not None:
                cands.append(self._current)
            if self._segments:
                cands.append(self._segments[-1])
            for s in cands:
                if s.frames >= 2:
                    return bytes(s.data), s.frames
            return b"", 0

    def seconds_kept(self) -> float:
        with self._lock:
            if not self._segments and self._current is None:
                return 0.0
            first = self._segments[0].t0 if self._segments else self._current.t0  # type: ignore[union-attr]
            return max(0.0, self.clock() - first)
