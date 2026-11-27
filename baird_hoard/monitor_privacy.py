"""Producer-side permissions for one selected desktop."""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Callable, ContextManager, Literal, NoReturn, Protocol, TypeVar

from .errors import BairdError

Medium = Literal["video", "audio"]
T = TypeVar("T")
POLICY_TTL = 0.1
BLOCK_CODES = frozenset({"privacy_blocked", "application_excluded", "policy_unavailable"})


@dataclass(frozen=True)
class PolicySnapshot:
    revision: int
    private: bool
    excluded: bool
    audio_excluded: bool


class CapturePolicy(Protocol):
    def read(self, monitor: str, layout: str) -> PolicySnapshot | None: ...

    def lease(self, monitor: str, layout: str, revision: int, medium: Medium) -> ContextManager[bool]:
        """Hold permission for exactly one native call, serialized with policy changes.

        The authority must confirm its current revision, scope, policy and visibility.
        It may publish a cutoff only after outstanding grants return. An HTTP poll
        or an expiring positive reply alone cannot provide this contract.
        """
        ...


class AcquisitionGate:
    def __init__(self, policy: CapturePolicy | None, selection: dict[str, str], clock=time.monotonic,
                 on_block: Callable[[BairdError], None] | None = None):
        self._policy = policy
        self._selection = (selection["id"], selection["layout"])
        self._clock = clock
        self._lock = threading.RLock()
        self._current: PolicySnapshot | None = None
        self._expires = float("-inf")
        self._blocked: str | None = None
        self._on_block = on_block

    @staticmethod
    def _error(code: str) -> BairdError:
        messages = {
            "policy_unavailable": "Screen privacy could not be confirmed.",
            "privacy_blocked": "Private mode stopped screen capture.",
            "application_excluded": "A privacy exclusion stopped local capture.",
        }
        return BairdError(code, messages[code], status=503 if code == "policy_unavailable" else 409)

    def _deny(self, code: str) -> NoReturn:
        self._blocked = code
        error = self._error(code)
        if self._on_block is not None:
            self._on_block(error)
        raise error

    def acquire(self, medium: Medium, operation: Callable[[], T]) -> T:
        with self._lock, ExitStack() as held:
            if self._blocked is not None:
                raise self._error(self._blocked)
            policy = self._policy
            if policy is None:
                self._deny("policy_unavailable")
            now = self._clock()
            if now >= self._expires:
                try:
                    self._current = policy.read(*self._selection)
                except Exception:
                    self._deny("policy_unavailable")
                self._expires = now + POLICY_TTL
            state = self._current
            if (not isinstance(state, PolicySnapshot) or type(state.revision) is not int
                    or any(type(flag) is not bool for flag in (state.private, state.excluded, state.audio_excluded))
                    or self._clock() >= self._expires):
                self._deny("policy_unavailable")
            if state.private:
                self._deny("privacy_blocked")
            if state.excluded or (medium == "audio" and state.audio_excluded):
                self._deny("application_excluded")
            try:
                confirmed = held.enter_context(policy.lease(*self._selection, state.revision, medium))
            except Exception:
                self._deny("policy_unavailable")
            if confirmed is not True:
                # The cached revision was displaced. The old grant is unusable even
                # inside its TTL, and this producer cannot restart on its own.
                held.close()
                self._expires = float("-inf")
                try:
                    current = policy.read(*self._selection)
                except Exception:
                    current = None
                if isinstance(current, PolicySnapshot):
                    if current.private is True:
                        self._deny("privacy_blocked")
                    if current.excluded is True or (medium == "audio" and current.audio_excluded is True):
                        self._deny("application_excluded")
                self._deny("policy_unavailable")
            return operation()

    def preflight(self) -> None:
        self.acquire("video", lambda: None)
        self.acquire("audio", lambda: None)

    def check(self) -> None:
        with self._lock:
            if self._blocked is not None:
                raise self._error(self._blocked)
