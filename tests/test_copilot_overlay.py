"""The overlay uses synthetic geometry and window doubles throughout."""

import sys
import time
from ctypes import c_void_p
from ctypes import wintypes
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from baird_hoard.config import Config
from baird_hoard.hotkeys import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT
from baird_hoard.main import create_app
from baird_hoard.monitor_source import MonitorSource
from baird_hoard import overlay_windows as win
from baird_hoard.overlay import CopilotOverlay, MemoryOverlay, OverlayDisplay, selected_overlay_display
from baird_hoard.overlay_windows import (
    HTTRANSPARENT,
    MA_NOACTIVATE,
    SWP_HIDEWINDOW,
    SWP_NOACTIVATE,
    SWP_SHOWWINDOW,
    SW_HIDE,
    SW_SHOWNOACTIVATE,
    WDA_EXCLUDEFROMCAPTURE,
    WDA_NONE,
    WM_MOUSEACTIVATE,
    WM_NCHITTEST,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    WS_POPUP,
    Win32Overlay,
    Win32Window,
)
from baird_hoard.services import Services

from .test_monitor_source import MONITOR, choose, confirm_test_shortcut, set_local_capture

DISPLAY = OverlayDisplay("test-secondary", -1920, -320, 1920, 1080)
SOURCE = {"mode": "overlay", "active": True, "blocked": None, "monitor": DISPLAY.id,
          "monitors": [{"id": DISPLAY.id, "left": DISPLAY.left, "top": DISPLAY.top,
                        "width": DISPLAY.width, "height": DISPLAY.height}]}
STEP = {"say": "Escribe el código", "hint": "Conserva los espacios.", "keys": "Ctrl+S", "until": "manual",
        "marks": [{"click": [208, 168]}, {"dbl": [1280, 720]}, {"right": [640, 360]},
                  {"type": "ejemplo", "at": [192, 60, 384, 72]},
                  {"box": [100, 120, 200, 60]},
                  {"arrow": [[208, 168], [1070, 554]]}, {"note": "Lee aquí", "at": [687, 554]}]}
GUIDE = {"agent": "Ana", "space": [1280, 720], "message": "Vamos a empezar.", "steps": [STEP]}


def wait_for(read, accepts=bool):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = read()
        if accepts(result):
            return result
        time.sleep(0.01)
    pytest.fail(f"Timed out on {result!r}")


@pytest.fixture
def synthetic_app(tmp_path, monkeypatch):
    def no_window():
        pytest.fail("The test-mode service tried to create a window")

    cfg = Config(data_dir=tmp_path / "data", port=17643, synthetic_capture=True, background=False,
                 isolated_instance=True, data_dir_configured=True)
    monkeypatch.setattr(CopilotOverlay, "_windows", staticmethod(no_window))
    svc = Services(cfg)
    svc.settings.update({"capture_enabled": True})
    svc.sources = SimpleNamespace(status=lambda: source.copy())
    source = {"mode": "hdmi", "active": True, "blocked": None, "monitor": None, "monitors": []}
    svc.copilot.frames = object()
    svc.copilot.background = False
    monkeypatch.setattr(svc.engine, "set_demand", Mock())
    svc.engine.state["state"] = "running"
    svc.copilot.source_changed(True)
    app = create_app(cfg, svc)
    with TestClient(app, base_url="http://127.0.0.1:17643", client=("127.0.0.1", 44444)) as client:
        yield client, svc, source


