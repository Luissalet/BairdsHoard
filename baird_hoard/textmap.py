"""Where every piece of text is on a picture.

The reader is the OCR that ships with Windows (``Windows.Media.Ocr``), reached through ``native/bairdocr.ps1``: one
PowerShell process started on first use and kept while it is being used, a JSON request per line in and a JSON answer
per line out. It runs on this PC, needs nothing installed and answers in about a tenth of a second, so the copilot
can place a mark on "the button that says Guardar" without an assistant ever looking at the picture.

A text map is ``{"w", "h", "lines": [Line]}`` with ``Line = [x, y, w, h, text, [[x, y, w, h, word], ...]]`` in pixels
of the picture that was read. :func:`find_text` looks a phrase up in it, tolerating what OCR gets wrong (case,
accents, spacing, a few characters)."""

from __future__ import annotations

import difflib
import json
import logging
import os
import queue
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

from .errors import BairdError

log = logging.getLogger("baird.textmap")

SCRIPT = Path(__file__).resolve().parent.parent / "native" / "bairdocr.ps1"
FUZZY_MIN = 0.8            # how alike a fuzzy match must be (difflib ratio on the squashed strings)
PART_MIN = 0.6             # a phrase found inside a longer run of letters must be at least this much of it
MAX_SPAN_WORDS = 14


def fold(text: str) -> str:
    """Lower case without accents, white space collapsed."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.lower().split())


def squash(text: str) -> str:
    """:func:`fold` with everything but letters and digits removed: what two readings of the same label share."""
    return "".join(ch for ch in fold(text) if ch.isalnum())


def _union(boxes: list[list[float]]) -> list[float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def _overlap(a: list[float], b: list[float]) -> float:
    """Share of the smaller box that the two have in common."""
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / max(1e-9, min(a[2] * a[3], b[2] * b[3]))


def find_text(lines: list[Any], query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Places where ``query`` is written, best first: ``[{"text", "box": [x, y, w, h], "score", "exact"}]``.

    A run of words that is exactly the phrase scores 1; the phrase inside a longer word or run scores by how much of
    the run it fills (at least :data:`PART_MIN`); when nothing contains it, runs that read almost like it
    (:data:`FUZZY_MIN`) are offered. Equal scores come in reading order. A mark in the wrong place is worse than no
    mark, so anything weaker is not a match."""
    q = squash(query)
    if not q:
        return []
    found: list[dict[str, Any]] = []
    fuzzy: list[dict[str, Any]] = []
    for line in lines:
        words = [w for w in (line[5] if len(line) > 5 else []) if len(w) >= 5]
        keys = [squash(w[4]) for w in words]
        for i in range(len(words)):
            if not keys[i]:
                continue
            acc = ""
            for j in range(i, min(len(words), i + MAX_SPAN_WORDS)):
                acc += keys[j]
                span = words[i:j + 1]
                if acc == q:
                    found.append(_match(span, 1.0, True))
                    break
                if q in acc:
                    # Inside a longer run ("Guardar" in "Guardar(G)"): only when the first word is needed (else a later
                    # start is tighter) and the phrase is most of the run, so "ir" never lands on "Confirmar".
                    if q not in acc[len(keys[i]):] and len(q) >= 3 and len(q) >= PART_MIN * len(acc):
                        found.append(_match(span, 0.5 + 0.45 * len(q) / len(acc), False))
                    break
                if len(q) >= 5 and 0.6 * len(q) <= len(acc) <= 1.5 * len(q):
                    ratio = difflib.SequenceMatcher(None, q, acc, autojunk=False).ratio()
                    if ratio >= FUZZY_MIN:
                        fuzzy.append(_match(span, 0.5 * ratio, False))
                if len(acc) > len(q) * 1.5 + 8:
                    break
    pool = found or fuzzy
    pool.sort(key=lambda m: (-round(m["score"], 3), round(m["box"][1] / 12), m["box"][0]))
    out: list[dict[str, Any]] = []
    for m in pool:
        if all(_overlap(m["box"], kept["box"]) < 0.5 for kept in out):
            out.append(m)
            if len(out) >= limit:
                break
    return out


def _match(span: list[Any], score: float, exact: bool) -> dict[str, Any]:
    return {"text": " ".join(str(w[4]) for w in span), "box": _union([w[:4] for w in span]), "score": round(score, 3), "exact": exact}


def count_text(lines: list[Any], query: str) -> int:
    """How many places show ``query`` (exactly or inside a longer run; fuzzy readings count when nothing else does)."""
    return len(find_text(lines, query, limit=50))


