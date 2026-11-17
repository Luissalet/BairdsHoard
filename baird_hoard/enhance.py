"""Video enhancement on a spare GPU: noise reduction, upscaling, sharpening, frame-rate interpolation and HDR.

The effects are ffmpeg filters (the GPU ones run on Vulkan through libplacebo and ``nlmeans_vulkan``, the video is
encoded with NVENC when it works and with x264 otherwise), so the same chain serves three jobs:

* :class:`EnhanceStage` - the live picture. The capture's transport stream goes through one ffmpeg process that decodes,
  applies the chain and encodes again; its output feeds a second :class:`~baird_hoard.tsbuf.TsBuffer` that the live
  view, clips and recordings read instead of the raw one while the enhancement is on.
* :meth:`Enhancer.enhance_file` - a saved video becomes a new file next to it; the original is never touched.
* :meth:`Enhancer.benchmark` - measures the milliseconds each effect adds per frame, so the person can see what the
  chain costs before relying on it.

Everything runs on one GPU (``gpu``, default 2: the Vulkan device index and the NVENC index of the spare card on
this PC); the chain never touches the capture device and the capture path stays a stream copy when the enhancement is
off.
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import BairdError
from .tsbuf import TsBuffer

log = logging.getLogger("baird.enhance")

NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DENOISE = ("off", "light", "medium", "strong")
INTERPOLATE = ("off", "blend", "mci")
UPSCALE_FACTORS = (0.0, 1.5, 2.0, 3.0)
MAX_WIDTH = 3840

DEFAULT_ENHANCE: dict[str, Any] = {
    "enabled": False,        # the live view (and clips and recordings when ``record``) go through the chain
    "record": True,          # clips and recordings are made from the enhanced picture while it is on
    "denoise": "off",        # off | light | medium | strong
    "upscale": 0.0,          # 0 = off, or the factor the picture grows by (1.5, 2, 3; never wider than 3840)
    "sharpen": 0.0,          # 0..1, strength of the sharpening after the upscale
    "interpolate": "off",    # off | blend (cheap, live) | mci (motion compensated, for saved files)
    "interpolate_fps": 0,    # 0 = double the source's frame rate (at most 120)
    "hdr": False,            # SDR to HDR10 (HEVC, PQ, BT.2020): saved files only, the live view stays SDR
    "gpu": 2,                # Vulkan and NVENC index of the GPU the chain runs on
}

EFFECT_KEYS = ("denoise", "upscale", "sharpen", "interpolate", "hdr")
OFF: dict[str, Any] = {"denoise": "off", "upscale": 0.0, "sharpen": 0.0, "interpolate": "off", "hdr": False}


def normalize_enhance(raw: Any, base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """``base`` (or the defaults) updated with ``raw`` and checked: unknown keys and bad values raise ``ValueError``."""
    data = copy.deepcopy(base if base is not None else DEFAULT_ENHANCE)
    if raw is None:
        return data
    if not isinstance(raw, dict):
        raise ValueError("enhance must be an object")
    for key, value in raw.items():
        if key not in DEFAULT_ENHANCE:
            raise ValueError(f"Unknown enhance setting: {key}")
        if value is not None:
            data[key] = value
    for key in ("enabled", "record", "hdr"):
        data[key] = data[key] is True
    if data["denoise"] not in DENOISE:
        raise ValueError(f"denoise must be one of {', '.join(DENOISE)}")
    if data["interpolate"] not in INTERPOLATE:
        raise ValueError(f"interpolate must be one of {', '.join(INTERPOLATE)}")
    factor = float(data["upscale"])
    if factor not in UPSCALE_FACTORS:
        raise ValueError("upscale must be 0 (off), 1.5, 2 or 3")
    data["upscale"] = factor
    data["sharpen"] = round(min(max(float(data["sharpen"]), 0.0), 1.0), 2)
    data["interpolate_fps"] = int(min(max(int(data["interpolate_fps"]), 0), 120))
    data["gpu"] = int(min(max(int(data["gpu"]), 0), 7))
    return data


def effects_on(fx: dict[str, Any]) -> list[str]:
    on = []
    if fx.get("denoise", "off") != "off":
        on.append("denoise")
    if float(fx.get("upscale") or 0) > 0:
        on.append("upscale")
    if float(fx.get("sharpen") or 0) > 0:
        on.append("sharpen")
    if fx.get("interpolate", "off") != "off":
        on.append("interpolate")
    if fx.get("hdr"):
        on.append("hdr")
    return on


def only(fx: dict[str, Any], effect: str) -> dict[str, Any]:
    """``fx`` with every effect off except ``effect`` (kept at its value)."""
    out = copy.deepcopy(fx)
    for key in EFFECT_KEYS:
        if key != effect:
            out[key] = OFF[key]
    return out


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


SHARPEN_SHADER = """//!HOOK OUTPUT
//!BIND HOOKED
//!DESC sharpen
vec4 hook() {{
    vec4 c = HOOKED_texOff(vec2(0.0, 0.0));
    vec4 n = HOOKED_texOff(vec2(0.0, -1.0));
    vec4 s = HOOKED_texOff(vec2(0.0, 1.0));
    vec4 w = HOOKED_texOff(vec2(-1.0, 0.0));
    vec4 e = HOOKED_texOff(vec2(1.0, 0.0));
    return c + {amount:.3f} * (4.0 * c - n - s - w - e);
}}
"""


class Chain:
    """What :func:`build_chain` decided: the ffmpeg arguments and what was left out and why."""

    def __init__(self) -> None:
        self.hw_args: list[str] = []
        self.vf = ""
        self.out_w = 0
        self.out_h = 0
        self.out_fps = 0.0
        self.pix_fmt = "yuv420p"
        self.applied: list[str] = []
        self.skipped: dict[str, str] = {}
        self.gpu_filters = False
        self.shader: Optional[str] = None
        self.hdr = False

    def describe(self) -> dict[str, Any]:
        return {"filters": self.vf, "width": self.out_w, "height": self.out_h, "fps": round(self.out_fps, 2),
                "applied": list(self.applied), "skipped": dict(self.skipped), "gpu_filters": self.gpu_filters, "hdr": self.hdr}


def build_chain(fx: dict[str, Any], width: int, height: int, fps: float, caps: dict[str, Any], *, live: bool,
                work_dir: Path) -> Chain:
    """The ffmpeg video filter chain for ``fx`` on a ``width`` x ``height`` picture at ``fps``.

    ``live`` chains skip what cannot keep up or what a browser cannot show (``mci`` interpolation becomes ``blend``;
    HDR is left out); the reasons are in ``skipped``."""
    ch = Chain()
    fps = fps or 30.0
    gpu = int(fx.get("gpu", 2))
    filters: dict[str, bool] = caps.get("filters", {})
    vulkan = bool(caps.get("vulkan"))
    pre: list[str] = []          # CPU, before anything else (light noise reduction)
    gpu_denoise: list[str] = []  # GPU, first hop
    post: list[str] = []         # CPU frame-rate interpolation, on the small picture
    on_gpu: list[str] = []       # GPU, second hop: upscale, sharpen, colour
    cpu_place: list[str] = []    # the same work on the CPU when Vulkan is not there
    out_w, out_h = width, height

    level = fx.get("denoise", "off")
    if level != "off":
        if vulkan and filters.get("nlmeans_vulkan") and level in ("medium", "strong"):
            gpu_denoise.append("nlmeans_vulkan=s=2:p=5:r=7" if level == "medium" else "nlmeans_vulkan=s=4:p=7:r=11")
        elif filters.get("hqdn3d"):
            pre.append({"light": "hqdn3d=2:1.5:3:2.5", "medium": "hqdn3d=4:3:6:4.5", "strong": "hqdn3d=7:6:9:7"}[level])
        else:
            ch.skipped["denoise"] = "ffmpeg has no denoise filter"
            level = "off"
        if level != "off":
            ch.applied.append("denoise")

    factor = float(fx.get("upscale") or 0)
    strength = float(fx.get("sharpen") or 0)
    hdr = bool(fx.get("hdr"))
    if hdr and live:
        ch.skipped["hdr"] = "El directo se muestra en SDR; el HDR se aplica al mejorar un vídeo guardado."
        hdr = False
    place: list[str] = []
    if factor > 0:
        scale = factor
        if width * scale > MAX_WIDTH:
            scale = MAX_WIDTH / width
        if scale > 1.01:
            out_w, out_h = _even(width * scale), _even(height * scale)
            place += [f"w={out_w}", f"h={out_h}", "upscaler=ewa_lanczos"]
            ch.applied.append("upscale")
        else:
            ch.skipped["upscale"] = f"La imagen ya mide {width} px de ancho (máximo {MAX_WIDTH})."
    if strength > 0:
        ch.shader = f"enhance-sharpen-{int(round(strength * 100)):03d}.glsl"
        place.append(f"custom_shader_path={ch.shader}")
        ch.applied.append("sharpen")
    if hdr:
        place += ["colorspace=bt2020nc", "color_primaries=bt2020", "color_trc=smpte2084", "range=tv", "inverse_tonemapping=1",
                  "format=p010le"]
        ch.applied.append("hdr")
        ch.pix_fmt = "p010le"
        ch.hdr = True
    if place:
        if vulkan and filters.get("libplacebo"):
            on_gpu.append("libplacebo=" + ":".join(place))
        else:
            # Without Vulkan: plain scaling and unsharp keep the effects available, only slower and plainer.
            if factor > 0 and "upscale" in ch.applied:
                cpu_place.append(f"scale={out_w}:{out_h}:flags=lanczos")
            if strength > 0:
                cpu_place.append(f"unsharp=5:5:{strength * 1.5:.2f}:5:5:0")
            if hdr:
                ch.skipped["hdr"] = "El HDR necesita Vulkan (libplacebo)."
                ch.applied.remove("hdr")
                ch.hdr = False
                ch.pix_fmt = "yuv420p"

    mode = fx.get("interpolate", "off")
    target = float(fx.get("interpolate_fps") or 0) or min(120.0, fps * 2)
    if mode != "off":
        if live and mode == "mci":
            ch.skipped["interpolate"] = "mci es demasiado lento para el directo; se usa blend."
            mode = "blend"
        if target <= fps + 0.5:
            ch.skipped["interpolate"] = f"El destino ({target:g} fps) no supera los {fps:g} fps de origen."
        elif mode == "blend" and filters.get("framerate"):
            post.append(f"framerate=fps={target:g}:scene=0")
            ch.applied.append("interpolate")
            fps = target
        elif mode == "mci" and filters.get("minterpolate"):
            post.append(f"minterpolate=fps={target:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
            ch.applied.append("interpolate")
            fps = target
        else:
            ch.skipped["interpolate"] = "ffmpeg no trae el filtro necesario."

    # Order: noise first (cleaner motion), then the frame rate on the small picture (the costly step is far cheaper
    # before the upscale), then upscale, sharpening and colour. The GPU is one hop unless the frame-rate step has to
    # run between its two stages.
    def hop(filters: list[str], fmt: str) -> list[str]:
        return ["format=yuv420p", "hwupload", *filters, "hwdownload", f"format={fmt}"]

    parts = list(pre)
    if gpu_denoise or on_gpu:
        ch.gpu_filters = True
        ch.hw_args = ["-init_hw_device", f"vulkan=vk:{gpu}", "-filter_hw_device", "vk"]
    if post:
        if gpu_denoise:
            parts += hop(gpu_denoise, "yuv420p")
        parts += post
        if on_gpu:
            parts += hop(on_gpu, ch.pix_fmt)
    elif gpu_denoise or on_gpu:
        parts += hop(gpu_denoise + on_gpu, ch.pix_fmt)
    parts += cpu_place
    if ch.pix_fmt == "yuv420p" and not (parts and parts[-1].startswith("format=")):
        parts.append("format=yuv420p")
    ch.vf = ",".join(parts)
    ch.out_w, ch.out_h, ch.out_fps = out_w, out_h, fps
    return ch


def ensure_shader(work_dir: Path, chain: Chain) -> None:
    if chain.shader:
        amount = int(chain.shader.split("-")[-1].split(".")[0]) / 100.0 * 0.5
        path = work_dir / chain.shader
        text = SHARPEN_SHADER.format(amount=amount)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            work_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def encoder_args(caps: dict[str, Any], gpu: int, width: int, height: int, fps: float, *, hdr: bool, live: bool,
                 gop: Optional[int] = None) -> tuple[list[str], str]:
    """``(arguments, encoder name)`` for the output of a chain."""
    kbps = int(min(60000, max(1500, width * height * fps * (0.07 if live else 0.09) / 1000)))
    gop = gop or max(1, int(round(fps)))
    if hdr:
        if caps.get("nvenc_hevc"):
            return (["-c:v", "hevc_nvenc", "-gpu", str(gpu), "-preset", "p5", "-profile:v", "main10", "-rc", "vbr", "-b:v", f"{kbps}k",
                     "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k", "-g", str(gop), "-tag:v", "hvc1",
                     "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"], "hevc_nvenc")
        return (["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                 "-x265-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc", "-g", str(gop)], "libx265")
    if caps.get("nvenc"):
        args = ["-c:v", "h264_nvenc", "-gpu", str(gpu), "-preset", "p3" if live else "p5", "-rc", "vbr", "-b:v", f"{kbps}k",
                "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k", "-g", str(gop), "-bf", "0" if live else "2",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
        if live:
            args += ["-tune", "ll", "-forced-idr", "1"]
        return args, "h264_nvenc"
    args = ["-c:v", "libx264", "-preset", "ultrafast" if live else "medium", "-g", str(gop), "-pix_fmt", "yuv420p"]
    if live:
        args += ["-tune", "zerolatency", "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
    else:
        args += ["-crf", "18"]
    return args, "libx264"


class Enhancer:
    """Probes what the machine can do and runs the chain over files, samples (benchmark) and, through
    :class:`EnhanceStage`, the live stream."""

    def __init__(self, ffmpeg: Optional[str], ffprobe: Optional[str], work_dir: Path):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.work_dir = Path(work_dir)
        self._caps: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.last_benchmark: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ what the machine can do

    def _run(self, args: list[str], *, data: Optional[bytes] = None, timeout: float = 60.0,
             cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        return subprocess.run(args, input=data, capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
                              cwd=str(cwd) if cwd else None)

    def _need_ffmpeg(self) -> str:
        if not self.ffmpeg:
            raise BairdError("unavailable", "ffmpeg was not found.", "Install it (winget install Gyan.FFmpeg) and restart the app.")
        return self.ffmpeg

    def capabilities(self, gpu: int = 2, refresh: bool = False) -> dict[str, Any]:
        """Which filters and encoders exist and whether Vulkan and NVENC work on ``gpu`` (probed once, on that GPU only)."""
        with self._lock:
            if gpu in self._caps and not refresh:
                return copy.deepcopy(self._caps[gpu])
        out: dict[str, Any] = {"ffmpeg": bool(self.ffmpeg), "gpu": gpu, "filters": {}, "vulkan": False, "nvenc": False,
                               "nvenc_hevc": False}
        if self.ffmpeg:
            try:
                text = self._run([self.ffmpeg, "-hide_banner", "-filters"], timeout=20).stdout.decode("utf-8", "replace")
                names = {line.split()[1] for line in text.splitlines() if len(line.split()) > 2}
                out["filters"] = {n: n in names for n in ("libplacebo", "nlmeans_vulkan", "hqdn3d", "framerate", "minterpolate", "unsharp")}
            except (subprocess.SubprocessError, OSError):
                log.debug("listing filters failed", exc_info=True)
            tiny = ["-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=0.5"]
            if out["filters"].get("libplacebo") or out["filters"].get("nlmeans_vulkan"):
                proc = self._run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-init_hw_device", f"vulkan=vk:{gpu}", "-filter_hw_device", "vk",
                                  *tiny, "-vf", "format=yuv420p,hwupload,hwdownload,format=yuv420p", "-f", "null", "-"], timeout=60)
                out["vulkan"] = proc.returncode == 0
            for key, enc in (("nvenc", "h264_nvenc"), ("nvenc_hevc", "hevc_nvenc")):
                proc = self._run([self.ffmpeg, "-hide_banner", "-loglevel", "error", *tiny, "-c:v", enc, "-gpu", str(gpu), "-f", "null", "-"],
                                 timeout=60)
                out[key] = proc.returncode == 0
        with self._lock:
            self._caps[gpu] = out
        return copy.deepcopy(out)

    def chain_for(self, fx: dict[str, Any], width: int, height: int, fps: float, *, live: bool) -> tuple[Chain, dict[str, Any]]:
        caps = self.capabilities(int(fx.get("gpu", 2)))
        chain = build_chain(fx, width, height, fps, caps, live=live, work_dir=self.work_dir)
        ensure_shader(self.work_dir, chain)
        return chain, caps

    def probe(self, path: Path) -> dict[str, Any]:
        """Width, height, frame rate, duration and whether there is sound."""
        if not self.ffprobe:
            raise BairdError("unavailable", "ffprobe was not found.")
        proc = self._run([self.ffprobe, "-v", "error", "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate:format=duration",
                          "-of", "json", str(path)], timeout=30)
        import json
        try:
            info = json.loads(proc.stdout or b"{}")
        except ValueError:
            info = {}
        out: dict[str, Any] = {"audio": False}
        for st in info.get("streams", []):
            if st.get("codec_type") == "video" and st.get("width") and "width" not in out:
                out.update(width=int(st["width"]), height=int(st["height"]))
                for key in ("avg_frame_rate", "r_frame_rate"):
                    num, _, den = str(st.get(key) or "0/1").partition("/")
                    try:
                        value = float(num) / float(den or 1)
                    except (ValueError, ZeroDivisionError):
                        value = 0.0
                    if value > 0:
                        out["fps"] = round(value, 3)
                        break
            elif st.get("codec_type") == "audio":
                out["audio"] = True
        try:
            out["duration"] = float((info.get("format") or {}).get("duration") or 0)
        except ValueError:
            out["duration"] = 0.0
        if "width" not in out:
            raise BairdError("invalid", "That file has no video picture.")
        out.setdefault("fps", 30.0)
        return out

    # ------------------------------------------------------------------ files

    def file_command(self, src: Path, dst: Path, fx: dict[str, Any], info: dict[str, Any]) -> tuple[list[str], Chain, str]:
        chain, caps = self.chain_for(fx, info["width"], info["height"], info["fps"], live=False)
        enc, name = encoder_args(caps, int(fx["gpu"]), chain.out_w, chain.out_h, chain.out_fps, hdr=chain.hdr, live=False)
        cmd = [self._need_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *chain.hw_args, "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
        if chain.vf:
            cmd += ["-vf", chain.vf]
        cmd += [*enc, "-c:a", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dst)]
        return cmd, chain, name

    def enhance_file(self, src: Path, dst: Path, fx: dict[str, Any], progress: Optional[Callable[[float], None]] = None,
                     timeout: float = 6 * 3600) -> dict[str, Any]:
        """Write the enhanced copy of ``src`` to ``dst``; returns what was done."""
        if not effects_on(fx):
            raise BairdError("invalid", "Turn at least one effect on first.", "enhance_set or the arguments choose them.")
        info = self.probe(src)
        cmd, chain, encoder = self.file_command(src, dst, fx, info)
        started = time.time()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW, cwd=str(self.work_dir))
        err: list[bytes] = []
        threading.Thread(target=lambda: err.append(proc.stderr.read() if proc.stderr else b""), daemon=True).start()
        total = max(info.get("duration") or 0.0, 0.001)
        try:
            for raw in iter(proc.stdout.readline, b"") if proc.stdout else []:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("out_time_us=") and progress:
                    try:
                        progress(min(0.99, int(line.split("=", 1)[1]) / 1e6 / total))
                    except ValueError:
                        pass
                if time.time() - started > timeout:
                    proc.kill()
                    break
            code = proc.wait(timeout=30)
        except Exception:
            proc.kill()
            raise
        if code != 0 or not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            message = (err[0] if err else b"").decode("utf-8", "replace")[-400:]
            raise BairdError("failed", "Could not enhance the video.", message)
        elapsed = time.time() - started
        frames = max(1, int(round(info["fps"] * total)))
        return {"encoder": encoder, "seconds": round(elapsed, 2), "ms_per_frame": round(elapsed * 1000 / frames, 1),
                "input": {"width": info["width"], "height": info["height"], "fps": info["fps"]}, **chain.describe()}

    # ------------------------------------------------------------------ cost

    def benchmark(self, fx: dict[str, Any], sample: Path, info: dict[str, Any], frames: int = 90) -> dict[str, Any]:
        """Milliseconds per input frame of the chain, each enabled effect alone and all together, over ``sample``.

        Every run decodes the sample, applies the filters and encodes into the null muxer. Each is run over a third of
        the frames and over all of them: the slope between the two is the steady cost per frame, what is left is the
        start-up (Vulkan, shader compilation, encoder) reported apart. ``baseline`` is the same run with no effect, so
        ``added_ms`` is what the effect itself costs."""
        ff = self._need_ffmpeg()
        caps = self.capabilities(int(fx["gpu"]))
        short = max(10, frames // 3)

        def timed(chain: Chain, enc: list[str], count: int) -> tuple[float, "subprocess.CompletedProcess"]:
            # ``-t`` before ``-i`` limits what is read, so ``count`` is input frames whatever the chain does to the rate.
            cmd = [ff, "-hide_banner", "-loglevel", "error", *chain.hw_args, "-t", f"{count / info['fps']:.4f}", "-i", str(sample),
                   "-map", "0:v:0"]
            if chain.vf:
                cmd += ["-vf", chain.vf]
            cmd += [*enc, "-f", "null", "-"]
            best, last = float("inf"), None
            for _ in range(2):  # the faster of two runs: the other is whatever else the machine was doing
                t0 = time.perf_counter()
                last = self._run(cmd, timeout=900, cwd=self.work_dir)
                best = min(best, time.perf_counter() - t0)
            return best, last

        def run(label: str, effect_fx: dict[str, Any]) -> dict[str, Any]:
            chain, _ = self.chain_for(effect_fx, info["width"], info["height"], info["fps"], live=False)
            enc, name = encoder_args(caps, int(fx["gpu"]), chain.out_w, chain.out_h, chain.out_fps, hdr=chain.hdr, live=True)
            t_short, p_short = timed(chain, enc, short)
            t_long, p_long = timed(chain, enc, frames)
            ok = p_short.returncode == 0 and p_long.returncode == 0
            per = (t_long - t_short) * 1000 / (frames - short) if ok else None
            if per is not None and per <= 0:
                per = None  # the longer run was not slower than the shorter one: the machine was too noisy to say
            return {"effect": label, "ok": ok and per is not None, "ms_per_frame": round(per, 2) if per is not None else None,
                    "startup_s": round(max(0.0, t_short - short * per / 1000), 2) if per is not None else None,
                    "applied": chain.applied, "skipped": chain.skipped, "encoder": name,
                    "error": "" if ok else (p_long.stderr or p_short.stderr).decode("utf-8", "replace")[-200:]}

        base_fx = copy.deepcopy(fx)
        for key in EFFECT_KEYS:
            base_fx[key] = OFF[key]
        baseline = run("baseline", base_fx)
        rows = []
        for effect in effects_on(fx):
            row = run(effect, only(fx, effect))
            row["added_ms"] = round(row["ms_per_frame"] - (baseline["ms_per_frame"] or 0.0), 2) if row["ok"] else None
            rows.append(row)
        combined = run("all", fx) if len(effects_on(fx)) > 1 else (dict(rows[0], effect="all") if rows else None)
        budget = 1000.0 / info["fps"]
        out = {"frames": frames, "input": {"width": info["width"], "height": info["height"], "fps": info["fps"]},
               "frame_budget_ms": round(budget, 2), "baseline": baseline, "effects": rows, "all": combined,
               "realtime": bool(combined and combined["ok"] and combined["ms_per_frame"] <= budget),
               "gpu": int(fx["gpu"]), "at": time.time()}
        self.last_benchmark = out
        return out


class _Listenable:
    def __init__(self) -> None:
        self._listeners: list[Callable[[bytes, list[int], int], None]] = []
        self._llock = threading.Lock()

    def add_listener(self, fn: Callable[[bytes, list[int], int], None]) -> None:
        with self._llock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[bytes, list[int], int], None]) -> None:
        with self._llock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _fire(self, aligned: bytes, starts: list[int], total: int) -> None:
        with self._llock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(aligned, starts, total)
            except Exception:  # noqa: BLE001 - a listener must not stop the chain
                log.exception("enhanced stream listener failed")


class EnhanceStage(_Listenable):
    """The live chain: raw transport stream in, enhanced transport stream out (its own buffer and listeners).

    It starts with the first raw data while the enhancement is on, ends when the data stops, and never blocks the
    capture: the raw chunks wait in a bounded queue and the oldest are dropped when ffmpeg falls behind."""

    QUEUE = 1024            # raw chunks that may wait while ffmpeg starts or lags (about 10 s of the device's stream)
    IDLE_S = 6.0

    def __init__(self, engine: Any, settings: Any, enhancer: Enhancer, on_state: Optional[Callable[[bool], None]] = None):
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.enhancer = enhancer
        self.on_state = on_state
        self.buffer = TsBuffer(keep_seconds=settings["buffer_seconds"])
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=self.QUEUE)
        self._primed_at = 0
        self._last_in = 0.0
        self._resync = False        # the queue overflowed: skip raw data until the next key frame
        self._started = 0.0
        self._flowing = False
        self._stop = threading.Event()
        self._gen = 0
        self.chain: Optional[Chain] = None
        self.encoder = ""
        self.message = ""
        self.stats: dict[str, Any] = {"dropped": 0, "frames": 0, "fps": 0.0, "speed": 0.0, "restarts": 0}
        engine.add_listener(self._on_raw)
        threading.Thread(target=self._watchdog, name="baird-enhance-watch", daemon=True).start()

    # ------------------------------------------------------------------ state

    def enabled(self) -> bool:
        return bool(self.settings["enhance"]["enabled"]) and bool(effects_on(self.settings["enhance"]))

    @property
    def flowing(self) -> bool:
        return self._flowing

    def snapshot(self) -> dict[str, Any]:
        """What :class:`~baird_hoard.live.LiveRemux` asks of its source: ``state`` is ``running`` once pictures flow."""
        return {"state": "running" if self._flowing else ("starting" if self._proc is not None else "idle")}

    def set_demand(self, source: object, active: bool) -> None:
        self.engine.set_demand(source, active)

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = {"enabled": self.enabled(), "state": self.snapshot()["state"], "message": self.message, "encoder": self.encoder,
                   "seconds_kept": round(self.buffer.seconds_kept(), 1), **self.stats}
            out["chain"] = self.chain.describe() if self.chain else None
            out["uptime_s"] = round(time.time() - self._started, 1) if self._proc is not None else 0.0
            return out

    def wait_flowing(self, timeout: float = 12.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._flowing:
                return True
            time.sleep(0.1)
        return self._flowing

    # ------------------------------------------------------------------ control

    def configure(self) -> None:
        """The settings changed: end the running chain; the next raw data starts it again with the new settings."""
        self._end_proc()

    def raw_state(self, running: bool) -> None:
        if not running:
            self._end_proc()

    def stop(self) -> None:
        self._stop.set()
        self._end_proc()

    def _end_proc(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            was_flowing, self._flowing = self._flowing, False
            self._gen += 1
        if proc is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.buffer.reset()
        if was_flowing and self.on_state:
            self.on_state(False)

    def _watchdog(self) -> None:
        while not self._stop.wait(1.0):
            if self._proc is not None and time.time() - self._last_in > self.IDLE_S:
                log.info("enhance chain idle; stopping it")
                self._end_proc()

    # ------------------------------------------------------------------ data

    def _on_raw(self, aligned: bytes, starts: list[int], end_total: int) -> None:
        if not self.enabled() or self._stop.is_set():
            if self._proc is not None:
                self._end_proc()
            return
        self._last_in = time.time()
        if self._proc is None:
            if not self._start():
                return
        if end_total <= self._primed_at:
            return
        if self._resync:
            if not starts:
                return
            aligned = self.engine.buffer.header + aligned[starts[0]:]
            self._resync = False
        try:
            self._queue.put_nowait(aligned)
        except queue.Full:
            self.stats["dropped"] += 1
            self._resync = True
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass

    def _start(self) -> bool:
        fx = self.settings["enhance"]
        try:
            inp = self.engine.snapshot().get("input") or {}
            width, height = int(inp.get("width") or 1920), int(inp.get("height") or 1080)
            fps = float(inp.get("fps") or 30.0)
            chain, caps = self.enhancer.chain_for(fx, width, height, fps, live=True)
            enc, name = encoder_args(caps, int(fx["gpu"]), chain.out_w, chain.out_h, chain.out_fps, hdr=False, live=True)
            cmd = [self.enhancer._need_ffmpeg(), "-hide_banner", "-loglevel", "error", *chain.hw_args, "-fflags", "+genpts+nobuffer+discardcorrupt",
                   "-flags", "low_delay", "-probesize", "2000000", "-analyzeduration", "2000000", "-f", "mpegts", "-i", "pipe:0",
                   "-map", "0:v:0", "-map", "0:a:0?"]
            if chain.vf:
                cmd += ["-vf", chain.vf]
            cmd += [*enc, "-c:a", "copy", "-flush_packets", "1", "-muxdelay", "0", "-f", "mpegts", "-progress", "pipe:2", "-stats_period", "1", "pipe:1"]
            head, self._primed_at = self.engine.buffer.tail()
            with self._lock:
                self.buffer.reset()
                self._queue = queue.Queue(maxsize=self.QUEUE)
                self._gen += 1
                gen = self._gen
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                              creationflags=NO_WINDOW, cwd=str(self.enhancer.work_dir))
                proc = self._proc
                self.chain, self.encoder, self.message = chain, name, ""
                self._started = time.time()
                self._resync = False
                self.stats.update(frames=0, fps=0.0, speed=0.0)
                self.stats["restarts"] += 1
            if head:
                self._queue.put_nowait(head)
            q = self._queue
            threading.Thread(target=self._writer, args=(proc, q, gen), name="baird-enhance-in", daemon=True).start()
            threading.Thread(target=self._reader, args=(proc, gen), name="baird-enhance-out", daemon=True).start()
            threading.Thread(target=self._errors, args=(proc, gen), name="baird-enhance-err", daemon=True).start()
            log.info("enhance chain started: %s (%s)", chain.vf or "no filters", name)
            return True
        except Exception as exc:  # noqa: BLE001
            self.message = str(exc)[:300]
            log.warning("enhance chain did not start: %s", exc)
            self._proc = None
            self._last_in = 0.0
            return False

    def _writer(self, proc: subprocess.Popen, q: "queue.Queue[Optional[bytes]]", gen: int) -> None:
        try:
            while gen == self._gen:
                chunk = q.get()
                if chunk is None or proc.stdin is None:
                    break
                proc.stdin.write(chunk)
        except (OSError, ValueError):
            pass

    def _reader(self, proc: subprocess.Popen, gen: int) -> None:
        out = proc.stdout
        if out is None:
            return
        try:
            while gen == self._gen:
                chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)  # type: ignore[union-attr]
                if not chunk:
                    break
                aligned, starts = self.buffer.feed(chunk)
                if not aligned:
                    continue
                if not self._flowing and self.buffer.latest()[1] and gen == self._gen:
                    self._flowing = True
                    if self.on_state:
                        self.on_state(True)
                self._fire(aligned, starts, self.buffer.bytes_total)
        except (OSError, ValueError):
            pass
        with self._lock:
            ended = gen == self._gen and self._proc is proc
        if ended:
            self.message = self.message or "La cadena de mejora terminó; se reinicia con la siguiente imagen."
            self._end_proc()

    def _errors(self, proc: subprocess.Popen, gen: int) -> None:
        if proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            key, _, value = line.partition("=")
            if key == "frame":
                try:
                    self.stats["frames"] = int(value)
                except ValueError:
                    pass
            elif key == "fps":
                try:
                    self.stats["fps"] = float(value)
                except ValueError:
                    pass
            elif key == "speed":
                try:
                    self.stats["speed"] = float(value.strip().rstrip("x") or 0)
                except ValueError:
                    pass
            elif "=" not in line and gen == self._gen:
                self.message = line[-300:]
                log.warning("enhance ffmpeg: %s", line[:300])


class StageSource(_Listenable):
    """Presents :class:`EnhanceStage` to :class:`~baird_hoard.live.LiveRemux` as if it were a capture engine."""

    def __init__(self, stage: EnhanceStage):
        self._stage = stage
        self.buffer = stage.buffer

    def add_listener(self, fn: Callable[[bytes, list[int], int], None]) -> None:
        self._stage.add_listener(fn)

    def remove_listener(self, fn: Callable[[bytes, list[int], int], None]) -> None:
        self._stage.remove_listener(fn)

    def snapshot(self) -> dict[str, Any]:
        return self._stage.snapshot()

    def set_demand(self, source: object, active: bool) -> None:
        self._stage.set_demand(source, active)


def demo_sample(ffmpeg: str, dest: Path, *, width: int = 1280, height: int = 720, fps: int = 30, seconds: int = 4) -> Path:
    """A noisy synthetic video for benchmarks that need no capture picture."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                           f"testsrc2=size={width}x{height}:rate={fps}:duration={seconds},noise=alls=14:allf=t",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(dest)],
                          capture_output=True, creationflags=NO_WINDOW, timeout=120)
    if proc.returncode != 0:
        raise BairdError("failed", "Could not make the demo sample.", proc.stderr.decode("utf-8", "replace")[-300:])
    return dest
