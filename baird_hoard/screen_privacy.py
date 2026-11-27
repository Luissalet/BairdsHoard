"""Apply the family's screen privacy settings to one local display."""

from __future__ import annotations

import logging
import ntpath
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import ContextManager, Literal

log = logging.getLogger("baird.screen_privacy")

BlockReason = Literal["private", "excluded", "missing_display", "policy_unavailable"]
BLOCK_REASONS = ("private", "excluded", "missing_display", "policy_unavailable")
# The authority sends revocations scoped to a display. None signals unknown policy or a lost subscription.
ScreenPolicySubscription = Callable[[str, Callable[[BlockReason | None], None]], Callable[[], None]]


@dataclass(frozen=True)
class ScreenPolicy:
    private_mode: bool
    excluded_apps: tuple[str, ...]
    display_excluded: bool = False


def app_key(app: str) -> str:
    return ntpath.basename(app.strip()).casefold()


class ScreenGate:
    """Check the Hub before opening, during a capture session and before publishing a local picture."""

    def __init__(self, read_policy: Callable[[], ScreenPolicy], display_id: str,
                 display_present: Callable[[str], bool],
                 visible_apps: Callable[[str], Iterable[str] | None],
                 permit: Callable[[], ContextManager[bool]] | None = None):
        self._read_policy = read_policy
        self._display_id = display_id
        self._display_present = display_present
        self._visible_apps = visible_apps
        self._permit = permit
        self._lock = threading.Lock()
        self._capture_io = threading.Lock()
        self._blocked: BlockReason | None = None

    @property
    def blocked(self) -> BlockReason | None:
        with self._lock:
            return self._blocked

    def block(self, reason: BlockReason | None) -> None:
        with self._lock:
            if self._blocked is None or (self._blocked == "policy_unavailable" and reason in ("private", "excluded")):
                self._blocked = reason if reason in BLOCK_REASONS else "policy_unavailable"

    def refresh_cutoff(self) -> BlockReason | None:
        """Resolve a fail-closed cutoff after the scoped authority settles."""
        if self.blocked == "policy_unavailable":
            reason = self._read_block_reason()
            if reason in ("private", "excluded"):
                self.block(reason)
        return self.blocked

    @contextmanager
    def capture_io(self) -> Iterator[bool]:
        """Keep a local read or delivery ordered with confirmation of a cutoff."""
        with self._capture_io:
            yield self.blocked is None

    @contextmanager
    def publication(self) -> Iterator[bool]:
        """Hold the authority's current scope through a short, nonblocking commit."""
        with ExitStack() as held:
            if self.blocked is None and self._permit is not None:
                try:
                    allowed = held.enter_context(self._permit())
                except Exception:
                    allowed = False
                if allowed is not True:
                    held.close()
                    self.block(None)
                    self.refresh_cutoff()
            yield self.blocked is None

    def confirm_revocation(self, timeout: float) -> None:
        """Acknowledge after all admitted capture work has left the gate."""
        if not self._capture_io.acquire(timeout=timeout):
            raise TimeoutError("The revoked screen capture still has pending work.")
        self._capture_io.release()

    def check(self) -> BlockReason | None:
        blocked = self.blocked
        if blocked is not None:
            return blocked
        # A revocation can set the latch even when an authority read is in progress.
        reason = self._read_block_reason()
        if reason is not None:
            self.block(reason)
        return self.blocked

    def _read_block_reason(self) -> BlockReason | None:
        try:
            return self._check()
        except Exception:
            log.debug("Screen policy could not be checked", exc_info=True)
            return "policy_unavailable"

    def _check(self) -> BlockReason | None:
        policy = self._read_policy()
        if (not isinstance(policy, ScreenPolicy) or type(policy.private_mode) is not bool
                or type(policy.display_excluded) is not bool
                or not isinstance(policy.excluded_apps, tuple)
                or any(not isinstance(app, str) or not app.strip() for app in policy.excluded_apps)):
            return "policy_unavailable"
        if policy.private_mode:
            return "private"
        if policy.display_excluded:
            return "excluded"
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