def agent(client, svc, name, **arguments):
    response = client.post("/api/agent/call", headers={"Authorization": f"Bearer {svc.token}"},
                           json={"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()


def test_fixed_overlay_profile_shares_the_monitor_permission_and_validates_layout(tmp_path, monkeypatch):
    inventory = [MONITOR]

    def unexpected_open(*args):
        pytest.fail("geometry and authorization must not open a capture device")

    config = Config(data_dir=tmp_path, port=17644, monitor_capture=True, overlay_mode=True,
                    isolated_instance=True, background=False)
    source = MonitorSource("synthetic-encoder", config.work_dir, lambda: inventory, unexpected_open)
    svc = Services(config, monitor_source=source, confirm_capture=confirm_test_shortcut)
    monkeypatch.setattr(svc.engine, "set_demand", Mock())
    with TestClient(create_app(config, svc), base_url="http://127.0.0.1:17644", client=("127.0.0.1", 44444)) as client:
        assert agent(client, svc, "source_list")["modes"] == ["overlay"]
        result = agent(client, svc, "source_select", mode="overlay", monitor=MONITOR.id)
        assert not result["active"] and svc._overlay_display() is None
        set_local_capture(client, True)
        assert agent(client, svc, "source_list")["active"]
        svc.engine.state["state"] = "running"
        geometry = OverlayDisplay(MONITOR.id, MONITOR.x, MONITOR.y, MONITOR.width, MONITOR.height)
        assert svc._overlay_display() == geometry
        inventory[0] = replace(MONITOR, x=MONITOR.x - 100)
        assert svc._overlay_display() is None
        inventory[0] = MONITOR
        set_local_capture(client, False)
        assert svc._overlay_display() is None and not agent(client, svc, "source_list")["active"]
    assert svc.engine._proc is None


def test_uses_only_the_selected_active_overlay_display():
    assert selected_overlay_display(SOURCE) == DISPLAY
    for changes in ({"active": False}, {"mode": "hdmi"}, {"mode": "monitor"}, {"blocked": "private"},
                    {"monitor": "missing"}, {"monitors": SOURCE["monitors"] * 2}):
        assert selected_overlay_display({**SOURCE, **changes}) is None


@pytest.mark.parametrize("dpi", [96, 120, 144])
def test_draw_buffer_maps_the_same_capture_to_physical_pixels_at_each_scale(synthetic_app, dpi):
    client, svc, _ = synthetic_app
    agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
    session = client.get("/copilot/state").json()["session"]
    buffer = MemoryOverlay(dpi)
    buffer.present(DISPLAY, session, "en", True)
    frame = buffer.frame
    assert frame.display.bounds == (-1920, -320, 0, 760)
    assert frame.pixels(16) == {96: 16, 120: 20, 144: 24}[dpi]
    marks = {mark.kind: mark for mark in frame.marks}
    assert set(marks) == {"click", "dbl", "right", "type", "box", "arrow", "note"}
    assert marks["click"].at == (312, 252)
    assert (DISPLAY.left + marks["click"].at[0], DISPLAY.top + marks["click"].at[1]) == (-1608, -68)
    assert marks["dbl"].at == (1919, 1079) and marks["dbl"].text == "Double-click"
    assert marks["arrow"].to == (1605, 831)
    assert marks["type"].box == (288, 90, 864, 198)
    assert frame.instruction == STEP["say"] and frame.hint == STEP["hint"]
    assert frame.heading == "1/1  ·  Ana" and frame.keys == ("Ctrl + S", "ejemplo")
    assert frame.conversation == ("Ana · Vamos a empezar.",)
    assert buffer.exclude_capture and buffer.shortcut is False


def test_windows_contract_and_shortcut_use_only_the_api_double(synthetic_app):
    client, svc, _ = synthetic_app
    agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
    session = client.get("/copilot/state").json()["session"]
    window = Mock()
    window.dpi.return_value = 120
    window.events.return_value = set()
    overlay = Win32Overlay(window)
    overlay.present(DISPLAY, session, "es", True)
    window.open.assert_called_once_with((-1920, -320, 0, 760), WS_POPUP,
                                        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE |
                                        WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    window.set_affinity.assert_called_once_with(WDA_EXCLUDEFROMCAPTURE)
    assert window.paint.call_args.args[0].dpi == 120
    window.show.assert_called_once_with(SW_SHOWNOACTIVATE)
    assert [call[0] for call in window.method_calls].index("set_affinity") < [
        call[0] for call in window.method_calls].index("show")
    window.register_hotkey.assert_called_once_with(1, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, 0x79)
    window.events.return_value = {"dpi"}
    window.dpi.return_value = 144
    assert overlay.pump() is False and window.paint.call_args.args[0].dpi == 144
    overlay.present(DISPLAY, session, "es", False)
    assert window.set_affinity.call_args.args == (WDA_NONE,)
    window.events.return_value = {"hide"}
    assert overlay.pump() is True
    assert window.show.call_args.args == (SW_HIDE,)
    overlay.close()
    window.unregister_hotkey.assert_called_once_with(1)

    native = object.__new__(Win32Window)
    native._set_pos = Mock(return_value=True)
    native.hwnd = 1
    assert native._handle(1, WM_NCHITTEST, 0, 0) == HTTRANSPARENT
    assert native._handle(1, WM_MOUSEACTIVATE, 0, 0) == MA_NOACTIVATE
    native.show(SW_SHOWNOACTIVATE)
    args = native._set_pos.call_args.args
    assert args[1].value == c_void_p(-1).value
    assert args[-1] & (SWP_SHOWWINDOW | SWP_NOACTIVATE) == SWP_SHOWWINDOW | SWP_NOACTIVATE
    native.show(SW_HIDE)
    assert native._set_pos.call_args.args[-1] & SWP_HIDEWINDOW


def test_affinity_failure_never_shows_the_window(synthetic_app):
    client, svc, _ = synthetic_app
    agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
    session = client.get("/copilot/state").json()["session"]
    window = Mock()
    window.set_affinity.side_effect = OSError("Capture exclusion unavailable")
    overlay = Win32Overlay(window)
    with pytest.raises(OSError, match="Capture exclusion unavailable"):
        overlay.present(DISPLAY, session, "es", True)
    window.show.assert_not_called()
    window.paint.assert_not_called()
    overlay.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes types")
def test_ctypes_passes_physical_bounds_and_affinity_to_a_doubled_user32(monkeypatch):
    dlls = {name: Mock() for name in ("user32", "gdi32", "kernel32")}
    monkeypatch.setattr(win.ct, "WinDLL", lambda name, **kwargs: dlls[name])
    monkeypatch.setattr(win.sys, "getwindowsversion", lambda: SimpleNamespace(build=19041))
    u = dlls["user32"]
    dlls["kernel32"].GetModuleHandleW.return_value = 100
    u.SetThreadDpiAwarenessContext.return_value = 42
    u.MonitorFromRect.return_value = 61
    u.RegisterClassW.return_value = 101
    u.CreateWindowExW.return_value = 500
    u.GetMonitorInfoW.side_effect = lambda monitor, info: setattr(info._obj, "rect", wintypes.RECT(*DISPLAY.bounds)) or True
    u.GetWindowDisplayAffinity.side_effect = lambda hwnd, affinity: setattr(affinity._obj, "value", 0x11) or True

    window = Win32Window()
    window.open(DISPLAY.bounds, WS_POPUP, win.OVERLAY_STYLES)
    actual = u.CreateWindowExW.call_args.args
    assert actual[0] == win.OVERLAY_STYLES
    assert actual[3:8] == (WS_POPUP, -1920, -320, 1920, 1080)
    assert u.SetThreadDpiAwarenessContext.call_args.args[0].value == c_void_p(-4).value
    u.SetLayeredWindowAttributes.assert_called_once_with(500, win.CLEAR, 0, win.LWA_COLORKEY)
    window.set_affinity(WDA_EXCLUDEFROMCAPTURE)
    u.SetWindowDisplayAffinity.assert_called_once_with(500, 0x11)
    u.GetWindowDisplayAffinity.assert_called_once()
    window.show(SW_SHOWNOACTIVATE)
    assert u.SetWindowPos.call_args.args[-1] & SWP_NOACTIVATE
    window.close()
    u.DestroyWindow.assert_called_once_with(500)


def test_same_agent_tools_show_guide_and_conversation_in_each_source(synthetic_app, monkeypatch):
    client, svc, source = synthetic_app
    controller = svc.copilot.overlay
    buffer = MemoryOverlay(144)
    # Stays in the normal Services test-mode presenter while allowing inspection of its frame.
    monkeypatch.setattr(controller, "_presenter", lambda: buffer)
    source.update(SOURCE)
    for mode in ("hdmi", "monitor", "overlay"):
        source["mode"] = mode
        agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
        page_state = client.get("/copilot/state").json()
        state = page_state["session"]
        assert state["step"]["marks"][0]["x"] == pytest.approx(208 / 1280)
        assert state["step"]["say"] == STEP["say"]
        client.post("/copilot/user", json={"action": "say", "text": "Ya lo veo."}).raise_for_status()
        assert client.get("/copilot/state").json()["session"]["log"][-1]["text"] == "Ya lo veo."
        if mode == "overlay":
            wait_for(controller.status, lambda status: status["visible"])
            wait_for(lambda: buffer.frame and "Tú · Ya lo veo." in buffer.frame.conversation)
        else:
            assert "overlay" not in page_state
            wait_for(controller.status, lambda status: not status["visible"])
        agent(client, svc, "copilot_stop")
        wait_for(controller.status, lambda status: not status["visible"])
    assert buffer.frame is None


def test_hiding_reshowing_and_ending_the_overlay(synthetic_app, monkeypatch):
    client, svc, source = synthetic_app
    controller = svc.copilot.overlay
    buffer = MemoryOverlay()
    presses = iter([True])

    def pump():
        return next(presses, False)

    monkeypatch.setattr(buffer, "pump", pump)
    monkeypatch.setattr(controller, "_presenter", lambda: buffer)
    source.update(SOURCE)
    with client.websocket_connect("ws://127.0.0.1:17643/copilot/ws?lang=en") as socket:
        socket.receive_json()
        agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
        wait_for(controller.status, lambda status: status["hidden"] and not status["visible"])
        assert buffer.frame is None
        statuses = [socket.receive_json().get("overlay", {}) for _ in range(2)]
        assert any(status.get("hidden") for status in statuses)
        client.post("/copilot/overlay", json={"action": "show", "language": "en"}).raise_for_status()
        wait_for(controller.status, lambda status: status["visible"])
        assert buffer.exclude_capture
        assert buffer.frame.mode == "Select Done in Baird."
        client.post("/copilot/overlay", json={"action": "include"}).raise_for_status()
        wait_for(lambda: buffer.exclude_capture, lambda value: value is False)
        client.post("/copilot/overlay", json={"action": "hide"}).raise_for_status()
        wait_for(controller.status, lambda status: status["hidden"] and not status["visible"])
        assert buffer.frame is None and not controller.status()["included"]
        client.post("/copilot/overlay", json={"action": "show"}).raise_for_status()
        wait_for(controller.status, lambda status: status["visible"])
        assert buffer.exclude_capture
        client.post("/copilot/user", json={"action": "next"}).raise_for_status()
        wait_for(controller.status, lambda status: not status["visible"])
        assert buffer.frame is None and not controller.status()["available"]


@pytest.mark.parametrize("coalesce", [False, True], ids=["presenter-sees-completion", "coalesced-completion"])
def test_capture_opt_in_ends_with_the_guide_even_if_the_session_is_reused(synthetic_app, monkeypatch, coalesce):
    client, svc, source = synthetic_app
    controller = svc.copilot.overlay
    buffer = MemoryOverlay()
    freeze, paused, release = Event(), Event(), Event()

    def pump():
        if freeze.is_set() and not paused.is_set():
            paused.set()
            assert release.wait(5), "The test did not release the presenter"
        return False

    monkeypatch.setattr(buffer, "pump", pump)
    monkeypatch.setattr(controller, "_presenter", lambda: buffer)
    source.update(SOURCE)
    agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
    wait_for(controller.status, lambda status: status["visible"])
    client.post("/copilot/overlay", json={"action": "include"}).raise_for_status()
    wait_for(lambda: not buffer.exclude_capture)
    initial = client.get("/copilot/state").json()["session"]
    next_step = {**STEP, "say": "Nueva guía después del último paso"}

    with client.websocket_connect("ws://127.0.0.1:17643/copilot/ws") as socket:
        assert socket.receive_json()["overlay"]["included"]
        if coalesce:
            freeze.set()
        try:
            if coalesce:
                assert paused.wait(4)
            done = client.post("/copilot/user", json={"action": "next"}).raise_for_status().json()
            pushed = socket.receive_json()
            assert done["session"]["status"] == pushed["session"]["status"] == "done"
            if not coalesce:
                wait_for(lambda: buffer.frame is None)
            else:
                assert buffer.frame.instruction == STEP["say"] and not buffer.exclude_capture
                late_include = client.post("/copilot/overlay", json={"action": "include"}).raise_for_status().json()
            agent(client, svc, "copilot_show", **{**GUIDE, "steps": [next_step]}, wait_s=0)
            next_session = client.get("/copilot/state").json()["session"]
        finally:
            release.set()

    assert next_session["status"] == "active" and next_session["id"] == initial["id"]
    wait_for(lambda: buffer.frame and buffer.frame.instruction == next_step["say"])
    assert buffer.exclude_capture
    assert not controller.status()["included"]
    assert not done.get("overlay", {}).get("included", False)
    assert not pushed.get("overlay", {}).get("included", False)
    if coalesce:
        assert not late_include.get("overlay", {}).get("included", False)


def test_disappears_when_source_is_lost_moved_or_closed(synthetic_app, monkeypatch):
    client, svc, source = synthetic_app
    controller = svc.copilot.overlay
    buffer = MemoryOverlay()
    monkeypatch.setattr(controller, "_presenter", lambda: buffer)
    source.update(SOURCE)
    agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
    wait_for(controller.status, lambda status: status["visible"])
    svc.copilot.source_changed(False)
    wait_for(controller.status, lambda status: not status["visible"])
    svc.copilot.source_changed(True)
    wait_for(controller.status, lambda status: status["visible"])
    source["monitors"] = [{**item, "left": 0} for item in SOURCE["monitors"]]
    wait_for(controller.status, lambda status: not status["visible"])
    assert buffer.frame is None
    agent(client, svc, "copilot_stop")
    agent(client, svc, "copilot_show", **GUIDE, wait_s=0)
    wait_for(controller.status, lambda status: status["visible"])
    svc.copilot.close()
    assert buffer.frame is None and not controller.status()["visible"]
