"""Shared helpers for the routers: the services object and a bridge to the agent tools (the UI calls the same handlers)."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..agent_tools import call_tool, uncapped
from ..services import Services


def services(request: Request) -> Services:
    return request.app.state.services


def tool(request: Request, name: str, arguments: dict[str, Any] | None = None) -> Any:
    with uncapped():
        return call_tool(services(request), name, arguments, caller="ui")
