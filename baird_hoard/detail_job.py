"""Local GPU rereading of an already saved Baird document, independent of the recorder."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from .errors import BairdError
from .hoard_link.lease import Lease
from .native import NO_WINDOW, bind_to_app


class DetailJob:
    def __init__(self, library):
        self.library = library
        self.lock = threading.RLock()
        self.report = {"state": "idle"}
        self.proc = None
        self.cancelled = False
        self.closed = False
        self.runtime = None
        try:
            cfg = json.loads((library.config.data_dir / "unlimited-ocr.json").read_text(encoding="utf-8-sig"))
            python, model = Path(cfg["python"]), Path(cfg["model"])
            if python.is_absolute() and model.is_absolute() and python.is_file() and (model / "model.safetensors").is_file():
                self.runtime = (python, model, cfg.get("gpu"))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def status(self):
        with self.lock:
            return {**self.report, "available": bool(self.runtime)}

    def start(self, source: Path, mode: str):
        if source.suffix.lower() != ".html" or mode not in ("document", "code"):
            raise BairdError("invalid", "Elige una transcripción HTML de Baird y un modo de lectura.")
        with self.lock:
            if not self.runtime or self.closed:
                raise BairdError("unavailable", "UnlimitedOCR local no está disponible.")
            if self.report["state"] == "running":
                raise BairdError("busy", "Hay una segunda lectura en curso.")
            self.cancelled = False
            self.report = {"state": "running", "source": source.name, "views": 0}
            threading.Thread(target=self._run, args=(source, mode), name="baird-detail", daemon=True).start()
            return self.status()

    def cancel(self, *, closing=False):
        with self.lock:
            self.closed |= closing
            self.cancelled = True
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
            return self.status()

    def _run(self, source, mode):
        partial = None
        final = {}
        try:
            path = self.library._new_path("Lectura detallada", ".html")
            python, model, gpus = self.runtime
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".html.part", delete=False) as f:
                partial = Path(f.name)
            with Lease(10000, owner="baird", purpose="Document OCR", gpu=gpus, timeout_s=60, autostart=False) as lease:
                with self.lock:
                    if self.cancelled:
                        return
                    if lease.gpu is None:
                        raise RuntimeError("No hay una GPU disponible para UnlimitedOCR.")
                    args = [str(python), "-X", "utf8", "-m", "baird_hoard.detail_worker", str(model),
                            str(lease.gpu), str(source), mode, str(partial)]
                    proc = self.proc = subprocess.Popen(args, cwd=Path(__file__).resolve().parents[1],
                        env={**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                    bind_to_app(proc)
                reason = ""
                last_reply = time.monotonic()
                stop_timer = threading.Event()
                timed_out = threading.Event()

                def watch():
                    while not stop_timer.wait(10):
                        if time.monotonic() - last_reply > 300:
                            timed_out.set()
                            proc.kill()
                            return

                threading.Thread(target=watch, name="baird-detail-timeout", daemon=True).start()
                try:
                    for line in proc.stdout:
                        last_reply = time.monotonic()
                        try:
                            progress = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(progress, dict):
                            continue
                        if "views" in progress:
                            with self.lock:
                                self.report["views"] = progress["views"]
                        if "error" in progress:
                            reason = str(progress["error"])[:300]
                    if proc.wait() or not self.report["views"]:
                        if timed_out.is_set():
                            raise RuntimeError("UnlimitedOCR dejó de responder durante cinco minutos.")
                        raise RuntimeError(reason or "El lector local terminó sin completar el documento.")
                finally:
                    stop_timer.set()
                    proc.stdout.close()
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                with self.lock:
                    if self.cancelled:
                        return
                    partial.rename(path)
                    final = {"state": "done", "name": path.name}
        except Exception as exc:
            final = {"state": "failed", "error": str(exc)[:300]}
        finally:
            try:
                if partial:
                    partial.unlink(missing_ok=True)
            finally:
                with self.lock:
                    self.proc = None
                    self.report.update(final if final.get("state") == "done" or not self.cancelled else {"state": "cancelled"})
