"""What an assistant sends to the copilot, checked and turned into what the page draws.

A *step* is a short instruction plus *marks*; a mark has a kind (click, dbl, right, type, box, arrow, note) and a
place. A place is a point, a box or a text on screen (an *anchor*: Baird looks for it, see ``copilot.py``). Numbers
come in the assistant's own pixels (:class:`Space`: the size of the picture it looked at, and which part of the
screen that was) and leave here as fractions of the whole picture, which is what the page and the runner use.

Everything here is plain data in, plain data out; mistakes raise :class:`BairdError` ``invalid`` with a hint that
shows the right shape, because the reader of that message is an assistant."""

from __future__ import annotations

import re
from typing import Any, Optional

from .errors import BairdError

MAX_STEPS = 40
MAX_MARKS = 12
MAX_SAY = 240
MAX_TALK = 600              # a hint, or a line the assistant says to the person
MAX_TEXT = 400
KINDS = ("click", "dbl", "right", "type", "box", "arrow", "note")
ACTIONS = ("click", "dbl", "right", "arrow")        # marks the person acts on: the screen changes when they do
STEP_KEYS = ("say", "hint", "marks", "until", "timeout_s", "keys", "at", "label", *KINDS)
SIDES = ("left", "right", "above", "below")
SAY_DEFAULT = {"click": "Pulsa aquí", "dbl": "Haz doble clic aquí", "right": "Haz clic derecho aquí", "type": "Escribe esto",
               "box": "Mira aquí", "arrow": "Arrastra", "note": "", "keys": "Pulsa estas teclas"}
STEP_TIMEOUT_S = 120.0


_GARBLED = re.compile("[\u00c2\u00c3\u00e2][\u0080-\u00bf\u0152-\u0192\u02c6\u02dc\u2013-\u203a\u20ac\u2122]")


