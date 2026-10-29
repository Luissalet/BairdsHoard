"""/api/agent/* - the bridge used by mcp_server.py (Bearer token from <DATA_DIR>/mcp-token)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..agent_tools import AGENT_INSTRUCTIONS, call_tool, tool_catalog
from ..hoard_link.agentkit import make_agent_router
from .deps import services


def _call(name: str, arguments: dict[str, Any], request: Request) -> Any:
    return call_tool(services(request), name, arguments)


router = APIRouter()
router.include_router(make_agent_router(tools_fn=tool_catalog, call_fn=_call, token_fn=lambda request: services(request).token,
                                        instructions=AGENT_INSTRUCTIONS, app_name="baird"))
