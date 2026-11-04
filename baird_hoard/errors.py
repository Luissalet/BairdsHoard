"""One error type for every expected failure, so the API, the agent tools and the UI report it the same way."""

from __future__ import annotations

from typing import Any

from .hoard_link.agentkit import AppError


class BairdError(AppError):
    """An expected, explainable failure with a stable ``code``, a message and an actionable ``hint``."""

    STATUS = {
        "not_found": 404,
        "confirm_required": 400,
        "invalid": 400,
        "busy": 409,
        "conflict": 409,
        "no_signal": 409,
        "no_device": 503,
        "stuck": 503,
        "unavailable": 503,
        "failed": 500,
    }

    def __init__(self, code: str, message: str, hint: str = "", *, status: int | None = None, **details: Any):
        super().__init__(code, message, hint=hint, status=status or self.STATUS.get(code, 400), details=details)
