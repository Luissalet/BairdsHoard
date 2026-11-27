"""Confirm local capture through Windows input, independently of HTTP."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from secrets import randbelow
from threading import Event
from typing import Callable

APPROVAL_KEYS = tuple(range(6, 12))
APPROVAL_SECONDS = 20
ConfirmCapture = Callable[[int, Event], bool]

_HOTKEY = 0x0312
_MODIFIERS = 0x0001 | 0x0002 | 0x0004  # Alt, Ctrl, Shift
_NO_REPEAT = 0x4000
_REMOVE = 1


def confirm_with_windows(key: int, cancelled: Event) -> bool:
    """Wait for one OS hotkey on this thread, with no window or input hook."""
    if os.name != "nt" or key not in APPROVAL_KEYS or cancelled.is_set():
        return False

    user = ctypes.WinDLL("user32", use_last_error=True)
    register = user.RegisterHotKey
    register.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    register.restype = wintypes.BOOL
    unregister = user.UnregisterHotKey
    unregister.argtypes = [wintypes.HWND, ctypes.c_int]
    unregister.restype = wintypes.BOOL
    peek = user.PeekMessageW
    peek.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    peek.restype = wintypes.BOOL

    message = wintypes.MSG()
    virtual_key = 0x70 + key - 1  # VK_F1 through VK_F11
    identifier = randbelow(0xBFFF) + 1
    peek(ctypes.byref(message), None, _HOTKEY, _HOTKEY, 0)  # Initialize the request thread's queue.
    if not register(None, identifier, _MODIFIERS | _NO_REPEAT, virtual_key):
        return False
    expected = (virtual_key << 16) | _MODIFIERS
    deadline = time.monotonic() + APPROVAL_SECONDS
    try:
        while not cancelled.is_set() and time.monotonic() < deadline:
            if (peek(ctypes.byref(message), None, _HOTKEY, _HOTKEY, _REMOVE)
                    and message.wParam == identifier and message.lParam == expected):
                return not cancelled.is_set()
            cancelled.wait(0.04)
        return False
    finally:
        unregister(None, identifier)
