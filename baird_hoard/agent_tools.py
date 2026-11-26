"""Tools for assistants. One catalogue drives ``/api/agent/*`` (the MCP bridge), the web UI (``/api/ui/call``) and
``mcp_server.py``. ``screen_look`` returns the picture itself (an MCP image) so an assistant can see the other computer."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .agent_sources import SourceArgs, SourceBoundArgs, SourceUndoArgs, call_on_source
from .enhance import DEFAULT_ENHANCE, EFFECT_KEYS, OFF, demo_sample, effects_on, normalize_enhance
from .errors import BairdError
from .hoard_link.agentkit import Empty, Tool, ann
from .hoard_link.agentkit import call_tool as _call_tool
from .hoard_link.agentkit import tool_catalog as _catalog
from .hoard_link.agentkit import uncapped  # noqa: F401 - re-exported for the UI routes
from .services import Services

AGENT_INSTRUCTIONS = """Baird's Hoard shows the selected capture source and keeps the last minutes in memory.
Use source_list to find the current mode, whether it is enabled, and the available display IDs. source_select prepares a source and returns an undo_token when the choice changes. The person turns on a local screen in Baird. If a live call returns source_off, ask them to turn it on. The optional source object on capture, recording and copilot tools checks the expected mode and monitor without switching.
screen_look returns the live picture as an image and saves it as a screenshot unless save=false. capture_status gives the input resolution and buffered seconds. For HDMI, state stuck means the person needs to unplug and reconnect the capture card.
clip_save saves the last N seconds as an MP4 (instant, no re-encoding). record_start / record_pause / record_resume / record_stop control the recording. media_list lists the screenshots, clips and recordings (newest first) with their paths; media_look shows a saved screenshot or a frame of a video. media_light_copy makes a smaller copy of a video for chats with upload limits. media_copy puts a file on the Windows clipboard so the user can paste it; media_reveal shows it in Explorer. media_delete sends a file to the Recycle Bin (confirm=true).
snapshot_save and screen_look accept a region (a rectangle in pixels of the input picture, or fractions 0..1) to save or look at only that part. media_search finds visible text in screenshots, recordings and saved reports and returns timestamped excerpts; media_ocr reads one now. send_to_faustus opens a Faustus chat with a file attached to its message box and returns the chat's url.
watch_add makes a rule that watches a zone and alerts the person (through the Hub's notifications) when it changes, when a changing zone goes still (a long process ended) or when a text appears or disappears; watch_list / watch_events show rules and alerts. hotkeys_status reports the global shortcuts.
enhance_set turns the GPU enhancement on and chooses the effects (denoise, upscale, sharpen, interpolate, hdr): the live view, clips and recordings then use the enhanced picture (screen_look and screenshots stay the original). enhance_status shows what it does now and what each effect costs per frame; enhance_benchmark measures that cost on a sample; media_enhance writes an enhanced copy of a saved video.
Copilot mode guides the person live on that screen: copilot_show draws steps over the live picture (a red dot where to click, boxes, arrows, the text to type, the keys to press) with one short instruction each as a subtitle under it, and Baird moves from step to step by itself by watching the screen, so send the whole plan in one call and let it wait (wait_s); it answers when the plan is done or the person needs you, with the picture. Its message is you talking to the person in the conversation beside the picture, where they can write back (event say). Point at things by the text they show ("click": "Guardar") whenever they have one: no look needed and it follows scrolling. copilot_look is the cheap way to see the screen while guiding (nothing is saved), copilot_find says where a text is, copilot_wait waits for the person, copilot_stop ends the session. The person clicks and types during guidance.
A captured picture may contain private information. Describe what the task needs."""


def _d(first: str, detail: str = "", synonyms: str = "") -> str:
    parts = [first]
    if detail:
        parts.append(detail)
    if synonyms:
        parts.append("Sinónimos: " + synonyms)
    return "\n".join(parts)


class RegionArg(BaseModel):
    x: float = Field(..., ge=0, description="Left edge.")
    y: float = Field(..., ge=0, description="Top edge.")
    w: float = Field(..., gt=0, description="Width.")
    h: float = Field(..., gt=0, description="Height.")
    unit: Literal["px", "fraction"] = Field("px", description="px = pixels of the input picture (capture_status gives its size); fraction = 0..1 of the width/height.")


class LookArgs(SourceBoundArgs):
    save: bool = Field(True, description="Also keep it in the library as a screenshot.")
    max_side: int = Field(1568, ge=320, le=3840, description="Longest side of the image returned to the assistant.")
    region: Optional[RegionArg] = Field(None, description="Look at only this rectangle of the screen.")


class SnapshotArgs(SourceBoundArgs):
    format: Optional[Literal["png", "jpg"]] = Field(None, description="png (exact pixels) or jpg; default from settings.")
    region: Optional[RegionArg] = Field(None, description="Save only this rectangle of the screen (a zone capture).")


class TranscribeArgs(SourceBoundArgs):
    region: Optional[RegionArg] = None
    detail: Literal["auto", "off", "document", "code"] = "auto"


class SearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=200, description="Words to find in screenshots, saved reports and the visible text of recordings. All must appear, ignoring case and accents.")
    limit: int = Field(30, ge=1, le=200)
    recording: Optional[str] = Field(None, max_length=255, description="Limit timestamp results to this library item.")
    after: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description="Return moments after this playback second.")


class ClipArgs(SourceBoundArgs):
    seconds: Optional[float] = Field(None, ge=1, le=600, description="How many seconds back; default from settings (30).")


class ListArgs(BaseModel):
    kind: Literal["all", "image", "video", "document"] = "all"
    limit: int = Field(50, ge=1, le=500)


class NameArg(BaseModel):
    name: str = Field(..., min_length=1, max_length=260, description="File name as media_list shows it.")


class RereadArgs(NameArg):
    mode: Literal["auto", "document", "code"] = "auto"


class LookFileArgs(NameArg):
    max_side: int = Field(1568, ge=320, le=3840)


class DeleteArgs(NameArg):
    confirm: bool = Field(False, description="Must be true: the file goes to the Recycle Bin.")


class RenameArgs(NameArg):
    new_name: str = Field(..., min_length=1, max_length=200)


class SendArgs(NameArg):
    message: str = Field("", max_length=4000, description="Text to leave in the chat's message box next to the file.")


class WatchAddArgs(SourceBoundArgs):
    name: Optional[str] = Field(None, max_length=80, description="Name shown in the alert; a default is made from the rule.")
    kind: Literal["change", "text"] = Field("change", description="change = compare the zone's pixels; text = read the zone with OCR.")
    mode: Optional[Literal["change", "still", "appears", "disappears"]] = Field(
        None, description="change rules: change (fire when the zone changes) or still (fire when a changing zone stops: a long process ended). "
                          "text rules: appears (default) or disappears.")
    region: Optional[RegionArg] = Field(None, description="Zone to watch; empty = the whole screen.")
    threshold: Optional[float] = Field(None, ge=0.1, le=100, description="Percentage of the zone that must change (default 5).")
    still_seconds: Optional[float] = Field(None, ge=2, le=3600, description="mode=still: seconds without change before firing (default 30).")
    text: Optional[str] = Field(None, max_length=200, description="kind=text: the text to wait for (case and accents ignored).")
    interval_s: Optional[float] = Field(None, ge=1, le=3600, description="Seconds between looks (change: 2, text: 10).")
    cooldown_s: Optional[float] = Field(None, ge=0, le=86400, description="Minimum seconds between two alerts of this rule (60).")
    notify: Optional[bool] = Field(None, description="Tell the person through the Hub (toast, phone...). Default true.")
    save_image: Optional[bool] = Field(None, description="Keep a picture of the zone with each alert. Default true.")
    enabled: Optional[bool] = None


class WatchIdArgs(SourceBoundArgs):
    id: str = Field(..., min_length=1, max_length=40, description="Rule id from watch_list.")


class WatchUpdateArgs(WatchAddArgs, WatchIdArgs):
    pass


class WatchEventsArgs(BaseModel):
    limit: int = Field(20, ge=1, le=200)


class RevealArgs(BaseModel):
    name: Optional[str] = Field(None, max_length=260, description="Select this file; empty opens the folder.")


class SettingsArgs(BaseModel):
    clip_seconds: Optional[int] = Field(None, ge=3, le=600)
    buffer_seconds: Optional[int] = Field(None, ge=10, le=600)
    snapshot_format: Optional[Literal["png", "jpg"]] = None
    audio_input: Optional[str] = Field(None, pattern=r"^(hdmi|line|spdif|[0-9])$", description="hdmi, line (3.5 mm), spdif or a crossbar input number from the capture status.")
    capture_enabled: Optional[bool] = None
    capture_always_on: Optional[bool] = Field(None, description="Keep capture on without demand. Repeated opens of the legacy driver contributed to a Windows crash.")
    media_dir: Optional[str] = Field(None, max_length=500, description="Folder for the files; empty string = default.")
    device: Optional[str] = Field(None, max_length=80)
    light_height: Optional[int] = Field(None, ge=240, le=1080)
    light_kbps: Optional[int] = Field(None, ge=300, le=20000)
    ocr_auto: Optional[bool] = Field(None, description="Read the text of every new screenshot in the background (for media_search).")
    hotkeys_enabled: Optional[bool] = Field(None, description="Global shortcuts for screenshot and clip.")
    hotkey_shot: Optional[str] = Field(None, max_length=40, description="e.g. ctrl+alt+c")
    hotkey_clip: Optional[str] = Field(None, max_length=40, description="e.g. ctrl+alt+x")


class EffectArgs(BaseModel):
    denoise: Optional[Literal["off", "light", "medium", "strong"]] = Field(None, description="Noise reduction; medium and strong run on the GPU.")
    upscale: Optional[float] = Field(None, ge=0, le=3, description="0 = off, or the factor the picture grows by: 1.5, 2 or 3 (never wider than 3840 px).")
    sharpen: Optional[float] = Field(None, ge=0, le=1, description="Sharpening strength after the upscale, 0..1.")
    interpolate: Optional[Literal["off", "blend", "mci"]] = Field(None, description="Frame-rate interpolation: blend is cheap and works live; mci (motion compensated) is for saved videos.")
    interpolate_fps: Optional[int] = Field(None, ge=0, le=120, description="Target frame rate; 0 = double the source's.")
    hdr: Optional[bool] = Field(None, description="SDR to HDR10 (HEVC). Saved videos only: the live view stays SDR.")
    gpu: Optional[int] = Field(None, ge=0, le=7, description="Vulkan and NVENC index of the GPU the chain runs on (default 2).")


class EnhanceSetArgs(EffectArgs):
    enabled: Optional[bool] = Field(None, description="Send the live view (and clips and recordings) through the chain.")
    record: Optional[bool] = Field(None, description="Clips and recordings come from the enhanced picture while it is on (default true).")


class EnhanceFileArgs(NameArg, EffectArgs):
    pass


class EnhanceBenchArgs(EffectArgs):
    source: str = Field("demo", max_length=260, description="demo = a synthetic noisy video (no capture needed), live = the last seconds of the capture, or a video's name from media_list.")
    size: Literal["720p", "1080p"] = Field("720p", description="Picture size of the demo sample.")
    capture_source: SourceArgs | None = Field(None, description="Expected capture source when source=live.")
    frames: int = Field(150, ge=30, le=600, description="Frames the longer run of each measurement processes (a third of them for the shorter).")


class CopilotLookArgs(SourceBoundArgs):
    max_side: Optional[int] = Field(None, ge=320, le=2048, description="Longest side of the picture you get (default 1024); smaller costs you less.")
    region: Optional[list[float]] = Field(None, min_length=4, max_length=4, description="[x, y, w, h] in the coordinates of your last look: zoom into that part instead of asking for a bigger picture.")
    text: bool = Field(False, description="Also list every text on screen as \"x,y text\" (its centre).")
    image: bool = Field(True, description="false: no picture, only what else was asked (with text=true, the cheapest way to read labels).")


class CopilotShowArgs(SourceBoundArgs):
    steps: list[Any] = Field(default_factory=list, max_length=40, description=(
        "The steps, in order; each is an object (or just a sentence). Fields: say (one short imperative sentence in the person's "
        "language: the subtitle under the picture), then what to draw: click / dbl / right (a place), type (the text to type, with at = where), keys (\"Ctrl+S\"), "
        "box (a place to look at), arrow ([from, to], a drag), note (a label, with at), or marks: [...] for several; hint (a second "
        "line); until (when the step is over: \"auto\" by default = the screen changes where you pointed or the typed text shows up, "
        "\"manual\", {\"text\": \"...\"} appears, {\"gone\": \"...\"}, {\"after\": seconds}). A place is \"text on screen\" "
        "(found by OCR; it may not be on screen yet), [x, y], [x, y, w, h] or {\"find\": \"Guardar\", \"nth\": 2, \"side\": "
        "\"right\", \"dx\": 40}; numbers are pixels of your last copilot_look. Example: [{\"say\": \"Abre Archivo\", \"click\": "
        "\"Archivo\"}, {\"say\": \"Escribe el nombre\", \"type\": \"informe\", \"at\": {\"find\": \"Nombre\", \"side\": \"right\"}}]."))
    message: Optional[str] = Field(None, max_length=600, description="You talking to the person: a line in the conversation beside the picture (explain, answer, say what comes next). With or without steps.")
    agent: Optional[str] = Field(None, max_length=40, description="Your name, shown to the person (first call).")
    goal: Optional[str] = Field(None, max_length=160, description="What the session is for, shown to the person (first call).")
    replace: bool = Field(True, description="true: these steps replace the ones not done yet. false: they go after them.")
    wait_s: float = Field(45, ge=0, le=150, description="Then wait up to this long, like copilot_wait (0 = answer at once).")
    look: Literal["auto", "always", "never"] = Field("auto", description="Whether the answer brings the picture: auto = when eyes are needed (plan done, help, a missing text).")
    each: bool = Field(False, description="Also answer after every single step (normally only when you are needed).")
    space: Optional[list[float]] = Field(None, min_length=2, max_length=2, description="[width, height] your coordinates are measured on, when it is not your last look.")


class RecordingTargetArgs(SourceBoundArgs):
    target: Optional[str] = Field(None, max_length=255, description="Restrict the action to the named active recording.")


class CopilotWaitArgs(SourceBoundArgs):
    wait_s: float = Field(45, ge=0, le=150, description="Longest wait in seconds.")
    look: Literal["auto", "always", "never"] = "auto"
    each: bool = False


class CopilotFindArgs(SourceBoundArgs):
    text: str = Field("", max_length=200, description="The text to find; empty lists every text on screen.")


class CopilotStopArgs(SourceBoundArgs):
    message: str = Field("", max_length=240, description="A last line for the person (\"Listo\").")


def _copilot_show(svc: Services, a: CopilotShowArgs) -> dict[str, Any]:
    spec: dict[str, Any] = {"steps": a.steps, "message": a.message, "agent": a.agent, "goal": a.goal, "replace": a.replace}
    if a.space:
        spec["space"] = a.space
    return svc.copilot.show(spec, wait_s=a.wait_s, each=a.each, look=a.look)


def _fx(svc: Services, a: EffectArgs) -> dict[str, Any]:
    """The effects a call names (the others off), or the saved ones when it names none."""
    given = {k: v for k, v in a.model_dump(exclude_none=True).items() if k in DEFAULT_ENHANCE}
    base = svc.settings["enhance"]
    if any(k in given for k in EFFECT_KEYS):
        base = dict(base, **OFF)
    return normalize_enhance(given, base)


def _enhance_set(svc: Services, a: EnhanceSetArgs) -> dict[str, Any]:
    changes = a.model_dump(exclude_none=True)
    if not changes:
        raise BairdError("invalid", "Nothing to change.", "Name an effect or enabled=true.")
    svc.apply_settings({"enhance": changes})
    return svc.enhance_status()


def _enhance_file(svc: Services, a: EnhanceFileArgs) -> dict[str, Any]:
    fx = _fx(svc, a)
    if not effects_on(fx):
        raise BairdError("invalid", "Choose at least one effect.", "denoise, upscale, sharpen, interpolate or hdr.")
    return svc.library.enhance_copy(a.name, fx)


def _enhance_benchmark(svc: Services, a: EnhanceBenchArgs) -> dict[str, Any]:
    import uuid
    fx = _fx(svc, a)
    if not effects_on(fx):
        fx = normalize_enhance({"denoise": "medium", "upscale": 2, "sharpen": 0.5, "interpolate": "blend"}, fx)
    work = svc.config.work_dir
    work.mkdir(parents=True, exist_ok=True)
    temp = None
    if a.source == "demo":
        w, h = (1920, 1080) if a.size == "1080p" else (1280, 720)
        temp = sample = demo_sample(svc.ffmpeg or "ffmpeg", work / f"bench-{uuid.uuid4().hex[:8]}.mp4", width=w, height=h,
                                    seconds=max(4, a.frames // 30 + 1))
    elif a.source == "live":
        with svc.engine.request():
            svc.engine.wait_for_picture()
            parts = svc.buffer.since(4)
        if not parts:
            raise BairdError("no_signal", "Nothing is in memory yet.")
        temp = sample = work / f"bench-{uuid.uuid4().hex[:8]}.ts"
        sample.write_bytes(b"".join(parts))
    else:
        sample = svc.library.path_of(a.source)
    try:
        info = svc.enhancer.probe(sample)
        frames = min(a.frames, max(30, int(info["fps"] * (info["duration"] or 4)) - 2))
        busy = svc.stage.flowing
        res = svc.enhancer.benchmark(fx, sample, info, frames)
        if busy:   # the live chain shares the GPU and the CPU with the measurement
            res["note"] = "The live enhancement was running during the measurement: the costs include its load. Turn it off for clean numbers."
        return res
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _region(a: Any) -> Optional[dict[str, Any]]:
    r = getattr(a, "region", None)
    return r.model_dump() if r is not None else None


def _look(svc: Services, a: LookArgs) -> dict[str, Any]:
    shot = svc.library.snapshot("png", _region(a))
    from pathlib import Path
    image = svc.library.image_for_model(Path(shot["path"]), a.max_side)
    out = {"name": shot["name"], "path": shot["path"], "width": shot.get("width"), "height": shot.get("height"), "_image": image}
    if not a.save:
        svc.library.delete(shot["name"])
        out.update(name=None, path=None)
    return out


def send_to_faustus(svc: Services, a: SendArgs) -> dict[str, Any]:
    from . import faustus
    return faustus.send_file(svc.library.path_of(a.name), a.message)


def _look_file(svc: Services, a: LookFileArgs) -> dict[str, Any]:
    path = svc.library.path_of(a.name)
    desc = svc.library.describe(path)
    return {**{k: desc[k] for k in ("name", "kind", "path", "width", "height", "duration")},
            "_image": svc.library.image_for_model(path, a.max_side)}


def _delete(svc: Services, a: DeleteArgs) -> dict[str, Any]:
    if not a.confirm:
        raise BairdError("confirm_required", f"{a.name} would go to the Recycle Bin.", "Repeat with confirm=true if the user asked for it.")
    return svc.library.delete(a.name)


def _settings_set(svc: Services, a: SettingsArgs) -> dict[str, Any]:
    changes = a.model_dump(exclude_none=True)
    if "capture_enabled" in changes and svc.sources.status()["mode"] != "hdmi":
        raise BairdError("source_activation_required", "Local screen activation is controlled in Baird.",
                         "Ask the person to use the source control in Baird.", status=403)
    if not changes:
        raise BairdError("invalid", "Nothing to change.")
    return svc.apply_settings(changes)


def _watch_check(svc: Services, a: WatchIdArgs) -> dict[str, Any]:
    with svc.engine.request():
        return svc.watch.check(a.id, picture=svc.engine.wait_for_picture)


TOOLS: list[Tool] = [
    Tool("source_list", _d("List the selected source, activation and available display identities.",
                           synonyms="listar fuentes, pantallas disponibles"),
         Empty, ann(read_only=True), lambda s, a: s.sources.status(list_displays=True)),
    Tool("source_select", _d("Choose an available source. The person activates a local screen in Baird.",
                             "Returns an undo token if the choice changes.", "elegir fuente, seleccionar pantalla"),
         SourceArgs, ann(), lambda s, a: s.sources.select(a.mode, a.monitor)),
    Tool("source_undo", _d("Undo the choice using its undo_token. A restored local display stays off.",
                           synonyms="deshacer selección de fuente"),
         SourceUndoArgs, ann(), lambda s, a: s.sources.undo(a.undo_token)),
    Tool("capture_status", _d("Report the selected source, picture, resolution, buffer and recording.",
                              synonyms="estado, señal, capturadora"),
         SourceBoundArgs, ann(read_only=True), lambda s, a: s.status()),
    Tool("capture_retry", _d("Retry the selected source while there is demand. The HDMI opening limit still applies."),
         SourceBoundArgs, ann(), lambda s, a: s.engine.retry_now()),
    Tool("screen_look", _d("Return the current image from the selected capture source.",
                           "Saved as a screenshot unless save=false.", "ver pantalla, captura, qué hay en el otro ordenador"),
         LookArgs, ann(), _look, timeout_s=30, capped=False),
    Tool("snapshot_save", _d("Save a screenshot of the selected source to the library.",
                             "With region it saves only that rectangle (a zone capture, named «Captura zona ...»).",
                             "captura, pantallazo, captura de una zona, recortar"),
         SnapshotArgs, ann(draft_safe=True), lambda s, a: s.library.snapshot(a.format, _region(a)), timeout_s=30),
    Tool("clip_save", _d("Save the last N seconds of the selected source as an MP4 clip.",
                         "Instant (copied from memory, no re-encoding).", "clip, últimos segundos, repetición"),
         ClipArgs, ann(draft_safe=True), lambda s, a: s.library.clip(a.seconds), timeout_s=120),
    Tool("record_start", _d("Start recording the selected source (picture and sound).", synonyms="grabar"),
         SourceBoundArgs, ann(draft_safe=True), lambda s, a: s.library.recorder.start()),
    Tool("record_pause", _d("Pause recording. Resume with record_resume for the same target.", synonyms="pausar grabación"),
         RecordingTargetArgs, ann(idempotent=True), lambda s, a: s.library.recorder.pause(target=a.target)),
    Tool("record_resume", _d("Resume the paused recording. Use record_pause to pause again.", synonyms="reanudar grabación"),
         RecordingTargetArgs, ann(idempotent=True), lambda s, a: s.library.recorder.resume(target=a.target), timeout_s=30),
    Tool("record_stop", _d("Stop the recording; the MP4 appears in the library a moment later.", synonyms="parar grabación"),
         RecordingTargetArgs, ann(draft_safe=True), lambda s, a: s.library.recorder.stop(target=a.target)),
    Tool("transcribe_start", _d("Record the screen and transcribe the document locally while it scrolls. Optionally select its region."),
         TranscribeArgs, ann(draft_safe=True), lambda s, a: s.transcriber.start(_region(a), a.detail), timeout_s=30),
    Tool("transcribe_stop", _d("Finish the document and its recording. Poll capture_status.transcription for the HTML path."),
         SourceBoundArgs, ann(draft_safe=True), lambda s, a: s.transcriber.stop()),
    Tool("transcribe_reread", _d("Transcribe a library MP4 or reread a Baird HTML with local UnlimitedOCR. Automatic video mode reconstructs numbered files. Poll capture_status.transcription.detail."),
         RereadArgs, ann(draft_safe=True), lambda s, a: s.transcriber.detail.start(s.library.path_of(a.name), a.mode)),
    Tool("transcribe_cancel_reread", _d("Cancel the current GPU rereading job. The original recording and Windows transcription are retained."),
         Empty, ann(draft_safe=True), lambda s, a: s.transcriber.detail.cancel()),
    Tool("media_list", _d("List screenshots, clips and recordings, newest first, with their paths."),
         ListArgs, ann(read_only=True), lambda s, a: {"items": s.library.list(a.kind, a.limit), "folder": s.settings.media_dir()}),
    Tool("media_look", _d("Look at a saved screenshot, or a frame of a saved video, as an image."),
         LookFileArgs, ann(read_only=True), _look_file, timeout_s=30, capped=False),
    Tool("media_light_copy", _d("Make a smaller copy of a video (720p) for chats with upload limits; runs in the background.",
                                synonyms="comprimir, versión ligera"),
         NameArg, ann(draft_safe=True), lambda s, a: s.library.light_copy(a.name)),
    Tool("media_copy", _d("Put a file on the Windows clipboard so it can be pasted (Ctrl+V) into a chat or a folder.",
                          synonyms="copiar, portapapeles"),
         NameArg, ann(idempotent=True), lambda s, a: s.library.copy_to_clipboard(a.name)),
    Tool("media_reveal", _d("Show a file (or the media folder) in Windows Explorer."),
         RevealArgs, ann(idempotent=True), lambda s, a: s.library.reveal(a.name)),
    Tool("media_rename", _d("Rename a file of the library."), RenameArgs, ann(), lambda s, a: s.library.rename(a.name, a.new_name)),
    Tool("media_delete", _d("Send a file of the library to the Recycle Bin (confirm=true)."), DeleteArgs, ann(destructive=True), _delete),
    Tool("media_search", _d("Find visible text in screenshots, recordings and saved transcripts, or search by name. Video moments include seconds and snippets.",
                            "Every word must appear; case and accents are ignored. Returns the files with a snippet of the matching text; "
                            "`unread` counts screenshots whose text is not read yet (media_ocr / ocr_reindex).",
                            "buscar capturas, buscar por texto, qué captura tenía"),
         SearchArgs, ann(read_only=True), lambda s, a: s.search(a.query, a.limit, a.recording, a.after), timeout_s=30),
    Tool("media_ocr", _d("Read the text of one saved screenshot now (Kafka's OCR) and return it.", synonyms="leer texto, OCR"),
         NameArg, ann(idempotent=True), lambda s, a: s.ocr.read_now(a.name), timeout_s=150),
    Tool("ocr_reindex", _d("Queue every screenshot whose text is missing or out of date for the background OCR; returns the counts."),
         Empty, ann(idempotent=True), lambda s, a: s.ocr.reindex()),
    Tool("send_to_faustus", _d("Open a Faustus chat with a screenshot or clip attached to its message box.",
                               "Returns the chat's url (`<faustus>/studio?s=<id>`); the person chooses the model and sends. "
                               "Faustus is found through the Hub (it may run on a Spark). Videos over 200 MB need media_light_copy first.",
                               "enviar a Faustus, mandar la captura al chat"),
         SendArgs, ann(), lambda s, a: send_to_faustus(s, a), timeout_s=300, capped=False),
    Tool("watch_list", _d("The screen-watch rules, how each is doing (last value, next look, errors) and the latest alerts.",
                          synonyms="vigilar pantalla, avisos"),
         Empty, ann(read_only=True), lambda s, a: s.watch.list()),
    Tool("watch_add", _d("Watch the other computer's screen and alert when something happens.",
                         "kind=change compares the pixels of a zone: mode=change fires when it changes, mode=still fires when a zone that was "
                         "changing stops (a long process finished). kind=text reads the zone with OCR and fires when `text` appears or disappears. "
                         "An alert saves a picture of the zone («Aviso ...»), notifies the person through the Hub and posts `baird.watch` on the family bus.",
                         "avisar cuando acabe, detectar cambios, vigilar un texto"),
         WatchAddArgs, ann(), lambda s, a: s.watch.add(a.model_dump(exclude_unset=True, exclude_none=True, exclude={"source"}))),
    Tool("watch_update", _d("Change a watch rule (or pause it with enabled=false)."), WatchUpdateArgs, ann(idempotent=True),
         lambda s, a: s.watch.update(a.id, a.model_dump(exclude_unset=True, exclude_none=True, exclude={"id", "source"}))),
    Tool("watch_remove", _d("Delete a watch rule."), WatchIdArgs, ann(destructive=True), lambda s, a: s.watch.remove(a.id)),
    Tool("watch_check", _d("Look once now at a rule's zone and say what it sees (it fires the rule if the condition is met).",
                           "The first look of a rule only records the starting picture."),
         WatchIdArgs, ann(), _watch_check, timeout_s=150),
    Tool("watch_events", _d("The latest alerts the watch rules fired, newest first."), WatchEventsArgs, ann(read_only=True),
         lambda s, a: {"events": s.watch.events(a.limit)}),
    Tool("hotkeys_status", _d("The global shortcuts for screenshot and clip: which combinations are registered and the last press.",
                              "Change them with settings_set (hotkey_shot, hotkey_clip, hotkeys_enabled)."),
         Empty, ann(read_only=True), lambda s, a: s.hotkeys.status(s.settings["hotkeys_enabled"])),
    Tool("enhance_status", _d("The GPU enhancement: its settings, what the live chain does now (state, frames per second, speed, dropped data), "
                              "what this machine supports (Vulkan, NVENC), the last cost measurement and the enhanced-copy jobs.",
                              synonyms="mejora de vídeo, superresolución, reducir ruido"),
         Empty, ann(read_only=True), lambda s, a: s.enhance_status(), timeout_s=60),
    Tool("enhance_set", _d("Turn the GPU enhancement on or off and choose its effects: denoise, upscale, sharpen, interpolate, hdr.",
                           "While it is on, the live view, clips and recordings use the enhanced picture; screenshots and screen_look stay the original. "
                           "Each effect has its own switch; changing them restarts the chain for the next picture.",
                           "mejorar la imagen, subir resolución, quitar ruido, más fotogramas, HDR"),
         EnhanceSetArgs, ann(idempotent=True), _enhance_set, timeout_s=60),
    Tool("enhance_benchmark", _d("Measure what each effect costs per frame (ms) on a sample, and whether the whole chain keeps up in real time.",
                                 "source=demo needs no capture; live uses the last seconds of the capture; or a saved video. The effects are the ones "
                                 "named here, else the ones that are on, else a standard set.", "coste por fotograma, medir la mejora"),
         EnhanceBenchArgs, ann(), _enhance_benchmark, timeout_s=600),
    Tool("media_enhance", _d("Make an enhanced copy of a saved video (name \"(mejorado)\") with the effects named (the others off) or, naming none, the ones that are on; background job.",
                             "Needs the effects chosen: denoise, upscale, sharpen, interpolate (mci allowed here), hdr (HEVC HDR10). The original is kept.",
                             "mejorar vídeo, superresolución, interpolar fotogramas"),
         EnhanceFileArgs, ann(draft_safe=True), _enhance_file),
    Tool("copilot_show", _d("Draw steps over the live picture from the selected source and wait for the person.",
                            "Starts a copilot session when there is none. Baird goes from step to step by itself (it watches the screen), so send "
                            "every step you already know in one call. Answers when the plan is done, the person asks for help or writes "
                            "(events help / say), a text you pointed at is not on screen (anchor_missing), a step takes too long (stalled) or "
                            "the session ended (stopped); still_waiting=true means nothing happened yet (call copilot_wait). The answer "
                            "brings the picture when eyes are needed.",
                            "copilot, modo copilot, guíame, guía en directo, tutorial en vivo, marca dónde pulsar, overlay"),
         CopilotShowArgs, ann(), _copilot_show, timeout_s=190, capped=False),
    Tool("copilot_wait", _d("Wait until the person you are guiding needs you (plan done, help, a message, a missing text, the end).",
                            "Call it again while still_waiting is true; stop after a stopped event."),
         CopilotWaitArgs, ann(read_only=True), lambda s, a: s.copilot.wait(a.wait_s, each=a.each, look=a.look), timeout_s=190, capped=False),
    Tool("copilot_look", _d("See a small live image of the selected source. With text=true, also list visible text and its positions.",
                            "Its pixels are the coordinates your next marks are read in. region zooms.", "mirar la pantalla, ver qué hay"),
         CopilotLookArgs, ann(read_only=True),
         lambda s, a: s.copilot.look(max_side=a.max_side, region=a.region, text=a.text, image=a.image), timeout_s=45, capped=False),
    Tool("copilot_find", _d("Find the position of text in the selected source, or list all visible text.",
                            "Coordinates are in the pixels of your last look. Use it to check a label before pointing at it."),
         CopilotFindArgs, ann(read_only=True), lambda s, a: s.copilot.texts(a.text), timeout_s=45),
    Tool("copilot_status", _d("Where the copilot session is: step, what it is waiting for, texts it is still looking for."), SourceBoundArgs,
         ann(read_only=True), lambda s, a: s.copilot.status()),
    Tool("copilot_stop", _d("End the copilot session and clear the marks; message is a last line for the person."),
         CopilotStopArgs, ann(idempotent=True), lambda s, a: s.copilot.stop(a.message)),
    Tool("jobs_list", _d("Background jobs (light copies, enhanced copies) and their state."), Empty, ann(read_only=True),
         lambda s, a: {"jobs": s.library.jobs()}),
    Tool("settings_get", _d("Current settings (clip length, memory, folder, screenshot format, audio input)."), Empty,
         ann(read_only=True), lambda s, a: s.settings.get()),
    Tool("settings_set", _d("Change capture settings. Capture runs on demand unless capture_always_on is enabled."),
         SettingsArgs, ann(idempotent=True), _settings_set),
]

_LIVE_TOOLS = frozenset({
    "capture_retry", "screen_look", "snapshot_save", "clip_save", "record_start", "record_resume",
    "transcribe_start", "watch_check", "copilot_show", "copilot_wait", "copilot_look", "copilot_find",
})
_SOURCE_TOOLS = _LIVE_TOOLS | {
    "capture_status", "record_pause", "record_stop", "transcribe_stop", "watch_add", "watch_update",
    "watch_remove", "copilot_status", "copilot_stop", "enhance_benchmark",
}


def _bind_source(tool: Tool) -> Tool:
    if tool.name not in _SOURCE_TOOLS:
        return tool

    def run(svc, args):
        expected = args.source
        live = tool.name in _LIVE_TOOLS
        if tool.name == "enhance_benchmark":
            if args.source != "live":
                if args.capture_source is not None:
                    raise BairdError("invalid", "capture_source is for a live benchmark.")
                return tool.run(svc, args)
            expected, live = args.capture_source, True
        return call_on_source(svc, expected, lambda: tool.run(svc, args), live=live)

    return replace(tool, run=run)


TOOLS = [_bind_source(tool) for tool in TOOLS]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tool_catalog() -> list[dict[str, Any]]:
    return _catalog(TOOLS)


def call_tool(svc: Services, name: str, arguments: Optional[dict[str, Any]], caller: str = "agent") -> Any:
    return _call_tool(TOOLS_BY_NAME, svc, name, arguments)
