"""Random access to one original OCR view without loading a recording-sized HTML into memory."""
from functools import lru_cache
import html
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
    return {"stamp": stamp, "stamps": [s for s, _ in index], "version": f"{st.st_size}-{st.st_mtime_ns}",
            "text": html.unescape(fields[-1].decode("utf-8")).replace("\r\n", "\n"), "notes": "\n".join(warnings),
            "image": "data:image/jpeg;base64," + picture[1].decode("ascii")}
