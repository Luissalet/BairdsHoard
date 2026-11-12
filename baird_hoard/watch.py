"""Watch the other computer's screen and tell the person when something happens.

A rule looks at a zone of the picture (or all of it) every few seconds:

* ``change`` rules compare small grey copies of the zone: ``mode="change"`` fires when at least ``threshold`` % of
  the zone changed since the last look; ``mode="still"`` fires once when a zone that was changing stays unchanged for
  ``still_seconds`` (a long process that finished: its output stops moving).
* ``text`` rules read the zone with the family's OCR (Kafka's Hoard) and fire when ``text`` appears
  (``mode="appears"``) or disappears (``mode="disappears"``); the first look only sets the starting point.

A firing is an event: it is kept (``watch.json``), a picture of the zone is saved in the library (``Aviso ...``), the
Hub is asked to notify the person (``fam_notify``: Windows toast, ntfy, Telegram or mail as the person configured)
and ``baird.watch`` goes onto the family bus. ``cooldown_s`` keeps a rule from firing again right away."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import BairdError
from .hoard_link.atomic import read_json, write_json_atomic
from .ocr import fold
from .region import parse_region

log = logging.getLogger("baird.watch")

PIXEL_STEP = 24            # a grey level that moved by more than this counts as a changed pixel
MAX_RULES = 30
MAX_EVENTS = 200
KINDS = ("change", "text")
MODES = {"change": ("change", "still"), "text": ("appears", "disappears")}


def changed_percent(before: bytes, after: bytes) -> float:
    """Percentage of positions whose grey level moved by more than :data:`PIXEL_STEP`."""
    if not before or len(before) != len(after):
        return 0.0
    moved = sum(1 for a, b in zip(before, after) if abs(a - b) > PIXEL_STEP)
    return 100.0 * moved / len(after)


def _num(raw: Any, name: str, low: float, high: float, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BairdError("invalid", f"{name} must be a number.") from None
    if value != value or not low <= value <= high:
        raise BairdError("invalid", f"{name} must be between {low:g} and {high:g}.")
    return value


def clean_rule(spec: dict[str, Any], input_size: Optional[tuple[int, int]] = None, base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Validate a rule given by the UI or an assistant; ``base`` is the existing rule when updating."""
    rule = dict(base or {})
    rule.update({k: v for k, v in spec.items() if v is not None and k in (
        "name", "kind", "mode", "region", "threshold", "still_seconds", "text", "interval_s", "cooldown_s", "enabled", "notify",
        "save_image")})
    kind = str(rule.get("kind") or "change")
    if kind not in KINDS:
        raise BairdError("invalid", "kind must be change or text.")
    mode = str(rule.get("mode") or MODES[kind][0])
    if mode not in MODES[kind]:
        raise BairdError("invalid", f"For a {kind} rule, mode must be {' or '.join(MODES[kind])}.")
    out: dict[str, Any] = {"id": rule.get("id") or uuid.uuid4().hex[:8], "kind": kind, "mode": mode,
                           "created": rule.get("created") or time.time(),
                           "enabled": bool(rule.get("enabled", True)), "notify": bool(rule.get("notify", True)),
                           "save_image": bool(rule.get("save_image", True))}
    out["region"] = parse_region(rule.get("region"), input_size)
    if kind == "change":
        out["threshold"] = _num(rule.get("threshold"), "threshold", 0.1, 100, 5.0)
        out["still_seconds"] = _num(rule.get("still_seconds"), "still_seconds", 2, 3600, 30.0)
        out["interval_s"] = _num(rule.get("interval_s"), "interval_s", 1, 600, 2.0)
        out["text"] = ""
    else:
        text = str(rule.get("text") or "").strip()
        if not text:
            raise BairdError("invalid", "A text rule needs the text to look for.")
        out["text"] = text[:200]
        out["interval_s"] = _num(rule.get("interval_s"), "interval_s", 5, 3600, 10.0)
        out["threshold"] = 0.0
        out["still_seconds"] = 0.0
    out["cooldown_s"] = _num(rule.get("cooldown_s"), "cooldown_s", 0, 86400, 60.0)
    name = str(rule.get("name") or "").strip()
    if not name:
        name = (f"Cambio en la pantalla" if kind == "change" and mode == "change" else
                "La pantalla dejó de moverse" if kind == "change" else
                f"Aparece «{out['text']}»" if mode == "appears" else f"Desaparece «{out['text']}»")
    out["name"] = name[:80]
    return out


NotifyFn = Callable[[str, str, str], Any]       # title, body, dedupe key
EmitFn = Callable[[str, dict[str, Any]], Any]
OcrFn = Callable[[str], dict[str, Any]]