def scaled(result: dict[str, Any], factor: float) -> dict[str, Any]:
    """The same text map with every number divided by ``factor`` (a picture that was enlarged before reading)."""
    if factor == 1:
        return result

    def box(b: list[Any]) -> list[Any]:
        return [round(b[0] / factor, 1), round(b[1] / factor, 1), round(b[2] / factor, 1), round(b[3] / factor, 1), *b[4:]]

    lines = [[*box(line[:4]), line[4], [box(w) for w in line[5]]] for line in result.get("lines", [])]
    return {**result, "w": round(result.get("w", 0) / factor), "h": round(result.get("h", 0) / factor), "lines": lines}


def powershell_path() -> Optional[Path]:
    """Windows PowerShell 5.1: the one that can call the Windows Runtime without extra packages."""
    if os.name != "nt":
        return None
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return exe if exe.is_file() else None


class TextReader:
    """Reads pictures with the Windows OCR helper. Thread-safe; one request at a time."""

    def __init__(self, script: Path = SCRIPT, *, idle_s: float = 120.0, timeout_s: float = 20.0):
        self.script = script
        self.idle_s = idle_s
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._answers: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        self._next_id = 0
        self._idle: Optional[threading.Timer] = None
        self.langs: list[str] = []
        self.error = ""

    def available(self) -> bool:
        return powershell_path() is not None and self.script.is_file()

    def read(self, path: Path, lang: str = "") -> dict[str, Any]:
        """The text map of the picture at ``path``. Raises :class:`BairdError` ``unavailable`` when Windows cannot read."""
        with self._lock:
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            try:
                proc = self._ensure()
                self._next_id += 1
                rid = self._next_id
                request = json.dumps({"id": rid, "path": str(path), "lang": lang or ""}, ensure_ascii=False) + "\n"
                assert proc.stdin is not None
                proc.stdin.write(request.encode("utf-8"))
                proc.stdin.flush()
                answer = self._take(rid)
            except (OSError, ValueError) as exc:
                self._kill()
                raise self._unavailable(f"{type(exc).__name__}: {exc}") from None
            finally:
                self._idle = threading.Timer(self.idle_s, self.stop)
                self._idle.daemon = True
                self._idle.start()
        if not answer.get("ok"):
            raise BairdError("failed", f"The picture could not be read: {answer.get('error') or 'no answer'}")
        return {"w": int(answer.get("w") or 0), "h": int(answer.get("h") or 0), "lang": str(answer.get("lang") or ""),
                "lines": answer.get("lines") or []}

    def stop(self) -> None:
        with self._lock:
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            self._kill()

    # ------------------------------------------------------------------ the helper process

    def _unavailable(self, why: str) -> BairdError:
        self.error = why[:200]
        return BairdError("unavailable", f"Text on the screen cannot be read here: {self.error}",
                          "Marks placed by coordinates still work.")

    def _ensure(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        exe = powershell_path()
        if exe is None or not self.script.is_file():
            raise self._unavailable("the Windows OCR helper is missing (it needs Windows 10 or later)")
        from .native import NO_WINDOW, bind_to_app
        self._answers = queue.Queue()
        proc = subprocess.Popen([str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.script)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        bind_to_app(proc)
        self._proc = proc
        threading.Thread(target=self._pump, args=(proc, self._answers), name="baird-ocr-out", daemon=True).start()
        try:
            hello = self._answers.get(timeout=self.timeout_s)
        except queue.Empty:
            hello = None
        if not hello or not hello.get("ready"):
            self._kill()
            raise self._unavailable(str((hello or {}).get("error") or "the Windows OCR helper did not start"))
        self.langs = [str(x) for x in (hello.get("langs") or [])]
        self.error = ""
        log.info("text reader started (languages: %s)", ", ".join(self.langs) or "none")
        return proc

    @staticmethod
    def _pump(proc: subprocess.Popen, answers: "queue.Queue[Optional[dict[str, Any]]]") -> None:
        out = proc.stdout
        if out is not None:
            for raw in iter(out.readline, b""):
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(data, dict):
                    answers.put(data)
        answers.put(None)

    def _take(self, rid: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                answer = self._answers.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                self._kill()
                raise self._unavailable("the Windows OCR helper did not answer in time") from None
            if answer is None:
                self._kill()
                raise self._unavailable("the Windows OCR helper stopped")
            if answer.get("id") == rid:
                return answer

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=1.5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
