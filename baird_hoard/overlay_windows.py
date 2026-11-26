"""Windows presenter. No Win32 calls occur until the controller presents a frame."""

from __future__ import annotations

import ctypes as ct
import math
import sys
from contextlib import contextmanager
from ctypes import wintypes as wt

from .hotkeys import MOD_NOREPEAT, parse_hotkey
from .overlay import HIDE_SHORTCUT, OverlayDisplay, OverlayFrame, render_frame

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
OVERLAY_STYLES = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
WDA_EXCLUDEFROMCAPTURE = 0x11
WDA_NONE = 0
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x40, 0x80
LWA_COLORKEY = 1

WM_PAINT, WM_ERASEBKGND = 0x000F, 0x0014
WM_DISPLAYCHANGE, WM_NCHITTEST, WM_MOUSEACTIVATE = 0x007E, 0x0084, 0x0021
WM_DPICHANGED, WM_HOTKEY = 0x02E0, 0x0312
HTTRANSPARENT, MA_NOACTIVATE = -1, 3
HOTKEY_ID = 1


def color(rgb: int) -> int:
    return ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | (rgb >> 16)


CLEAR = color(0xFF00FF)
WHITE = color(0xFFFFFF)
HUD = color(0x0E1116)
RED = color(0xFF3B30)
MUTED = color(0xE2C9CB)


class Win32Overlay:
    """Own the window and shortcut on a single thread. The window API can be doubled."""

    def __init__(self, window=None):
        self.window = window if window is not None else Win32Window()
        self.display = None
        self.affinity = None
        self.args = None
        self.shortcut = False

    def present(self, display: OverlayDisplay, session, language: str, exclude_capture: bool) -> None:
        opened = self.display != display
        if opened:
            self.close()
            self.window.open(display.bounds, WS_POPUP, OVERLAY_STYLES)
            self.display = display
        affinity = WDA_EXCLUDEFROMCAPTURE if exclude_capture else WDA_NONE
        if affinity != self.affinity:
            if not opened:
                self.window.show(SW_HIDE)
            self.window.set_affinity(affinity)
        self.window.paint(render_frame(display, self.window.dpi(), session, language))
        if opened or affinity != self.affinity:
            self.window.show(SW_SHOWNOACTIVATE)
        if opened:
            mods, vk = parse_hotkey(HIDE_SHORTCUT)
            self.shortcut = self.window.register_hotkey(HOTKEY_ID, mods | MOD_NOREPEAT, vk)
        self.affinity = affinity
        self.args = display, session, language, exclude_capture

    def pump(self) -> bool:
        events = self.window.events()
        if "hide" in events:
            self.window.show(SW_HIDE)
            return True
        if "layout" in events:
            self.window.show(SW_HIDE)
            raise OSError("The display layout changed while the overlay was visible.")
        if "dpi" in events:
            self.present(*self.args)
        return False

    def close(self) -> None:
        if self.shortcut:
            self.window.unregister_hotkey(HOTKEY_ID)
        self.window.close()
        self.display = self.affinity = self.args = None
        self.shortcut = False


class PaintStruct(ct.Structure):
    _fields_ = [("dc", wt.HDC), ("erase", wt.BOOL), ("rect", wt.RECT), ("restore", wt.BOOL),
                ("update", wt.BOOL), ("reserved", wt.BYTE * 32)]


class MonitorInfo(ct.Structure):
    _fields_ = [("size", wt.DWORD), ("rect", wt.RECT), ("work", wt.RECT), ("flags", wt.DWORD)]


def bind(dll, name, restype, *args):
    func = getattr(dll, name)
    func.argtypes = args
    func.restype = restype
    return func


def checked(result):
    if not result:
        raise ct.WinError(ct.get_last_error())
    return result