class WatchEngine:
    def __init__(self, path: Path, library: Any, engine: Any, work_dir: Path, *, ocr_fn: OcrFn, notify_fn: Optional[NotifyFn] = None,
                 emit_fn: Optional[EmitFn] = None, clock: Callable[[], float] = time.time,
                 on_demand: Optional[Callable[[bool], None]] = None):
        self.path = path
        self.library = library
        self.engine = engine
        self.work_dir = work_dir
        self.ocr_fn = ocr_fn
        self.notify_fn = notify_fn
        self.emit_fn = emit_fn
        self.clock = clock
        self.on_demand = on_demand
        self._lock = threading.RLock()
        raw = read_json(path, default={}) or {}
        self._rules: list[dict[str, Any]] = [r for r in raw.get("rules", []) if isinstance(r, dict)]
        self._events: list[dict[str, Any]] = [e for e in raw.get("events", []) if isinstance(e, dict)][-MAX_EVENTS:]
        self._run: dict[str, dict[str, Any]] = {}        # per rule, memory only
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="baird-watch", daemon=True)
        self._thread.start()
        with self._lock:
            self._sync_demand()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._sync_demand()

    def _sync_demand(self) -> None:
        if self.on_demand:
            active = bool(self._thread and not self._stop.is_set() and any(r.get("enabled") for r in self._rules))
            self.on_demand(active)

    def _save(self) -> None:
        with self._lock:
            write_json_atomic(self.path, {"rules": self._rules, "events": self._events[-MAX_EVENTS:]})
            self._sync_demand()

    # ------------------------------------------------------------------ rules

    def _input_size(self) -> Optional[tuple[int, int]]:
        try:
            return self.library.input_size()
        except Exception:  # noqa: BLE001
            return None

    def list(self) -> dict[str, Any]:
        with self._lock:
            rules = []
            for rule in self._rules:
                st = self._run.get(rule["id"], {})
                rules.append({**rule, "status": {"checked": st.get("checked"), "value": st.get("value"), "next": st.get("next"),
                                                 "last_fired": st.get("last_fired"), "error": st.get("error", ""),
                                                 "state": st.get("state", "")}})
            return {"rules": rules, "events": list(reversed(self._events[-20:])), "active": sum(1 for r in self._rules if r.get("enabled"))}

    def summary(self) -> dict[str, Any]:
        """What the status line needs: how many rules are on and the newest event (cheap, polled every second)."""
        with self._lock:
            last = self._events[-1] if self._events else None
            return {"active": sum(1 for r in self._rules if r.get("enabled")), "rules": len(self._rules),
                    "last_event": {"id": last["id"], "at": last["at"], "rule": last["rule"], "detail": last["detail"],
                                   "image": last.get("image")} if last else None}

    def add(self, spec: dict[str, Any]) -> dict[str, Any]:
        rule = clean_rule(spec, self._input_size())
        with self._lock:
            if len(self._rules) >= MAX_RULES:
                raise BairdError("invalid", f"At most {MAX_RULES} rules.", "Remove one first.")
            self._rules.append(rule)
            self._save()
        return rule

    def update(self, rule_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            old = self._find(rule_id)
            rule = clean_rule(changes, self._input_size(), base=old)
            self._rules[self._rules.index(old)] = rule
            self._run.pop(rule_id, None)
            self._save()
        return rule

    def remove(self, rule_id: str) -> dict[str, Any]:
        with self._lock:
            rule = self._find(rule_id)
            self._rules.remove(rule)
            self._run.pop(rule_id, None)
            self._save()
        return {"removed": rule_id, "name": rule["name"]}

    def _find(self, rule_id: str) -> dict[str, Any]:
        for rule in self._rules:
            if rule["id"] == rule_id:
                return rule
        raise BairdError("not_found", f"There is no watch rule {rule_id!r}.", "watch_list shows the ids.")

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._events[-max(1, limit):]))

    # ------------------------------------------------------------------ looking

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                if self.engine.snapshot().get("state") != "running":
                    continue
                now = self.clock()
                with self._lock:
                    due = [r for r in self._rules if r.get("enabled") and self._run.get(r["id"], {}).get("next", 0) <= now]
                for rule in due:
                    self.check(rule["id"], respect_enabled=True)
            except Exception:  # noqa: BLE001 - the watcher must keep going
                log.debug("watch tick failed", exc_info=True)

    def check(self, rule_id: str, respect_enabled: bool = False, *, picture: Optional[Callable[[], None]] = None) -> dict[str, Any]:
        """Look once now. Returns ``{value, fired, detail}`` (``value`` is the % changed, or whether the text is seen)."""
        with self._lock:
            rule = dict(self._find(rule_id))
            st = self._run.setdefault(rule_id, {})
        now = self.clock()
        st["next"] = now + rule["interval_s"]
        st["checked"] = now
        try:
            (picture or self.engine.require_picture)()
            result = self._look_change(rule, st, now) if rule["kind"] == "change" else self._look_text(rule, st)
            st["error"] = ""
        except BairdError as exc:
            st["error"] = f"{exc}"[:200]
            return {"value": None, "fired": False, "detail": "", "error": st["error"]}
        except Exception as exc:  # noqa: BLE001
            st["error"] = f"{type(exc).__name__}: {exc}"[:200]
            return {"value": None, "fired": False, "detail": "", "error": st["error"]}
        fired = False
        if result.get("detail"):
            fired = self._fire(rule, st, result["detail"], now)
        return {"value": result.get("value"), "fired": fired, "detail": result.get("detail", ""), "error": ""}

    def _look_change(self, rule: dict[str, Any], st: dict[str, Any], now: float) -> dict[str, Any]:
        cur = self.library.grab_gray(rule["region"])
        prev, st["prev"] = st.get("prev"), cur
        if prev is None:
            st["state"] = "looking"
            return {"value": None}
        pct = round(changed_percent(prev, cur), 1)
        st["value"] = pct
        moving = pct >= rule["threshold"]
        if rule["mode"] == "change":
            st["state"] = "changing" if moving else "still"
            return {"value": pct, "detail": f"Cambió el {pct:g} % de la zona." if moving else ""}
        # still: it must have been moving, then stay quiet for still_seconds
        if moving:
            st["was_moving"], st["quiet_since"], st["state"] = True, None, "changing"
            return {"value": pct}
        if not st.get("was_moving"):
            st["state"] = "still"
            return {"value": pct}
        st.setdefault("quiet_since", None)
        if st["quiet_since"] is None:
            st["quiet_since"] = now
        quiet = now - st["quiet_since"]
        st["state"] = f"quiet {quiet:.0f}s"
        if quiet >= rule["still_seconds"]:
            st["was_moving"], st["quiet_since"], st["state"] = False, None, "still"
            return {"value": pct, "detail": f"La zona lleva {quiet:.0f} s sin cambiar."}
        return {"value": pct}

    def _look_text(self, rule: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        dest = self.work_dir / f"watch-{rule['id']}.png"
        try:
            self.library.grab_file(rule["region"], dest)
            res = self.ocr_fn(str(dest))
        finally:
            dest.unlink(missing_ok=True)
        if not res.get("ok"):
            raise BairdError("unavailable", f"OCR not available: {res.get('error', 'no answer')}", "OCR is Kafka's Hoard.")
        seen = fold(rule["text"]) in fold(res.get("text") or "")
        before, st["seen"] = st.get("seen"), seen
        st["value"], st["state"] = seen, "visible" if seen else "not visible"
        if before is None or before == seen:
            return {"value": seen}
        if rule["mode"] == "appears" and seen:
            return {"value": seen, "detail": f"Apareció «{rule['text']}»."}
        if rule["mode"] == "disappears" and not seen:
            return {"value": seen, "detail": f"Desapareció «{rule['text']}»."}
        return {"value": seen}

    # ------------------------------------------------------------------ firing

    def _fire(self, rule: dict[str, Any], st: dict[str, Any], detail: str, now: float) -> bool:
        last = st.get("last_fired")
        if last is not None and now - last < rule["cooldown_s"]:
            return False
        st["last_fired"] = now
        event: dict[str, Any] = {"id": uuid.uuid4().hex[:10], "at": now, "rule_id": rule["id"], "rule": rule["name"],
                                 "detail": detail, "image": None, "notified": None}
        if rule.get("save_image"):
            try:
                event["image"] = self.library.snapshot(None, rule.get("region"), prefix="Aviso", origin="watch")["name"]
            except Exception as exc:  # noqa: BLE001
                event["image_error"] = str(exc)[:120]
        with self._lock:
            self._events.append(event)
            self._save()
        if self.emit_fn:
            try:
                self.emit_fn("baird.watch", {"rule": rule["name"], "detail": detail, "image": event["image"], "event": event["id"]})
            except Exception:  # noqa: BLE001
                log.debug("bus emit failed", exc_info=True)
        if rule.get("notify") and self.notify_fn:
            threading.Thread(target=self._notify, args=(rule, event), name="baird-watch-notify", daemon=True).start()
        return True

    def _notify(self, rule: dict[str, Any], event: dict[str, Any]) -> None:
        try:
            res = self.notify_fn(f"Baird: {rule['name']}", event["detail"], f"baird:{rule['id']}:{int(event['at'] // max(1, rule['cooldown_s']))}")
            ok = bool(res.get("ok")) if isinstance(res, dict) else bool(res)
            event["notified"] = ok if ok else (str(res.get("error") or "not delivered")[:120] if isinstance(res, dict) else "not delivered")
        except Exception as exc:  # noqa: BLE001
            event["notified"] = f"{type(exc).__name__}"
        self._save()
