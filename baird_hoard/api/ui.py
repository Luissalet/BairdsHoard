"""Routes for the bundled web UI: status, the capture buttons, the library, the files themselves, settings and the
live WebSocket. Everything the buttons do is also an agent tool (``POST /api/ui/call`` runs those handlers)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..agent_tools import TOOLS_BY_NAME
from ..errors import BairdError
from ..hoard_link import family
from ..native import list_devices
from .deps import services, tool

router = APIRouter()


class CallBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    arguments: Optional[dict[str, Any]] = None


class SnapshotBody(BaseModel):
    format: Optional[str] = None


class ClipBody(BaseModel):
    seconds: Optional[float] = Field(None, ge=1, le=600)


class RenameBody(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=200)


@router.get("/api/status")
def status(request: Request):
    return services(request).status()


@router.post("/api/snapshot")
def snapshot(request: Request, body: SnapshotBody | None = None):
    return services(request).library.snapshot((body.format if body else None) or None)


@router.post("/api/clip")
def clip(request: Request, body: ClipBody | None = None):
    return services(request).library.clip(body.seconds if body else None)


@router.post("/api/record/start")
def record_start(request: Request):
    return services(request).library.recorder.start()


@router.post("/api/record/stop")
def record_stop(request: Request):
    return services(request).library.recorder.stop()


@router.get("/api/media")
def media(request: Request, kind: str = "all", limit: int = 200):
    lib = services(request).library
    return {"items": lib.list(kind if kind in ("all", "image", "video") else "all", max(1, min(limit, 1000))),
            "folder": str(lib.folder)}


@router.get("/media/{name}")
def media_file(request: Request, name: str, download: bool = False):
    path = services(request).library.path_of(name)
    disposition = "attachment" if download else "inline"
    return FileResponse(path, headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(path.name)}",
                                       "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/api/thumb/{name}")
def thumb(request: Request, name: str):
    path = services(request).library.thumbnail(name)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.post("/api/media/{name}/copy")
def media_copy(request: Request, name: str):
    return services(request).library.copy_to_clipboard(name)


@router.post("/api/media/{name}/reveal")
def media_reveal(request: Request, name: str):
    return services(request).library.reveal(name)


@router.post("/api/media/{name}/light")
def media_light(request: Request, name: str):
    return services(request).library.light_copy(name)


@router.post("/api/media/{name}/rename")
def media_rename(request: Request, name: str, body: RenameBody):
    return services(request).library.rename(name, body.new_name)


@router.delete("/api/media/{name}")
def media_delete(request: Request, name: str):
    return services(request).library.delete(name)


@router.post("/api/folder/reveal")
def folder_reveal(request: Request):
    return services(request).library.reveal(None)


@router.get("/api/settings")
def settings_get(request: Request):
    return services(request).settings.get()


@router.put("/api/settings")
def settings_put(request: Request, body: dict[str, Any]):
    return services(request).apply_settings(body)


@router.get("/api/devices")
def devices(request: Request):
    if services(request).config.fake:
        return {"devices": ["demo"]}
    return {"devices": list_devices()}


@router.get("/api/ui/tools")
def ui_tools():
    return {"tools": sorted(TOOLS_BY_NAME)}


@router.post("/api/ui/call")
def ui_call(request: Request, body: CallBody):
    if body.name not in TOOLS_BY_NAME:
        raise BairdError("not_found", f"Unknown tool: {body.name}")
    t0 = time.monotonic()
    ok, error = False, ""
    try:
        result = tool(request, body.name, body.arguments)
        ok = True
        return result
    except Exception as exc:  # noqa: BLE001 - re-raised: the app's handlers shape the response
        error = str(exc)[:200]
        raise
    finally:
        family.record_call(body.name, ok, int((time.monotonic() - t0) * 1000), caller="ui", error=error)


@router.websocket("/api/live")
async def live(ws: WebSocket):
    """Fragmented MP4 of the live picture: a JSON ``init`` message with the MIME type, then binary init segment and
    fragments. ``{"type":"reset"}`` means a new stream follows (start a new MediaSource)."""
    await ws.accept()
    svc = ws.app.state.services
    viewer = await run_in_threadpool(svc.live.attach, asyncio.get_running_loop())

    async def pump_in() -> None:
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            viewer.push(None)

    reader = asyncio.create_task(pump_in())
    try:
        while True:
            item = await viewer.queue.get()
            if item is None:
                break
            if isinstance(item, (bytes, bytearray)):
                await ws.send_bytes(bytes(item))
            else:
                await ws.send_json(item)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader.cancel()
        svc.live.detach(viewer)
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
