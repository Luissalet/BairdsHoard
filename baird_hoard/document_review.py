"""Random access to one original OCR view without loading a recording-sized HTML into memory."""
from functools import lru_cache
import html
import json
from pathlib import Path
import re

from .errors import BairdError

LIMIT = 32 * 1024 * 1024
STAMP = re.compile(rb"<section><h2>(\d+:[0-5]\d\.\d)</h2>")


@lru_cache(maxsize=8)
def _index(path, size, modified):
    result = []
    with path.open("rb") as stream:
        offset = 0
        while line := stream.readline(LIMIT):
            if len(line) >= LIMIT:
                raise BairdError("invalid", "Vista demasiado grande para revisar.")
            if match := STAMP.match(line):
                result.append((match[1].decode("ascii"), offset))
            offset += len(line)
    if not result:
        raise BairdError("invalid", "El documento no contiene vistas de Baird.")
    return result


def original_view(path: Path, number: int = 0):
    if path.suffix.lower() != ".html":
        raise BairdError("invalid", "Elige un documento HTML de Baird.")
    st = path.stat()
    index = _index(path, st.st_size, st.st_mtime_ns)
    if not 0 <= number < len(index):
        raise BairdError("invalid", "Vista fuera del documento.")
    stamp, offset = index[number]
    with path.open("rb") as stream:
        stream.seek(offset)
        chunks = bytearray()
        while len(chunks) < LIMIT:
            line = stream.readline(LIMIT - len(chunks))
            if not line:
                break
            chunks.extend(line)
            if b"</section>" in line:
                break
    raw = bytes(chunks).partition(b"</section>")[0]
    picture = re.search(rb'<img [^>]*src="data:image/jpeg;base64,([A-Za-z0-9+/=]+)"', raw)
    fields = re.findall(rb"<pre>(.*?)</pre>", raw, re.S)
    if not picture or not fields:
        raise BairdError("invalid", "La vista está incompleta.")
    warnings = [html.unescape(re.sub("<br\\s*/?>", "\n", s.decode("utf-8"))) for s in re.findall(rb"<p>(.*?)</p>", raw, re.S)]
    text = html.unescape(fields[-1].decode("utf-8")).replace("\r\n", "\n")
    return {"stamp": stamp, "stamps": [s for s, _ in index], "version": f"{st.st_size}-{st.st_mtime_ns}",
            "text": text, "notes": "\n".join(warnings), "areas": _review_areas(raw, text, warnings),
            "image": "data:image/jpeg;base64," + picture[1].decode("ascii")}


def _review_areas(raw, text, warnings):
    lines = text.split("\n")
    tag = re.search(rb'<span hidden data-ocr-areas="([0-9,\[\]\-]{1,200000})"></span>', raw)
    if tag:
        try:
            areas = json.loads(tag[1])
        except (ValueError, RecursionError):
            areas = []
        valid = []
        for area in areas if isinstance(areas, list) else ():
            if not isinstance(area, list) or len(area) != 7 or not all(type(v) is int for v in area):
                continue
            row, start, end, x, y, width, height = area
            if (0 <= row < len(lines) and (start == end == -1 or 0 <= start <= end <= len(lines[row]))
                    and min(x,y) >= 0 and 0 < width <= 32768-x and 0 < height <= 32768-y):
                valid.append(area)
        if valid:
            return valid
    # Older exports have a vertical coordinate but no measured character positions.
    occupied = [i for i, line in enumerate(lines) if line.strip()]
    return [[occupied[int(n)-1], -1, -1, 0, int(y), 0, 22]
            for n,y in re.findall(r"Línea visible (\d{1,5}) \(y=(\d{1,5}) px\)", "\n".join(warnings))
            if 1 <= int(n) <= len(occupied) and int(y) < 32768]
