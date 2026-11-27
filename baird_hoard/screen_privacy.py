"""Apply the family's screen privacy settings to one local display."""

from __future__ import annotations

import logging
import ntpath
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

log = logging.getLogger("baird.screen_privacy")

POLICY_REFRESH_S = 0.5


@dataclass(frozen=True)
class ScreenPolicy:
    private_mode: bool
    excluded_apps: tuple[str, ...]


def app_key(app: str) -> str:
    return ntpath.basename(app.strip()).casefold()


class ScreenGate:
    """Check the Hub before opening, during a capture session and before publishing a local picture."""

    def __init__(self, read_policy: Callable[[], ScreenPolicy], display_id: str,
                 display_present: Callable[[str], bool],
                 visible_apps: Callable[[str], Iterable[str] | None]):
        self._read_policy = read_policy
        self._display_id = display_id
        self._display_present = display_present
        self._visible_apps = visible_apps
        self._lock = threading.Lock()
        self._next_check = 0.0
        self.blocked: str | None = None

    def check(self, fresh: bool = False) -> str | None:
        with self._lock:
            if self.blocked is None and (fresh or time.monotonic() >= self._next_check):
                try:
                    self.blocked = self._check()
                except Exception:
                    log.debug("Screen policy could not be checked", exc_info=True)
                    self.blocked = "policy_unavailable"
                self._next_check = time.monotonic() + POLICY_REFRESH_S
            return self.blocked

    def _check(self) -> str | None:
        policy = self._read_policy()
        if (not isinstance(policy, ScreenPolicy) or type(policy.private_mode) is not bool
                or not isinstance(policy.excluded_apps, tuple)
                or any(not isinstance(app, str) or not app.strip() for app in policy.excluded_apps)):
            return "policy_unavailable"
        if policy.private_mode:
            return "private"
        if not self._display_present(self._display_id):
            return "missing_display"
        if policy.excluded_apps:
            visible = self._visible_apps(self._display_id)
            if visible is None:
                return "policy_unavailable"
            excluded = {app_key(app) for app in policy.excluded_apps}
            if any(app_key(app) in excluded for app in visible):
                return "excluded"
        return None
