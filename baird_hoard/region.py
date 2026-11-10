"""A rectangle of the picture (``{x, y, w, h}``) and the ffmpeg ``crop`` filter that cuts it.

``unit="fraction"`` means 0..1 of the width and height (what the web UI sends: it does not know the device's
resolution); ``unit="px"`` is pixels of the input picture (what an assistant usually knows from ``capture_status``).
Rectangles that stick out of the picture are cut to it; one that does not touch it is refused."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .errors import BairdError

MIN_FRACTION = 0.004
MIN_PX = 4


def parse_region(raw: Any, input_size: Optional[tuple[int, int]] = None) -> Optional[dict[str, Any]]:
    """Validate ``raw`` (``None`` stays ``None``). Returns ``{x, y, w, h, unit}`` with clamped numbers."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise BairdError("invalid", "region must be an object {x, y, w, h, unit}.")
    unit = str(raw.get("unit") or "px").lower()
    if unit not in ("px", "fraction"):
        raise BairdError("invalid", "region.unit must be px or fraction.")
    try:
        x, y, w, h = (float(raw[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise BairdError("invalid", "region needs numeric x, y, w and h.") from None
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x, y, w, h)):
        raise BairdError("invalid", "region numbers must be finite.")
    if w <= 0 or h <= 0:
        raise BairdError("invalid", "region needs a positive width and height.")
    if unit == "fraction":
        full_w = full_h = 1.0
        floor = MIN_FRACTION
    else:
        full_w, full_h = (float(input_size[0]), float(input_size[1])) if input_size else (float("inf"), float("inf"))
        floor = MIN_PX
        x, y, w, h = (round(v) for v in (x, y, w, h))
    x, y = max(0.0, x), max(0.0, y)
    if x >= full_w or y >= full_h:
        raise BairdError("invalid", "region is outside the picture.")
    w, h = min(w, full_w - x), min(h, full_h - y)
    if w < floor or h < floor:
        raise BairdError("invalid", "region is too small.")
    if unit == "fraction":
        return {"x": round(x, 5), "y": round(y, 5), "w": round(w, 5), "h": round(h, 5), "unit": unit}
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(h), "unit": unit}


def crop_filter(region: dict[str, Any]) -> str:
    """The ``crop`` filter for a region from :func:`parse_region` (commas escaped for a filtergraph)."""
    x, y, w, h = region["x"], region["y"], region["w"], region["h"]
    if region["unit"] == "fraction":
        return f"crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y}"
    return f"crop=w=min({w}\\,iw-{x}):h=min({h}\\,ih-{y}):x=min({x}\\,iw-2):y=min({y}\\,ih-2)"