class Win32Window:
    def __init__(self):
        if sys.platform != "win32" or sys.getwindowsversion().build < 19041:
            raise OSError("Monitor overlays require Windows 10 version 2004 or later.")
        u = ct.WinDLL("user32", use_last_error=True)
        g = ct.WinDLL("gdi32", use_last_error=True)
        k = ct.WinDLL("kernel32", use_last_error=True)
        proc = ct.WINFUNCTYPE(ct.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WindowClass(ct.Structure):
            _fields_ = [("style", wt.UINT), ("procedure", proc), ("cls_extra", ct.c_int), ("wnd_extra", ct.c_int),
                        ("instance", wt.HINSTANCE), ("icon", wt.HICON), ("cursor", wt.HANDLE), ("brush", wt.HBRUSH),
                        ("menu", wt.LPCWSTR), ("name", wt.LPCWSTR)]

        self._proc = proc(self._handle)
        self._instance = bind(k, "GetModuleHandleW", wt.HINSTANCE, wt.LPCWSTR)(None)
        self._class_name = f"BairdCopilotOverlay_{id(self):x}"
        self._class = WindowClass(procedure=self._proc, instance=self._instance, name=self._class_name)
        self._registered = False
        self.hwnd = None
        self._frame = None
        self._error = None
        self._events = set()

        self._dpi_context = bind(u, "SetThreadDpiAwarenessContext", wt.HANDLE, wt.HANDLE)
        self._monitor_from_rect = bind(u, "MonitorFromRect", wt.HANDLE, ct.POINTER(wt.RECT), wt.DWORD)
        self._monitor_info = bind(u, "GetMonitorInfoW", wt.BOOL, wt.HANDLE, ct.POINTER(MonitorInfo))
        self._register_class = bind(u, "RegisterClassW", wt.ATOM, ct.POINTER(WindowClass))
        self._unregister_class = bind(u, "UnregisterClassW", wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
        self._create = bind(u, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                            ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
        self._layer = bind(u, "SetLayeredWindowAttributes", wt.BOOL, wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD)
        self._affinity = bind(u, "SetWindowDisplayAffinity", wt.BOOL, wt.HWND, wt.DWORD)
        self._read_affinity = bind(u, "GetWindowDisplayAffinity", wt.BOOL, wt.HWND, ct.POINTER(wt.DWORD))
        self._set_pos = bind(u, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ct.c_int, ct.c_int,
                             ct.c_int, ct.c_int, wt.UINT)
        self._destroy = bind(u, "DestroyWindow", wt.BOOL, wt.HWND)
        self._dpi = bind(u, "GetDpiForWindow", wt.UINT, wt.HWND)
        self._invalidate = bind(u, "InvalidateRect", wt.BOOL, wt.HWND, ct.POINTER(wt.RECT), wt.BOOL)
        self._update = bind(u, "UpdateWindow", wt.BOOL, wt.HWND)
        self._begin = bind(u, "BeginPaint", wt.HDC, wt.HWND, ct.POINTER(PaintStruct))
        self._end = bind(u, "EndPaint", wt.BOOL, wt.HWND, ct.POINTER(PaintStruct))
        self._default = bind(u, "DefWindowProcW", ct.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        self._peek = bind(u, "PeekMessageW", wt.BOOL, ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
        self._dispatch = bind(u, "DispatchMessageW", ct.c_ssize_t, ct.POINTER(wt.MSG))
        self._register = bind(u, "RegisterHotKey", wt.BOOL, wt.HWND, ct.c_int, wt.UINT, wt.UINT)
        self._unregister = bind(u, "UnregisterHotKey", wt.BOOL, wt.HWND, ct.c_int)
        self._draw = GdiDrawing(u, g)

    def open(self, bounds, style, ex_style):
        checked(self._dpi_context(ct.c_void_p(-4)))  # Per-monitor V2. Window position and size are physical pixels.
        left, top, right, bottom = bounds
        rect = wt.RECT(*bounds)
        monitor = checked(self._monitor_from_rect(ct.byref(rect), 0))
        info = MonitorInfo(size=ct.sizeof(MonitorInfo))
        checked(self._monitor_info(monitor, ct.byref(info)))
        if (info.rect.left, info.rect.top, info.rect.right, info.rect.bottom) != bounds:
            raise OSError("The captured display no longer matches the Windows desktop.")
        checked(self._register_class(ct.byref(self._class)))
        self._registered = True
        self.hwnd = checked(self._create(ex_style, self._class_name, "Baird Copilot", style, left, top,
                                         right - left, bottom - top, None, None, self._instance, None))
        checked(self._layer(self.hwnd, CLEAR, 0, LWA_COLORKEY))

    def set_affinity(self, value):
        checked(self._affinity(self.hwnd, value))
        actual = wt.DWORD()
        checked(self._read_affinity(self.hwnd, ct.byref(actual)))
        if value != actual.value:
            raise OSError("Windows did not apply the requested capture affinity.")

    def dpi(self):
        return checked(self._dpi(self.hwnd))

    def show(self, command):
        checked(self._set_visibility(self.hwnd, command))

    def _set_visibility(self, hwnd, command):
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        if command == SW_HIDE:
            return self._set_pos(hwnd, None, 0, 0, 0, 0, flags | SWP_HIDEWINDOW | SWP_NOZORDER)
        return self._set_pos(hwnd, ct.c_void_p(-1), 0, 0, 0, 0, flags | SWP_SHOWWINDOW)  # HWND_TOPMOST

    def register_hotkey(self, key, mods, vk):
        return bool(self._register(None, key, mods, vk))

    def unregister_hotkey(self, key):
        self._unregister(None, key)

    def paint(self, frame):
        self._frame = frame
        checked(self._invalidate(self.hwnd, None, False))
        self._update(self.hwnd)
        self._check_error()

    def _check_error(self):
        if self._error is not None:
            raise self._error

    def events(self):
        msg = wt.MSG()
        while self._peek(ct.byref(msg), None, 0, 0, 1):
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._events.add("hide")
            else:
                self._dispatch(ct.byref(msg))
        self._check_error()
        events, self._events = self._events, set()
        return events

    def _handle(self, hwnd, message, wparam, lparam):
        if message == WM_NCHITTEST:
            return HTTRANSPARENT
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if message == WM_ERASEBKGND:
            return 1
        if message in (WM_DISPLAYCHANGE, WM_DPICHANGED):
            self._events.add("layout" if message == WM_DISPLAYCHANGE else "dpi")
            if message == WM_DISPLAYCHANGE:
                self._set_visibility(hwnd, SW_HIDE)
            return 0
        if message == WM_PAINT:
            paint = PaintStruct()
            dc = self._begin(hwnd, ct.byref(paint))
            try:
                if dc and self._frame is not None:
                    self._draw.frame(dc, self._frame)
            except OSError as exc:
                self._error = exc
                self._set_visibility(hwnd, SW_HIDE)
            finally:
                self._end(hwnd, ct.byref(paint))
            return 0
        return self._default(hwnd, message, wparam, lparam)

    def close(self):
        if self.hwnd:
            self._set_visibility(self.hwnd, SW_HIDE)
            self._destroy(self.hwnd)
            self.hwnd = None
        if self._registered:
            self._unregister_class(self._class_name, self._instance)
            self._registered = False
        self._frame = self._error = None
        self._events.clear()


class GdiDrawing:
    def __init__(self, u, g):
        h, n, col = wt.HDC, ct.c_int, wt.COLORREF
        self.fill = bind(u, "FillRect", n, h, ct.POINTER(wt.RECT), wt.HBRUSH)
        self.text = bind(u, "DrawTextW", n, h, wt.LPCWSTR, n, ct.POINTER(wt.RECT), wt.UINT)
        self.pen = bind(g, "CreatePen", wt.HANDLE, n, n, col)
        self.brush = bind(g, "CreateSolidBrush", wt.HBRUSH, col)
        self.font = bind(g, "CreateFontW", wt.HANDLE, n, n, n, n, n, *([wt.DWORD] * 8), wt.LPCWSTR)
        self.stock = bind(g, "GetStockObject", wt.HANDLE, n)
        self.select = bind(g, "SelectObject", wt.HANDLE, h, wt.HANDLE)
        self.delete = bind(g, "DeleteObject", wt.BOOL, wt.HANDLE)
        self.rectangle = bind(g, "Rectangle", wt.BOOL, h, n, n, n, n)
        self.round_rect = bind(g, "RoundRect", wt.BOOL, h, n, n, n, n, n, n)
        self.ellipse = bind(g, "Ellipse", wt.BOOL, h, n, n, n, n)
        self.line = bind(g, "Polyline", wt.BOOL, h, ct.POINTER(wt.POINT), n)
        self.polygon = bind(g, "Polygon", wt.BOOL, h, ct.POINTER(wt.POINT), n)
        self.text_color = bind(g, "SetTextColor", col, h, col)
        self.text_background = bind(g, "SetBkMode", n, h, n)
        self.measure = bind(g, "GetTextExtentPoint32W", wt.BOOL, h, wt.LPCWSTR, n, ct.POINTER(wt.SIZE))

    @contextmanager
    def using(self, dc, obj, *, owned=True):
        checked(obj)
        try:
            previous = checked(self.select(dc, obj))
            try:
                yield
            finally:
                self.select(dc, previous)
        finally:
            if owned:
                self.delete(obj)

    def _fill(self, dc, bounds, value):
        brush = checked(self.brush(value))
        try:
            self.fill(dc, ct.byref(wt.RECT(*bounds)), brush)
        finally:
            self.delete(brush)

    def _label(self, dc, text, x, y, width, height, pad, ink=WHITE):
        bounds = wt.RECT(x + pad, y, x + width - pad, y + height)
        self.text_color(dc, ink)
        flags = 0x0004 | 0x0020 | 0x0800 | 0x8000  # centered vertically, single line, literal &, end ellipsis
        self.text(dc, text, -1, ct.byref(bounds), flags)

    def _shape(self, dc, method, bounds, line, width, fill=None, dashed=False):
        with self.using(dc, self.pen(1 if dashed else 0, width, line)):
            brush = self.stock(5) if fill is None else self.brush(fill)  # NULL_BRUSH
            with self.using(dc, brush, owned=fill is not None):
                method(dc, *bounds)

    def _marks(self, dc, frame):
        px = frame.pixels
        halo, dot, margin = px(15), px(6), px(5)
        w, h = frame.display.width, frame.display.height
        for mark in frame.marks:
            ink = MUTED if mark.lost else RED
            if mark.box:
                self._shape(dc, self.rectangle, mark.box, ink, px(2) if mark.kind != "type" else 1,
                            dashed=mark.kind == "type")
            if mark.at:
                x, y = mark.at
                if mark.kind in ("click", "dbl", "right"):
                    self._shape(dc, self.ellipse, (x - halo, y - halo, x + halo, y + halo), ink, px(2))
                if mark.kind in ("click", "dbl", "right", "arrow") or (mark.kind == "type" and mark.box is None):
                    self._shape(dc, self.ellipse, (x - dot, y - dot, x + dot, y + dot), WHITE, px(2), ink)
                if mark.kind == "arrow" and mark.to:
                    self._arrow(dc, mark.at, mark.to, ink, px(16), px(3))
            tag = mark.text
            if mark.number:
                tag = f"{mark.number}  {tag}".strip()
            if not tag:
                continue
            size = wt.SIZE()
            self.measure(dc, tag, len(tag.encode("utf-16-le")) // 2, ct.byref(size))
            width, height = min(w - 2 * margin, size.cx + px(18)), px(25)
            if mark.box:
                x, y, _, bottom = mark.box
                y = bottom + margin if (mark.kind == "type" and bottom < h - height - margin) or y < height else y - height
            else:
                x, y = mark.at
                x = x - halo - width if x > .7 * w else x + halo
                y -= height + margin
            x, y = max(margin, min(x, w - width - margin)), max(margin, min(y, h - height - margin))
            self._shape(dc, self.round_rect, (x, y, x + width, y + height, px(9), px(9)), ink, 1, HUD)
            self._label(dc, tag, x, y, width, height, px(8))

    def _arrow(self, dc, start, end, ink, tip, stroke):
        x, y = end
        angle = math.atan2(y - start[1], x - start[0])
        head = [(x, y)] + [(round(x + math.cos(angle + offset) * tip),
                           round(y + math.sin(angle + offset) * tip)) for offset in (2.6, -2.6)]
        shaft = (wt.POINT * 2)(wt.POINT(*start), wt.POINT(*end))
        with self.using(dc, self.pen(0, stroke + 2, WHITE)):
            self.line(dc, shaft, 2)
        with self.using(dc, self.pen(0, stroke, ink)), self.using(dc, self.brush(ink)):
            self.line(dc, shaft, 2)
            self.polygon(dc, (wt.POINT * 3)(*(wt.POINT(*point) for point in head)), 3)

    def frame(self, dc, frame: OverlayFrame):
        w, h = frame.display.width, frame.display.height
        px = frame.pixels
        self._fill(dc, (0, 0, w, h), CLEAR)
        self.text_background(dc, 1)  # transparent lettering over the already painted dark backgrounds
        normal = self.font(-px(14), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI")
        with self.using(dc, normal):
            self._marks(dc, frame)
            lines = [(frame.heading, MUTED), (frame.instruction, WHITE), (frame.hint, MUTED),
                     ("    ".join(frame.keys), WHITE), (frame.mode, MUTED)]
            lines += [(line, WHITE) for line in frame.conversation]
            lines = [(line, ink) for line, ink in lines if line]
            margin, padding, gap = px(14), px(10), px(4)
            width = min(w - 2 * margin, px(760))
            flags = 0x0010 | 0x0800 | 0x2000  # wrap, literal &, whole lines
            measured = []
            for line, ink in lines:
                rect = wt.RECT(0, 0, width - 2 * padding, 0)
                line_height = self.text(dc, line, -1, ct.byref(rect), flags | 0x0400)
                measured.append((line, ink, line_height))
            height = min(h - 2 * margin, sum(line_height + gap for _, _, line_height in measured) + 2 * padding)
            left = (w - width) // 2
            top = margin if frame.bar_at_top else h - margin - height
            self._shape(dc, self.round_rect, (left, top, left + width, top + height, px(16), px(16)), MUTED, 1, HUD)
            y = top + padding
            for line, ink, line_height in measured:
                bottom = min(y + line_height, top + height - padding)
                if bottom <= y:
                    break
                self.text_color(dc, ink)
                rect = wt.RECT(left + padding, y, left + width - padding, bottom)
                self.text(dc, line, -1, ct.byref(rect), flags | 0x8000)  # ellipsis if the display is unusually short
                y += line_height + gap
