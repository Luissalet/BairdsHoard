"""The GPU enhancement: settings, the filter chain, the live stage, enhanced clips and recordings, enhanced copies and
the tools. The tests run the chain on the CPU (no Vulkan, no NVENC) so they never touch a GPU."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from baird_hoard.enhance import (DEFAULT_ENHANCE, Enhancer, build_chain, effects_on, normalize_enhance, only)

from .conftest import FFMPEG

CPU_CAPS = {"ffmpeg": True, "gpu": 2, "vulkan": False, "nvenc": False, "nvenc_hevc": False,
            "filters": {"libplacebo": False, "nlmeans_vulkan": False, "hqdn3d": True, "framerate": True, "minterpolate": True, "unsharp": True}}
GPU_CAPS = {**CPU_CAPS, "vulkan": True, "nvenc": True, "nvenc_hevc": True,
            "filters": {**CPU_CAPS["filters"], "libplacebo": True, "nlmeans_vulkan": True}}


def chain(fx, caps, width=1280, height=720, fps=30.0, live=False):
    return build_chain(normalize_enhance(fx), width, height, fps, caps, live=live, work_dir=Path("."))


# ---------------------------------------------------------------- settings

def test_defaults_are_off_and_normalize_checks_values():
    assert DEFAULT_ENHANCE["enabled"] is False and effects_on(DEFAULT_ENHANCE) == []
    fx = normalize_enhance({"denoise": "medium", "upscale": 2, "sharpen": 0.456, "interpolate": "blend", "hdr": True})
    assert fx["upscale"] == 2.0 and fx["sharpen"] == 0.46 and effects_on(fx) == ["denoise", "upscale", "sharpen", "interpolate", "hdr"]
    for bad in ({"denoise": "extreme"}, {"upscale": 4}, {"interpolate": "magic"}, {"nope": 1}):
        with pytest.raises(ValueError):
            normalize_enhance(bad)
    assert effects_on(only(fx, "sharpen")) == ["sharpen"]


def test_settings_merge_a_partial_enhance_update(tmp_path):
    from baird_hoard.config import Settings

    s = Settings(tmp_path / "s.json")
    s.update({"enhance": {"denoise": "light", "enabled": True}})
    after = s.update({"enhance": {"upscale": 2}})["enhance"]
    assert after["denoise"] == "light" and after["enabled"] is True and after["upscale"] == 2.0
    with pytest.raises(ValueError):
        s.update({"enhance": {"upscale": 9}})
    assert Settings(tmp_path / "s.json")["enhance"]["denoise"] == "light"          # written to disk


# ---------------------------------------------------------------- the chain

def test_gpu_chain_runs_noise_then_frame_rate_then_upscale():
    c = chain({"denoise": "medium", "upscale": 2, "sharpen": 0.5, "interpolate": "mci", "interpolate_fps": 60}, GPU_CAPS)
    vf = c.vf
    assert vf.index("nlmeans_vulkan") < vf.index("minterpolate") < vf.index("libplacebo")
    assert (c.out_w, c.out_h, c.out_fps) == (2560, 1440, 60.0) and c.gpu_filters
    assert c.hw_args == ["-init_hw_device", "vulkan=vk:2", "-filter_hw_device", "vk"]
    assert c.applied == ["denoise", "upscale", "sharpen", "interpolate"] and c.shader == "enhance-sharpen-050.glsl"


def test_the_gpu_is_the_one_in_the_settings():
    assert chain({"upscale": 2, "gpu": 3}, GPU_CAPS).hw_args[1] == "vulkan=vk:3"


def test_live_chain_leaves_out_what_cannot_run_live():
    c = chain({"interpolate": "mci", "hdr": True, "upscale": 2}, GPU_CAPS, live=True)
    assert "minterpolate" not in c.vf and "framerate=fps=60" in c.vf and "hdr" not in c.applied
    assert "hdr" in c.skipped and "interpolate" in c.skipped
    saved = chain({"hdr": True, "upscale": 1.5}, GPU_CAPS)
    assert saved.hdr and saved.pix_fmt == "p010le" and "color_trc=smpte2084" in saved.vf and (saved.out_w, saved.out_h) == (1920, 1080)


def test_cpu_fallback_keeps_the_effects_available():
    c = chain({"denoise": "medium", "upscale": 2, "sharpen": 0.5, "hdr": True}, CPU_CAPS)
    assert not c.gpu_filters and c.hw_args == []
    assert "hqdn3d" in c.vf and "scale=2560:1440" in c.vf and "unsharp" in c.vf and "libplacebo" not in c.vf
    assert "hdr" in c.skipped and c.pix_fmt == "yuv420p"


def test_upscale_is_capped_at_4k_and_interpolation_needs_a_higher_rate():
    c = chain({"upscale": 3}, GPU_CAPS, width=2560, height=1440)
    assert c.out_w == 3840 and c.out_h == 2160
    done = chain({"upscale": 2}, GPU_CAPS, width=3840, height=2160)
    assert "upscale" in done.skipped and done.out_w == 3840
    low = chain({"interpolate": "blend", "interpolate_fps": 30}, GPU_CAPS, fps=30.0)
    assert "interpolate" in low.skipped and low.out_fps == 30.0


# ---------------------------------------------------------------- real ffmpeg

pytestmark_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def cpu_enhancer(tmp_path):
    from baird_hoard.capture import ffmpeg_path, ffprobe_path

    ff = ffmpeg_path()
    enh = Enhancer(ff, ffprobe_path(ff), tmp_path / "work")
    enh.work_dir.mkdir(parents=True, exist_ok=True)
    enh._caps[2] = dict(CPU_CAPS)
    return enh


def probe(path: Path) -> dict:
    out = subprocess.run([FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate,codec_name", "-of", "json", str(path)], capture_output=True, check=True).stdout
    return json.loads(out)["streams"][0]


@pytestmark_ffmpeg
def test_enhance_file_writes_a_bigger_smoother_copy(tmp_path):
    from .conftest import make_ts

    src = tmp_path / "in.mp4"
    ts = make_ts(tmp_path / "in.ts", seconds=2)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(tmp_path / "in.ts"), "-c", "copy", str(src)], check=True)
    assert ts
    enh = cpu_enhancer(tmp_path)
    dst = tmp_path / "out.mp4"
    seen = []
    result = enh.enhance_file(src, dst, normalize_enhance({"denoise": "light", "upscale": 2, "sharpen": 0.5, "interpolate": "blend"}),
                              progress=seen.append)
    info = probe(dst)
    assert (info["width"], info["height"], info["r_frame_rate"]) == (640, 360, "60/1") and info["codec_name"] == "h264"
    assert result["applied"] == ["denoise", "upscale", "sharpen", "interpolate"] and result["encoder"] == "libx264" and seen
    assert src.is_file()                                       # the original is untouched


@pytestmark_ffmpeg
def test_enhance_file_without_effects_is_refused(tmp_path):
    from baird_hoard.errors import BairdError

    with pytest.raises(BairdError):
        cpu_enhancer(tmp_path).enhance_file(tmp_path / "x.mp4", tmp_path / "y.mp4", normalize_enhance({}))


@pytestmark_ffmpeg
def test_benchmark_reports_each_effect_and_the_baseline(tmp_path):
    from baird_hoard.enhance import demo_sample

    enh = cpu_enhancer(tmp_path)
    sample = demo_sample(enh.ffmpeg, tmp_path / "s.mp4", width=320, height=180, seconds=4)
    info = enh.probe(sample)
    fx = normalize_enhance({"denoise": "light", "upscale": 2, "interpolate": "blend"})
    res = enh.benchmark(fx, sample, info, frames=90)
    assert [r["effect"] for r in res["effects"]] == ["denoise", "upscale", "interpolate"]
    # A cost too small to tell from the machine's noise comes back as None ("could not measure"), never as a made-up number.
    for row in [res["baseline"], *res["effects"], res["all"]]:
        assert row["ms_per_frame"] is None or row["ms_per_frame"] > 0
        assert row["ok"] is (row["ms_per_frame"] is not None) and row["error"] == ""
    assert res["all"]["applied"] == ["denoise", "upscale", "interpolate"]
    assert res["frame_budget_ms"] == pytest.approx(33.33, abs=0.01) and isinstance(res["realtime"], bool)
    assert enh.last_benchmark is res


# ---------------------------------------------------------------- the live stage and the app

@pytest.fixture
def env(tmp_path):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient

    from baird_hoard.config import Config
    from baird_hoard.main import create_app
    from baird_hoard.services import Services

    cfg = Config(data_dir=tmp_path / "data", port=5232, fake=True, data_dir_configured=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(
        '{"media_dir": "%s", "clip_seconds": 3, "capture_always_on": true, "hotkeys_enabled": false, "ocr_auto": false}'
        % str(tmp_path / "media").replace("\\", "\\\\"))
    svc = Services(cfg)
    svc.enhancer._caps[2] = dict(CPU_CAPS)                      # never a GPU in the tests
    with TestClient(create_app(cfg, svc), base_url="http://127.0.0.1:5232", client=("127.0.0.1", 50000)) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            st = c.get("/api/status").json()
            if st["capture"]["state"] == "running" and (st["capture"]["buffered_seconds"] or 0) >= 3.5:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"demo source did not start: {st}")
        yield c


def agent(c, tool, /, **arguments):
    headers = {"Authorization": f"Bearer {c.app.state.services.token}"}
    return c.post("/api/agent/call", headers=headers, json={"name": tool, "arguments": arguments})


def wait_for(fn, seconds=40.0):
    end = time.time() + seconds
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError("timed out")


def test_the_tools_exist_and_status_starts_off(env):
    names = {t["name"] for t in env.get("/api/agent/tools", headers={"Authorization": f"Bearer {env.app.state.services.token}"}).json()["tools"]}
    assert {"enhance_status", "enhance_set", "enhance_benchmark", "media_enhance"} <= names
    st = agent(env, "enhance_status").json()
    assert st["settings"]["enabled"] is False and st["live"]["state"] == "idle" and st["capabilities"]["vulkan"] is False
    assert agent(env, "enhance_set").status_code == 400                                  # nothing to change
    assert agent(env, "enhance_set", upscale=2.5).status_code == 400                      # not a factor the chain has


def test_live_view_clips_and_recordings_use_the_enhanced_picture(env):
    svc = env.app.state.services
    assert agent(env, "enhance_set", enabled=True, upscale=2, denoise="light").json()["settings"]["upscale"] == 2.0
    wait_for(lambda: svc.stage.flowing)
    live = env.get("/api/status").json()["enhance"]
    assert live["state"] == "running" and live["chain"]["width"] == 2560 and live["chain"]["applied"] == ["denoise", "upscale"]
    # the live viewers get a stream of the enhanced size: its init segment carries the new size (avc1 level for 2560x1440)
    assert svc.stage.buffer.seconds_kept() > 0 and svc.stage.buffer.sps is not None
    wait_for(lambda: svc.stage.buffer.seconds_kept() >= 3.0)
    clip = agent(env, "clip_save", seconds=3).json()
    assert clip["enhanced"] is True and (clip["width"], clip["height"]) == (2560, 1440)
    assert agent(env, "record_start").json()["state"] == "recording"
    time.sleep(3)
    agent(env, "record_stop")
    rec = wait_for(lambda: next((i for i in agent(env, "media_list", kind="video").json()["items"] if i["origin"] == "recording"), None))
    assert rec["enhanced"] is True and (rec["width"], rec["height"]) == (2560, 1440)
    # screenshots and screen_look stay the original picture
    assert agent(env, "snapshot_save").json()["width"] == 1280


def test_record_off_keeps_clips_original_and_turning_it_off_stops_the_chain(env):
    svc = env.app.state.services
    agent(env, "enhance_set", enabled=True, upscale=2, record=False)
    wait_for(lambda: svc.stage.flowing)
    clip = agent(env, "clip_save", seconds=3).json()
    assert clip["enhanced"] is False and clip["width"] == 1280
    agent(env, "enhance_set", enabled=False)
    wait_for(lambda: not svc.stage.flowing and env.get("/api/status").json()["enhance"]["state"] == "idle")


def test_enhanced_copy_job_and_named_effects_only(env):
    lib = env.app.state.services.library
    src = agent(env, "clip_save", seconds=3).json()["name"]
    agent(env, "enhance_set", sharpen=1.0, interpolate="blend")                 # saved effects the call below must not inherit
    assert agent(env, "media_enhance", name=src).status_code == 200              # none named: the saved effects
    job = agent(env, "media_enhance", name=src, upscale=2).json()                # named: only that one
    assert job["target"] == src.replace(".mp4", " (mejorado 2).mp4")           # the first job holds the plain name
    done = wait_for(lambda: next((j for j in lib.jobs() if j["id"] == job["id"] and j["state"] != "running"), None))
    assert done["state"] == "done" and done["result"]["applied"] == ["upscale"] and (done["result"]["width"], done["result"]["height"]) == (2560, 1440)
    item = next(i for i in lib.list("video", 50) if i["name"] == job["target"])
    assert item["enhanced"] is True and item["width"] == 2560
    assert agent(env, "media_enhance", name="Captura nada.mp4", upscale=2).status_code == 404


def test_benchmark_tool_on_the_demo_sample(env):
    res = agent(env, "enhance_benchmark", source="demo", frames=60, denoise="light", interpolate="blend").json()
    assert [r["effect"] for r in res["effects"]] == ["denoise", "interpolate"] and res["baseline"]["ok"]
    assert env.get("/api/status").json()["enhance"]["state"] == "idle"                 # measuring does not turn the chain on
    assert agent(env, "enhance_status").json()["last_benchmark"]["input"]["width"] == 1280
