"""Read the desktop topology without opening a capture or audio stream."""

from __future__ import annotations

import ctypes as ct
import os
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as wt
from uuid import UUID

from .monitor_source import DesktopMonitor
from .errors import BairdError

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


class FrameInfo(ct.Structure):
    _fields_ = [("present", ct.c_int64), ("mouse", ct.c_int64), ("accumulated", wt.UINT),
                ("coalesced", wt.BOOL), ("protected", wt.BOOL), ("pointer", wt.POINT),
                ("pointer_visible", wt.BOOL), ("metadata", wt.UINT), ("pointer_shape", wt.UINT)]


class TextureDesc(ct.Structure):
    _fields_ = [(name, wt.UINT) for name in (
        "width", "height", "mip_levels", "array_size", "format", "samples", "quality",
        "usage", "bind", "cpu", "misc",
    )]


class Mapping(ct.Structure):
    _fields_ = [("data", ct.c_void_p), ("row_pitch", wt.UINT), ("depth_pitch", wt.UINT)]


def _iid(value):
    return (ct.c_byte * 16).from_buffer_copy(UUID(value).bytes_le)


def _query(interface, iid, owned):
    result = ct.c_void_p()
    query = _method(interface, 0, wt.LONG, ct.c_void_p, ct.POINTER(ct.c_void_p))
    _check(query(interface, ct.byref(_iid(iid)), ct.byref(result)))
    owned.callback(_release, result)
    return result


def _indexed(interface, slot, index, owned):
    result = ct.c_void_p()
    get = _method(interface, slot, wt.LONG, wt.UINT, ct.POINTER(ct.c_void_p))
    _check(get(interface, index, ct.byref(result)))
    owned.callback(_release, result)
    return result


def frame_size(monitor: DesktopMonitor) -> tuple[int, int]:
    return (monitor.height, monitor.width) if monitor.rotation in (2, 4) else (monitor.width, monitor.height)


@contextmanager
def desktop_frames(monitor: DesktopMonitor):
    """Open one output's duplication interface. The caller gates every pull."""
    with ExitStack() as owned:
        dxgi = ct.WinDLL("dxgi")
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [ct.c_void_p, ct.POINTER(ct.c_void_p)]
        create_factory.restype = wt.LONG
        factory = ct.c_void_p()
        iid = _iid("770aae78-f26f-4dba-a829-253c83d1b387")
        _check(create_factory(ct.byref(iid), ct.byref(factory)))
        owned.callback(_release, factory)
        adapter = _indexed(factory, 12, monitor.adapter, owned)  # EnumAdapters1
        output = _indexed(adapter, 7, monitor.output, owned)  # EnumOutputs
        output1 = _query(output, "00cddea8-939b-4b83-a340-a685226666cc", owned)

        create_device = ct.WinDLL("d3d11").D3D11CreateDevice
        pointer = ct.POINTER(ct.c_void_p)
        create_device.argtypes = [ct.c_void_p, wt.UINT, ct.c_void_p, wt.UINT, ct.c_void_p, wt.UINT,
                                  wt.UINT, pointer, ct.POINTER(wt.UINT), pointer]
        create_device.restype = wt.LONG
        device, context = ct.c_void_p(), ct.c_void_p()
        _check(create_device(adapter, 0, None, 0x20, None, 0, 7, ct.byref(device), None, ct.byref(context)))
        owned.callback(_release, device)
        owned.callback(_release, context)
        duplication = ct.c_void_p()
        _check(_method(output1, 22, wt.LONG, ct.c_void_p, pointer)(output1, device, ct.byref(duplication)))
        owned.callback(_release, duplication)

        width, height = frame_size(monitor)
        staging = ct.c_void_p()
        texture = TextureDesc(width, height, 1, 1, 87, 1, 0, 3, 0, 0x20000, 0)
        _check(_method(device, 5, wt.LONG, ct.POINTER(TextureDesc), ct.c_void_p, pointer)(
            device, ct.byref(texture), None, ct.byref(staging)
        ))
        owned.callback(_release, staging)
        acquire = _method(duplication, 8, wt.LONG, wt.UINT, ct.POINTER(FrameInfo), pointer)
        release_frame = _method(duplication, 14, wt.LONG)
        copy = _method(context, 47, None, ct.c_void_p, ct.c_void_p)
        map_texture = _method(context, 14, wt.LONG, ct.c_void_p, wt.UINT, wt.UINT, wt.UINT, ct.POINTER(Mapping))
        unmap = _method(context, 15, None, ct.c_void_p, wt.UINT)
        row = width * 4

        def pull() -> bytes | None:
            frame, resource = FrameInfo(), ct.c_void_p()
            hr = acquire(duplication, 0, ct.byref(frame), ct.byref(resource))
            if hr & 0xFFFFFFFF == 0x887A0027:  # DXGI_ERROR_WAIT_TIMEOUT
                return None
            if hr & 0xFFFFFFFF == 0x887A0026:  # DXGI_ERROR_ACCESS_LOST
                raise BairdError("monitor_reselect", "The desktop layout changed.", status=409)
            _check(hr)
            with ExitStack() as frame_owned:
                frame_owned.callback(release_frame, duplication)
                frame_owned.callback(_release, resource)
                source = _query(resource, "6f15aaf2-d208-4e89-9ab4-489535d34f9c", frame_owned)
                desc = TextureDesc()
                _method(source, 10, None, ct.POINTER(TextureDesc))(source, ct.byref(desc))
                if (desc.width, desc.height, desc.format) != (width, height, texture.format):
                    raise BairdError("monitor_reselect", "The desktop layout changed.", status=409)
                copy(context, staging, source)
                mapped = Mapping()
                _check(map_texture(context, staging, 0, 1, 0, ct.byref(mapped)))
                frame_owned.callback(unmap, context, staging, 0)
                if mapped.row_pitch == row:
                    return ct.string_at(mapped.data, row * height)
                return b"".join(ct.string_at(mapped.data + y * mapped.row_pitch, row) for y in range(height))

        yield pull
