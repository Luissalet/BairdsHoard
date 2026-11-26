"""Read the desktop topology without opening a capture or audio stream."""

from __future__ import annotations

import ctypes as ct
import os
from ctypes import wintypes as wt
from uuid import UUID

from .monitor_source import DesktopMonitor

NOT_FOUND = 0x887A0002


class OutputDesc(ct.Structure):
    _fields_ = [("name", wt.WCHAR * 32), ("rect", wt.RECT), ("attached", wt.BOOL),
                ("rotation", wt.UINT), ("monitor", wt.HANDLE)]


class AdapterDesc(ct.Structure):
    _fields_ = [("name", wt.WCHAR * 128), ("vendor", wt.UINT), ("device", wt.UINT),
                ("subsys", wt.UINT), ("revision", wt.UINT), ("video_memory", ct.c_size_t),
                ("system_memory", ct.c_size_t), ("shared_memory", ct.c_size_t),
                ("luid_low", wt.DWORD), ("luid_high", wt.LONG), ("flags", wt.UINT)]


class DisplayDevice(ct.Structure):
    _fields_ = [("size", wt.DWORD), ("name", wt.WCHAR * 32), ("label", wt.WCHAR * 128),
                ("flags", wt.DWORD), ("id", wt.WCHAR * 128), ("key", wt.WCHAR * 128)]


def _method(interface, slot, returns, *args):
    vtable = ct.cast(interface, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(returns, ct.c_void_p, *args)(vtable[slot])


def _release(interface):
    _method(interface, 2, wt.ULONG)(interface)


def _check(hr):
    if hr < 0:
        raise OSError(f"DXGI error 0x{hr & 0xFFFFFFFF:08x}")


def _interfaces(interface, slot):
    fetch = _method(interface, slot, wt.LONG, wt.UINT, ct.POINTER(ct.c_void_p))
    index = 0
    while True:
        item = ct.c_void_p()
        hr = fetch(interface, index, ct.byref(item))
        if hr & 0xFFFFFFFF == NOT_FOUND:
            return
        _check(hr)
        try:
            yield index, item
        finally:
            _release(item)
        index += 1


def _identity(enumerate_display, name):
    monitors = []
    index = 0
    while True:
        device = DisplayDevice()
        device.size = ct.sizeof(device)
        # EDD_GET_DEVICE_INTERFACE_NAME returns the monitor's device interface path.
        if not enumerate_display(name, index, ct.byref(device), 1):
            break
        if device.flags & 1 and device.id:
            monitors.append((device.id, device.label))
        index += 1
    # A clone can have several physical monitors behind a single DXGI output.
    return monitors[0] if len(monitors) == 1 else None


def desktop_inventory() -> list[DesktopMonitor]:
    if os.name != "nt":
        raise OSError("Monitor capture requires Windows.")
    user32 = ct.WinDLL("user32")
    enumerate_display = user32.EnumDisplayDevicesW
    enumerate_display.argtypes = [wt.LPCWSTR, wt.DWORD, ct.POINTER(DisplayDevice), wt.DWORD]
    enumerate_display.restype = wt.BOOL
    dpi = user32.SetThreadDpiAwarenessContext
    dpi.argtypes = [ct.c_void_p]
    dpi.restype = ct.c_void_p
    previous = dpi(ct.c_void_p(-4))  # Per-monitor aware V2, so coordinates are physical pixels.
    dxgi = ct.WinDLL("dxgi")
    create = dxgi.CreateDXGIFactory1
    create.argtypes = [ct.c_void_p, ct.POINTER(ct.c_void_p)]
    create.restype = wt.LONG
    iid = (ct.c_byte * 16).from_buffer_copy(UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    factory = ct.c_void_p()
    try:
        _check(create(ct.byref(iid), ct.byref(factory)))
        monitors = []
        for adapter_index, adapter in _interfaces(factory, 12):  # IDXGIFactory1::EnumAdapters1
            desc = AdapterDesc()
            _check(_method(adapter, 10, wt.LONG, ct.POINTER(AdapterDesc))(adapter, ct.byref(desc)))
            for output_index, output in _interfaces(adapter, 7):  # IDXGIAdapter::EnumOutputs
                out = OutputDesc()
                _check(_method(output, 7, wt.LONG, ct.POINTER(OutputDesc))(output, ct.byref(out)))
                if not out.attached:
                    continue
                identity = _identity(enumerate_display, out.name)
                if identity:
                    device_id, label = identity
                    rect = out.rect
                    monitors.append(DesktopMonitor(device_id, label or out.name, rect.left, rect.top,
                                                   rect.right - rect.left, rect.bottom - rect.top, out.rotation,
                                                   adapter_index, output_index, desc.vendor))
        return monitors
    finally:
        if factory:
            _release(factory)
        if previous:
            dpi(previous)
