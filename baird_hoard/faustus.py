"""Send a screenshot or clip to Faustus: a new chat is created with the file attached to its message box.

Where Faustus answers is what the family says: ``BAIRD_FAUSTUS_URL`` / ``HOARD_FAUSTUS_URL`` when set, the address the
Hub reports in ``GET /api/placement`` when Faustus was moved to a Spark, and the Faustus of this PC (7000, 7001).
The steps, all plain Faustus routes:

1. ``POST /api/hoard-copilot/conversations`` makes the chat (``POST /api/session`` is the fallback);
2. ``POST /api/upload`` stores the file for that chat;
3. ``PUT /api/sessions/<id>/draft`` puts the file (and a message, if given) in the chat's message box.

Opening ``<faustus>/studio?s=<id>`` shows it, ready to send. The person chooses the model and presses send."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from .errors import BairdError
from .hoard_link import family
from .hoard_link._hubclient import fetch

LOCAL_URLS = ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
MAX_UPLOAD = 200 * 1024 * 1024


def faustus_candidates(hub_get=fetch) -> list[str]:
    """Faustus addresses to try, best first."""
    urls: list[str] = []
    for var in ("BAIRD_FAUSTUS_URL", "HOARD_FAUSTUS_URL"):
        value = (os.environ.get(var) or "").strip().rstrip("/")
        if value:
            urls.append(value)
    try:
        status, data = hub_get(family._hub() + "/api/placement", timeout=2.0)
        if status == 200 and isinstance(data, dict) and data.get("location") == "sparks" and not (data.get("job") or {}).get("running"):
            url = (data.get("urls") or {}).get("faustus")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url.rstrip("/"))
    except Exception:  # noqa: BLE001 - the Hub is optional
        pass
    urls.extend(LOCAL_URLS)
    return list(dict.fromkeys(urls))


def _headers() -> dict[str, str]:
    token = (os.environ.get("HOARD_FAUSTUS_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def find_faustus(candidates: Optional[list[str]] = None, client: Optional[httpx.Client] = None) -> str:
    """The first candidate whose ``/api/health`` is healthy. Raises :class:`BairdError` ``unavailable`` when none answers."""
    urls = candidates if candidates is not None else faustus_candidates()
    own = client is None
    client = client or httpx.Client(trust_env=False)
    tried = []
    try:
        for url in urls:
            local = url.startswith(("http://127.", "http://localhost"))
            try:
                resp = client.get(url + "/api/health", headers=_headers(), timeout=2.0 if local else 8.0)
                if resp.status_code == 200 and resp.json().get("status") == "healthy":
                    return url
                tried.append(f"{url} ({resp.status_code})")
            except (httpx.HTTPError, ValueError):
                tried.append(f"{url} (sin respuesta)")
    finally:
        if own:
            client.close()
    raise BairdError("unavailable", "No Faustus answers.", "Start Faustus (or check the Hub's «Run on» choice). Tried: " + ", ".join(tried))


def _check(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("detail") or body.get("error") or body)[:200]
        except ValueError:
            detail = resp.text[:200]
        raise BairdError("failed", f"Faustus refused {what} ({resp.status_code}).", detail)
    try:
        return resp.json()
    except ValueError:
        return {}


def send_file(path: Path, message: str = "", *, name: str = "", client: Optional[httpx.Client] = None,
              base_url: Optional[str] = None) -> dict[str, Any]:
    """Open a Faustus chat with ``path`` attached. Returns ``{session_id, url, faustus, file_id}``."""
    size = path.stat().st_size
    if size > MAX_UPLOAD:
        raise BairdError("invalid", f"{path.name} is {size // 1_000_000} MB; Faustus takes files up to {MAX_UPLOAD // 1_000_000} MB.",
                         "Make a light copy of the video first.")
    own = client is None
    client = client or httpx.Client(trust_env=False, timeout=120.0)
    try:
        base = (base_url or find_faustus(client=client)).rstrip("/")
        headers = _headers()
        title = (name or f"Baird: {path.stem}")[:80]
        session_id = ""
        try:
            conv = client.post(base + "/api/hoard-copilot/conversations", headers=headers, timeout=30,
                               json={"name": title, "context": {"origin": {"app_id": "baird", "data_space_id": "baird",
                                                                           "app_name": "Baird's Hoard"}}})
            if conv.status_code < 400:
                session_id = str(conv.json().get("session_id") or "")
        except (httpx.HTTPError, ValueError):
            session_id = ""
        if not session_id:
            made = _check(client.post(base + "/api/session", headers=headers, timeout=30,
                                      data={"name": title, "skip_validation": "true"}), "to create the chat")
            session_id = str(made.get("id") or "")
        if not session_id:
            raise BairdError("failed", "Faustus did not return the id of the new chat.")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            up = _check(client.post(base + "/api/upload", headers=headers, data={"session_id": session_id},
                                    files={"files": (path.name, fh, mime)}, timeout=600), "the file")
        files = up.get("files") or []
        if not files:
            raise BairdError("failed", "Faustus stored no file.")
        file_id = str(files[0].get("id") or "")
        _check(client.put(base + f"/api/sessions/{session_id}/draft", headers=headers, timeout=30,
                          json={"text": message, "attachment_ids": [file_id]}), "the message box")
        return {"session_id": session_id, "file_id": file_id, "faustus": base, "url": f"{base}/studio?s={session_id}",
                "file": path.name, "title": title}
    except httpx.HTTPError as exc:
        raise BairdError("unavailable", f"Faustus did not answer: {type(exc).__name__}.", "Check that Faustus is running.") from exc
    finally:
        if own:
            client.close()
