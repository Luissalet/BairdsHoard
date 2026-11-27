"""``/copilot``: the live guidance an assistant draws over the picture (``copilot.py``).

``GET /copilot`` is a page of plain text for an assistant: what this is, where things stand and how to drive it, so
that handing a chat this one address is enough. Under it, the same things the ``copilot_*`` tools do, for assistants
that only have HTTP (bodies are JSON; text that arrives in the Windows code page instead of UTF-8 is read too), and
what Baird's own page uses: the state, its WebSocket and the person's buttons.

(``/api/copilot`` is something else: the Faustus chat panel that every Hoard page carries.)"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import Any, Callable

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..agent_sources import call_on_source
from ..copilot import brief
from ..errors import BairdError
from .deps import services


class Utf8Json(JSONResponse):
    """JSON that says it is UTF-8. Windows PowerShell 5.1 reads an unlabelled answer as Latin-1, and an assistant
    working from there would get every accent of the conversation garbled."""
    media_type = "application/json; charset=utf-8"


router = APIRouter(default_response_class=Utf8Json)
NO_STORE = {"Cache-Control": "no-store"}


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")      # a console that did not send UTF-8
    try:
        data = json.loads(text)
    except ValueError:
        raise BairdError("invalid", "The body must be JSON.", 'Example: {"steps": [{"say": "Pulsa Guardar", "click": "Guardar"}], "wait_s": 120}') from None
    if not isinstance(data, dict):
        raise BairdError("invalid", "The body must be a JSON object.")
    return data


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else 0.0        # NaN and infinity are no time to wait at all


async def _patiently(request: Request, call: Callable[[threading.Event], dict[str, Any]]) -> dict[str, Any]:
    """Run a call that may wait for the person in a worker thread, and tell it when the caller hung up: what it was
    waiting for then stays for the next call instead of going into an answer nobody reads."""
    gone = threading.Event()
    task = asyncio.ensure_future(run_in_threadpool(call, gone))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                return task.result()
            if not gone.is_set() and await request.is_disconnected():
                gone.set()
    except asyncio.CancelledError:
        gone.set()
        raise


def _with_look_url(request: Request, out: dict[str, Any]) -> dict[str, Any]:
    """Over HTTP the picture does not travel inside the JSON: the answer says where to GET it."""
    if out.pop("_image", None) is not None and out.get("look"):
        out["look_url"] = f"{str(request.base_url).rstrip('/')}/copilot/look/{out['look']}.jpg"
    return out


def _look_mode(value: Any) -> str:
    if value is True:
        return "always"
    if value is False:
        return "never"
    return value if value in ("auto", "always", "never") else "auto"


def _live(request: Request, call):
    svc = services(request)
    return call_on_source(svc, None, lambda: call(svc.copilot))


@router.get("/copilot")
def copilot_brief(request: Request):
    svc = services(request)
    text = brief(str(request.base_url).rstrip("/"), svc.copilot, svc.engine.snapshot())
    return PlainTextResponse(text, headers=NO_STORE)


@router.get("/copilot/state")
def copilot_state(request: Request):
    return services(request).copilot.state()


@router.get("/copilot/status")
def copilot_status(request: Request):
    return services(request).copilot.status()


@router.post("/copilot/show")
@router.post("/copilot/step")
@router.post("/copilot/plan")
@router.post("/copilot/say")
async def copilot_show(request: Request):
    body = await _body(request)
    wait_s, each, look = _number(body.get("wait_s"), 0.0), body.get("each") is True, _look_mode(body.get("look"))
    out = await _patiently(request, lambda gone: _live(
        request, lambda copilot: copilot.show(body, wait_s=wait_s, each=each, look=look, gone=gone)))
    return _with_look_url(request, out)


@router.post("/copilot/wait")
async def copilot_wait_post(request: Request):
    body = await _body(request)
    wait_s, each, look = _number(body.get("wait_s"), 60.0), body.get("each") is True, _look_mode(body.get("look"))
    out = await _patiently(request, lambda gone: _live(
        request, lambda copilot: copilot.wait(wait_s, each=each, look=look, gone=gone)))
    return _with_look_url(request, out)


@router.get("/copilot/wait")
async def copilot_wait_get(request: Request, wait_s: float = 60.0, each: bool = False, look: str = "auto"):
    mode = _look_mode(look)
    out = await _patiently(request, lambda gone: _live(
        request, lambda copilot: copilot.wait(_number(wait_s, 0.0), each=each, look=mode, gone=gone)))
    return _with_look_url(request, out)


def _region(raw: str) -> Any:
    if not raw:
        return None
    try:
        return [float(v) for v in raw.replace(";", ",").split(",")]
    except ValueError:
        raise BairdError("invalid", "region must be x,y,w,h.") from None


@router.get("/copilot/look.jpg")
def copilot_look(request: Request, w: int = 0, region: str = ""):
    """The screen now as a JPEG. Its pixels are the coordinates marks are given in from now on."""
    def look(copilot):
        out = copilot.look(max_side=w or None, region=_region(region))
        return Response(copilot.look_image(out["look"]), media_type="image/jpeg",
                        headers={**NO_STORE, "X-Copilot-Look": out["look"],
                                 "X-Copilot-Space": f"{out['space'][0]}x{out['space'][1]}"})

    return _live(request, look)


@router.get("/copilot/look/{name}")
def copilot_look_kept(request: Request, name: str):
    jpeg = _live(request, lambda copilot: copilot.look_image(name.rsplit(".", 1)[0]))
    return Response(jpeg, media_type="image/jpeg", headers=NO_STORE)


@router.get("/copilot/text")
def copilot_text(request: Request, q: str = "", format: str = "text"):
    """Every text on screen as ``x,y text`` lines (or, with ``q``, where that text is). ``format=json`` for the data."""
    out = _live(request, lambda copilot: copilot.texts(q))
    if format == "json":
        return out
    space = out.get("space")
    head = f"# coordinates in a {space[0]}x{space[1]} picture" if space else "# coordinates"
    if q.strip():
        lines = [f"{m['at'][0]},{m['at'][1]} {m['text']}  (score {m['score']})" for m in out["matches"]] or ["# not on screen"]
    else:
        lines = out["lines"]
    return PlainTextResponse("\n".join([head, *lines]) + "\n", headers=NO_STORE)


@router.post("/copilot/stop")
async def copilot_stop(request: Request):
    body = await _body(request)
    return await run_in_threadpool(services(request).copilot.stop, str(body.get("message") or ""))


@router.post("/copilot/user")
async def copilot_user(request: Request):
    """The person's buttons on Baird's page: next, back, repeat, help, say, stop."""
    body = await _body(request)
    copilot = services(request).copilot
    return await run_in_threadpool(copilot.user, str(body.get("action") or ""), str(body.get("text") or ""))


@router.post("/copilot/overlay")
async def copilot_overlay(request: Request):
    body = await _body(request)
    return await run_in_threadpool(services(request).copilot.overlay_control,
                                   str(body.get("action") or ""), str(body.get("language") or ""))


@router.websocket("/copilot/ws")
async def copilot_ws(ws: WebSocket):
    """Every new state of the copilot, as JSON, for the page that draws it."""
    await ws.accept()
    copilot = ws.app.state.services.copilot
    if copilot.overlay is not None:
        copilot.overlay.set_language(ws.query_params.get("lang"))
    sub = copilot.subscribe(asyncio.get_running_loop())

    async def pump_in() -> None:
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            sub.push(None)

    reader = asyncio.create_task(pump_in())
    try:
        while True:
            item = await sub.queue.get()
            if item is None:
                break
            await ws.send_json(item)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader.cancel()
        copilot.unsubscribe(sub)
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
