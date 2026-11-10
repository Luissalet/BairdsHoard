"""Text of the screenshots, so they can be found by what they show.

The OCR engine is the family's: Kafka's Hoard, tool ``ocr_image``, reached through the Hub (``hoard_link.fam_docs``).
Each screenshot is read once, in the background, and its text is kept in ``<data>/ocr-index.json`` next to the file's
modification time; a changed or new file is read again. When Kafka is not running the screenshots wait and are read
later (every minute at most), nothing else is affected."""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from .config import Settings
from .errors import BairdError
from .hoard_link import fam_docs
from .hoard_link.atomic import read_json, write_json_atomic
from .media import IMAGE_EXT, Library

log = logging.getLogger("baird.ocr")

RETRY_S = 60.0
MAX_TEXT = 20000


def fold(text: str) -> str:
    """Lower case without accents, white space collapsed: what searches compare."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.lower().split())


def snippet(text: str, query_terms: list[str], width: int = 70) -> str:
    """A piece of ``text`` around the first term that matches, on one line."""
    flat = " ".join(text.split())
    pieces = [unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii").lower() for ch in flat]
    low = "".join(pieces)
    starts: list[int] = []                 # position in ``flat`` of every character of ``low``
    for i, piece in enumerate(pieces):
        starts.extend([i] * len(piece))
    for term in query_terms:
        at = low.find(term)
        if at >= 0:
            start = max(0, starts[at] - width // 2)
            return ("…" if start else "") + flat[start:start + width * 2]
    return flat[: width * 2]


OcrFn = Callable[[str], dict[str, Any]]


class OcrIndex:
    def __init__(self, path: Path, library: Library, settings: Settings, ocr_fn: Optional[OcrFn] = None, *, background: bool = True):
        self.path = path
        self.library = library
        self.settings = settings
        self.ocr_fn: OcrFn = ocr_fn or (lambda p: fam_docs.ocr_image(p, lang="es", timeout_s=120.0))
        self.background = background
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = read_json(path, default={}) or {}
        self._queue: list[str] = []
        self._retry_at = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error = ""

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if not self.background or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="baird-ocr", daemon=True)
        self._thread.start()
        if self.settings["ocr_auto"]:
            self.reindex()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------------ hooks from the library

    def enqueue(self, path: Path) -> None:
        if not self.settings["ocr_auto"]:
            return
        with self._lock:
            if path.name not in self._queue:
                self._queue.append(path.name)
        self._wake.set()

    def renamed(self, old: str, new: str) -> None:
        with self._lock:
            entry = self._data.pop(old, None)
            if entry is not None:
                self._data[new] = entry
                self._save()
            self._queue = [new if n == old else n for n in self._queue]

    def deleted(self, name: str) -> None:
        with self._lock:
            if self._data.pop(name, None) is not None:
                self._save()
            self._queue = [n for n in self._queue if n != name]

    # ------------------------------------------------------------------ reading

    def reindex(self) -> dict[str, Any]:
        """Queue every screenshot whose text is missing or out of date. Returns the counts."""
        queued = 0
        for item in self.library.list("image", 5000):
            name = item["name"]
            if self._needs(name, Path(item["path"])):
                with self._lock:
                    if name not in self._queue:
                        self._queue.append(name)
                        queued += 1
        self._wake.set()
        return {"queued": queued, **self.status()}

    def _needs(self, name: str, path: Path) -> bool:
        with self._lock:
            entry = self._data.get(name)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return False
        return not entry or entry.get("mtime_ns") != mtime or ("error" in entry and entry.get("retry_at", 0) <= time.time())

    def read_now(self, name: str) -> dict[str, Any]:
        """Read one screenshot's text now (waits for the OCR); returns ``{name, text, backend}``. Raises
        :class:`BairdError` when the OCR is not available."""
        path = self.library.path_of(name)
        if path.suffix.lower() not in IMAGE_EXT:
            raise BairdError("invalid", "Only screenshots have text.")
        entry = self._read(name, path)
        if "error" in entry:
            raise BairdError("unavailable", f"The OCR could not read {name}: {entry['error']}",
                             "OCR is Kafka's Hoard: start it from the Hub and try again.")
        return {"name": name, "text": entry["text"], "backend": entry.get("backend", "")}

    def _read(self, name: str, path: Path) -> dict[str, Any]:
        mtime = path.stat().st_mtime_ns
        res = self.ocr_fn(str(path))
        if res.get("ok"):
            text = str(res.get("text") or "").strip()[:MAX_TEXT]
            entry = {"text": text, "mtime_ns": mtime, "at": time.time(), "backend": str(res.get("backend") or "")}
            self.last_error = ""
        else:
            self.last_error = str(res.get("error") or "OCR failed")[:200]
            entry = {"error": self.last_error, "kind": res.get("kind", ""), "mtime_ns": mtime, "at": time.time(),
                     "retry_at": time.time() + RETRY_S, "text": ""}
        with self._lock:
            self._data[name] = entry
            self._save()
        return entry

    def _save(self) -> None:
        write_json_atomic(self.path, self._data)

    def _loop(self) -> None:
        while not self._stop.is_set():
            name = None
            with self._lock:
                if self._queue and time.time() >= self._retry_at:
                    name = self._queue.pop(0)
            if name is None:
                self._wake.wait(timeout=RETRY_S)
                self._wake.clear()
                if time.time() >= self._retry_at and self.settings["ocr_auto"] and self._has_failed():
                    self.reindex()
                continue
            try:
                path = self.library.path_of(name)
            except BairdError:
                continue
            try:
                entry = self._read(name, path)
            except Exception as exc:  # noqa: BLE001
                log.debug("ocr of %s failed", name, exc_info=True)
                self.last_error = str(exc)[:200]
                continue
            if "error" in entry and entry.get("kind") in ("hub_down", "app_down", "app_missing", "tool_missing", "timeout"):
                # Nobody to read it right now: wait before the next try instead of failing every screenshot in a row.
                self._retry_at = time.time() + RETRY_S

    def _has_failed(self) -> bool:
        with self._lock:
            return any("error" in e for e in self._data.values())

    # ------------------------------------------------------------------ search and status

    def text_of(self, name: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(name)
        return None if not entry or "error" in entry else entry.get("text", "")

    def search(self, query: str, limit: int = 50) -> dict[str, Any]:
        """Screenshots whose text (or name) contains every word of ``query``, ignoring case and accents."""
        terms = [t for t in fold(query).split() if t]
        if not terms:
            raise BairdError("invalid", "Write something to search for.")
        out: list[dict[str, Any]] = []
        for item in self.library.list("image", 5000):
            text = self.text_of(item["name"]) or ""
            hay = fold(item["name"] + " " + text)
            if all(t in hay for t in terms):
                out.append({**item, "snippet": snippet(text, terms), "matched_text": bool(text)})
                if len(out) >= limit:
                    break
        return {"query": query, "items": out, "unread": self.status()["unread"]}

    def status(self) -> dict[str, Any]:
        images = self.library.list("image", 5000)
        with self._lock:
            have = sum(1 for i in images if i["name"] in self._data and "error" not in self._data[i["name"]])
            queued = len(self._queue)
        return {"enabled": bool(self.settings["ocr_auto"]), "indexed": have, "unread": max(0, len(images) - have),
                "queued": queued, "error": self.last_error}
