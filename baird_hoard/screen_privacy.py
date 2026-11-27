"""Apply the family's screen privacy settings to one local display."""

from __future__ import annotations

import logging
import ntpath
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("baird.screen_privacy")

BlockReason = Literal["private", "excluded", "missing_display", "policy_unavailable"]
BLOCK_REASONS = ("private", "excluded", "missing_display", "policy_unavailable")
# The authority sends revocations scoped to a display. None signals unknown policy or a lost subscription.
ScreenPolicySubscription = Callable[[str, Callable[[BlockReason | None], None]], Callable[[], None]]


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
        self._transport = threading.RLock()
        self._blocked: BlockReason | None = None

    @property
    def blocked(self) -> BlockReason | None:
        with self._lock:
            return self._blocked

    def block(self, reason: BlockReason | None) -> None:
        with self._lock:
            if self._blocked is None:
                self._blocked = reason if reason in BLOCK_REASONS else "policy_unavailable"

    def read_transport(self, read: Callable[[int], bytes], size: int) -> bytes:
        """Serialize the transport call with confirmation of a cutoff."""
        with self._transport:
            if self.blocked is not None:
                return b""
            return read(size)

    def confirm_revocation(self, timeout: float) -> None:
        """Acknowledge only when no other thread can still start an authorized read."""
        if not self._transport.acquire(timeout=timeout):
            raise TimeoutError("The revoked screen transport has not stopped.")
        self._transport.release()

    def check(self) -> BlockReason | None:
        blocked = self.blocked
        if blocked is not None:
            return blocked
        # A revocation can set the latch even when an authority read is in progress.
        try:
            reason = self._check()
        except Exception:
            log.debug("Screen policy could not be checked", exc_info=True)
            reason = "policy_unavailable"
        if reason is not None:
            self.block(reason)
        return self.blocked

    def _check(self) -> BlockReason | None:
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
