"""Local screen capture is explicitly enabled from Baird's source bar."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..agent_tools import SourceArgs
from ..errors import BairdError
from .deps import services

router = APIRouter(prefix="/api/sources")


class SourceActive(BaseModel):
    active: bool


@router.post("/select")
def select_source(request: Request, body: SourceArgs):
    return services(request).sources.select(body.mode, body.monitor)


@router.post("/active")
def set_source_active(request: Request, body: SourceActive):
    if body.active and (request.client is None or request.client.host not in ("127.0.0.1", "::1")):
        raise BairdError("capture_unavailable", "Start local screen capture from this computer.", status=403)
    return services(request).sources.set_active(body.active)