def repair(value: Any) -> Any:
    """Undo text that was UTF-8, got read as the Windows code page and was sent on like that ("Pulsa aquÃ\u00ad" for
    "Pulsa aquí"): what Windows PowerShell 5.1 does to a script saved without a byte order mark. Strings inside lists
    and objects are repaired too. Text that does not look garbled, or does not decode cleanly, is left as it is."""
    if isinstance(value, dict):
        return {key: repair(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [repair(item) for item in value]
    if not isinstance(value, str) or not _GARBLED.search(value):
        return value
    raw = bytearray()
    for ch in value:
        try:
            raw += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 255:
                return value
            raw.append(ord(ch))          # the five bytes the code page leaves undefined arrive as themselves
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return value


def _bad(message: str, hint: str = "") -> BairdError:
    return BairdError("invalid", message, hint)


def _numbers(raw: Any, count: int) -> Optional[list[float]]:
    if isinstance(raw, (list, tuple)) and len(raw) == count and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        values = [float(v) for v in raw]
        if all(v == v and abs(v) < 1e7 for v in values):
            return values
    return None


def _finite(value: Any) -> Optional[float]:
    """The number, or ``None`` for anything that is not one (NaN and infinity are not: a clamp lets them through)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) < 1e7:
        return float(value)
    return None


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 5)


class Space:
    """How an assistant's numbers map onto the picture: the size in pixels it saw (``size``) and which part of the
    picture that was (``view``, fractions; the whole picture unless it zoomed)."""

    def __init__(self, size: Optional[tuple[float, float]], view: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)):
        self.size = size
        self.view = view

    def _need(self) -> tuple[float, float]:
        if not self.size or self.size[0] <= 0 or self.size[1] <= 0:
            raise _bad("The size of the picture is not known yet, so coordinates cannot be placed.",
                       "Look first (copilot_look), pass space: [width, height], or point at a text on screen instead.")
        return self.size

    def point(self, x: float, y: float) -> tuple[float, float]:
        w, h = self._need()
        return _clamp(self.view[0] + x / w * self.view[2]), _clamp(self.view[1] + y / h * self.view[3])

    def delta(self, dx: float, dy: float) -> tuple[float, float]:
        w, h = self._need()
        return dx / w * self.view[2], dy / h * self.view[3]

    def box(self, x: float, y: float, w: float, h: float) -> list[float]:
        if w <= 0 or h <= 0:
            raise _bad("A box needs a positive width and height.")
        x0, y0 = self.point(x, y)
        x1, y1 = self.point(x + w, y + h)
        if x1 - x0 < 0.002 or y1 - y0 < 0.002:
            raise _bad("That box is outside the picture or too small.")
        return [x0, y0, round(x1 - x0, 5), round(y1 - y0, 5)]

    def px(self, fx: float, fy: float) -> list[int]:
        """A place of the picture (fractions) in the assistant's pixels."""
        if not self.size:
            return [round(fx, 4), round(fy, 4)]  # type: ignore[list-item]
        return [round((fx - self.view[0]) / self.view[2] * self.size[0]), round((fy - self.view[1]) / self.view[3] * self.size[1])]

    def px_box(self, box: list[float]) -> list[int]:
        x0, y0 = self.px(box[0], box[1])
        x1, y1 = self.px(box[0] + box[2], box[1] + box[3])
        return [x0, y0, x1 - x0, y1 - y0]


# ---------------------------------------------------------------------------------------------- what an assistant sends

def clean_where(raw: Any, space: Space) -> dict[str, Any]:
    """A place given by an assistant: ``{"point": (x, y)}``, ``{"box": [x, y, w, h]}`` (fractions of the picture) or
    ``{"anchor": {...}}`` (a text on screen that Baird looks for)."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise _bad("An empty text cannot be pointed at.")
        return {"anchor": {"text": text[:200], "nth": 0, "near": None, "dx": 0.0, "dy": 0.0, "side": ""}}
    pair = _numbers(raw, 2)
    if pair:
        return {"point": space.point(*pair)}
    quad = _numbers(raw, 4)
    if quad:
        return {"box": space.box(*quad)}
    if isinstance(raw, dict):
        text = raw.get("find", raw.get("text"))
        if isinstance(text, str) and text.strip():
            side = str(raw.get("side") or "").lower()
            if side and side not in SIDES:
                raise _bad(f"side must be one of {', '.join(SIDES)}.")
            nth = raw.get("nth") or 0
            if not isinstance(nth, int) or isinstance(nth, bool) or not 0 <= nth <= 50:
                raise _bad("nth must be a whole number from 1 (the first one in reading order).")
            near = _numbers(raw.get("near"), 2)
            shift = [0.0 if raw.get(key) is None else _finite(raw[key]) for key in ("dx", "dy")]
            if None in shift:
                raise _bad("dx and dy must be numbers (pixels to move the mark by).")
            dx, dy = space.delta(*shift) if any(shift) else (0.0, 0.0)
            return {"anchor": {"text": text.strip()[:200], "nth": nth, "near": space.point(*near) if near else None,
                               "dx": dx, "dy": dy, "side": side}}
        if all(isinstance(raw.get(k), (int, float)) for k in ("x", "y", "w", "h")):
            return {"box": space.box(raw["x"], raw["y"], raw["w"], raw["h"])}
        if all(isinstance(raw.get(k), (int, float)) for k in ("x", "y")):
            return {"point": space.point(raw["x"], raw["y"])}
    raise _bad(f"Cannot tell where {raw!r} is.",
               'Use "text on screen", [x, y], [x, y, w, h] or {"find": "text", "nth": 2, "side": "right"}.')


def clean_mark(raw: Any, space: Space) -> Optional[dict[str, Any]]:
    """One mark as the page draws it. ``None`` for a note without a place (the caller adds it to the step's hint)."""
    if isinstance(raw, str):
        raw = {"click": raw}
    if not isinstance(raw, dict):
        raise _bad(f"A mark must be an object, not {type(raw).__name__}.", 'Example: {"click": "Guardar"} or {"type": "hola", "at": [300, 220]}.')
    kind = str(raw.get("kind") or "").lower()
    where: Any = raw.get("at")
    to: Any = raw.get("to")
    text = raw.get("text", raw.get("label", ""))
    if not kind:
        short = [k for k in KINDS if k in raw]
        if len(short) != 1:
            raise _bad("A mark needs exactly one of: " + ", ".join(KINDS) + ".", 'Example: {"click": [412, 236]}.')
        kind = short[0]
        value = raw[kind]
        if kind in ("type", "note"):
            text = value
        elif kind == "arrow":
            ends = value if isinstance(value, (list, tuple)) and len(value) == 2 and _numbers(value, 2) is None else None
            if ends is None:
                raise _bad("An arrow needs its two ends.", 'Example: {"arrow": [[200, 300], [600, 300]]} or {"arrow": ["Archivo", "Papelera"]}.')
            where, to = ends
        else:
            where = value
    if kind not in KINDS:
        raise _bad(f"Unknown kind of mark: {kind!r}.", "Kinds: " + ", ".join(KINDS) + ".")
    if text is not None and (isinstance(text, bool) or not isinstance(text, (str, int, float))):
        raise _bad(f"The text of a mark must be a string, not {type(text).__name__}.", 'Example: {"type": "informe-final", "at": "Nombre"}.')
    text = str(text if text is not None else "").strip()[:MAX_TEXT]
    if kind in ("type", "note") and not text:
        raise _bad(f"A {kind} mark needs its text.")
    mark: dict[str, Any] = {"kind": kind, "x": None, "y": None, "box": None, "to": None, "text": text, "find": None, "state": "ok",
                            "_anchor": None, "_to_anchor": None, "_matches": 1}
    if where is None:
        if kind == "note":
            return None
        if kind != "type":
            raise _bad(f"A {kind} mark needs a place (at).")
        return mark
    place = clean_where(where, space)
    if "anchor" in place:
        mark.update(_anchor=place["anchor"], find=place["anchor"]["text"], state="searching")
    elif "box" in place:
        box = place["box"]
        mark.update(x=_clamp(box[0] + box[2] / 2), y=_clamp(box[1] + box[3] / 2), box=box if kind in ("box", "type") else None)
    else:
        if kind == "box":
            raise _bad("A box needs [x, y, w, h] or a text on screen.")
        mark.update(x=place["point"][0], y=place["point"][1])
    if kind == "arrow":
        if to is None:
            raise _bad("An arrow needs where it ends (to).")
        end = clean_where(to, space)
        if "anchor" in end:
            mark.update(_to_anchor=end["anchor"], state="searching")
        elif "box" in end:
            mark["to"] = [_clamp(end["box"][0] + end["box"][2] / 2), _clamp(end["box"][1] + end["box"][3] / 2)]
        else:
            mark["to"] = list(end["point"])
    return mark


def clean_keys(raw: Any) -> list[list[str]]:
    """``"Ctrl+S"`` or ``"Ctrl+K, Ctrl+S"`` (or a list of them) as key caps: ``[["Ctrl", "S"], ...]``."""
    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).replace(" then ", ",").replace(" luego ", ",").split(",")
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Mayús", "mayus": "Mayús", "mayús": "Mayús", "win": "Win",
             "cmd": "Cmd", "enter": "Intro", "intro": "Intro", "return": "Intro", "esc": "Esc", "escape": "Esc", "tab": "Tab",
             "space": "Espacio", "espacio": "Espacio", "del": "Supr", "delete": "Supr", "supr": "Supr", "backspace": "Retroceso",
             "altgr": "AltGr"}
    out: list[list[str]] = []
    for item in items:
        combo = str(item).strip()
        if not combo:
            continue
        plus = combo.endswith("++") or combo == "+"
        parts = [p.strip() for p in combo.rstrip("+").split("+") if p.strip()] + (["+"] if plus else [])
        caps = [names.get(p.lower(), p.upper() if len(p) == 1 else p[:1].upper() + p[1:]) for p in parts][:6]
        if caps:
            out.append([c[:16] for c in caps])
    return out[:4]


def clean_until(raw: Any, space: Space) -> dict[str, Any]:
    """When a step is over. ``auto`` is resolved from the step's marks (:func:`clean_step`)."""
    hint = 'Use "auto", "manual", "change", {"text": "..."}, {"gone": "..."}, {"after": seconds} or {"change": [x, y, w, h]}.'
    if raw is None or raw == "auto":
        return {"mode": "auto"}
    if raw in ("manual", "change"):
        return {"mode": raw}
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = {"after": raw}
    if not isinstance(raw, dict):
        raise _bad(f"Cannot tell when the step ends from {str(raw)[:80]!r}.", hint)
    for key in ("text", "gone"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            return {"mode": key, "text": raw[key].strip()[:200]}
    if _finite(raw.get("after")) is not None:
        return {"mode": "after", "seconds": float(min(max(raw["after"], 0.2), 3600))}
    if "change" in raw:
        out: dict[str, Any] = {"mode": "change"}
        quad = _numbers(raw["change"], 4)
        if quad:
            out["region"] = space.box(*quad)
        if _finite(raw.get("threshold")) is not None:
            out["threshold"] = min(max(float(raw["threshold"]), 0.1), 100.0) / 100.0
        return out
    if raw.get("mode") in ("auto", "manual", "change"):
        return {"mode": raw["mode"]}
    raise _bad(f"Cannot tell when the step ends from {raw!r}.", hint)


def clean_step(raw: Any, space: Space) -> dict[str, Any]:
    """One step: ``{say, hint, keys, marks, until, timeout_s}`` plus what the runner needs (``_mode``, ``_probe``)."""
    if isinstance(raw, str):
        raw = {"say": raw}
    if not isinstance(raw, dict):
        raise _bad(f"A step must be an object or a sentence, not {type(raw).__name__}.", 'Example: {"say": "Abre Archivo", "click": "Archivo"}.')
    unknown = [k for k in raw if k not in STEP_KEYS]
    if unknown:
        raise _bad(f"Unknown step field(s): {', '.join(sorted(map(str, unknown)))}.", "Fields: " + ", ".join(STEP_KEYS) + ".")
    specs = raw.get("marks") or []
    if isinstance(specs, (dict, str)):
        specs = [specs]
    if not isinstance(specs, (list, tuple)):
        raise _bad(f"marks must be a list of marks, not {type(specs).__name__}.", 'Example: "marks": [{"click": "Aceptar"}, {"box": "Nombre"}].')
    specs = list(specs)
    label = raw.get("label")
    for kind in ("click", "dbl", "right", "box", "arrow"):
        if kind in raw:
            specs.append({kind: raw[kind], **({"text": label} if label else {})})
    at_used = False
    for kind in ("type", "note"):
        if kind in raw:
            specs.append({kind: raw[kind], "at": None if at_used else raw.get("at")})
            at_used = True
    if "at" in raw and not at_used and not any(k in raw for k in ("click", "dbl", "right", "box", "arrow")):
        specs.append({"click": raw["at"], **({"text": label} if label else {})})
    if len(specs) > MAX_MARKS:
        raise _bad(f"At most {MAX_MARKS} marks in one step.", "Split it into several steps.")
    hint = str(raw.get("hint") or "").strip()
    marks: list[dict[str, Any]] = []
    for spec in specs:
        mark = clean_mark(spec, space)
        if mark is None:
            note = str(spec.get("note") or spec.get("text") or "").strip()
            hint = f"{hint} {note}".strip()
            continue
        mark["id"] = f"m{len(marks)}"
        marks.append(mark)
    keys = clean_keys(raw.get("keys"))
    say = str(raw.get("say") or "").strip()
    if not say:
        if not marks and not keys:
            raise _bad("A step needs at least a sentence (say), a mark or keys.")
        say = SAY_DEFAULT["keys"] if keys and not marks else SAY_DEFAULT[marks[0]["kind"]] if marks else ""
    until = clean_until(raw.get("until"), space)
    timeout = raw.get("timeout_s")
    timeout = float(min(max(timeout, 5), 3600)) if _finite(timeout) is not None else STEP_TIMEOUT_S
    typed = next((m["text"] for m in marks if m["kind"] == "type" and m["text"]), "")
    mode = until["mode"]
    if mode == "auto":      # the text to type shows up; the screen changes where an action was asked; else the person says
        mode = "typed" if typed else "change" if keys or any(m["kind"] in ACTIONS for m in marks) else "manual"
    return {"say": say[:MAX_SAY], "hint": hint[:MAX_TALK], "keys": keys, "marks": marks, "until": until, "timeout_s": timeout,
            "_mode": mode, "_probe": typed[:48]}


def public_mark(mark: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mark.items() if not k.startswith("_")}
