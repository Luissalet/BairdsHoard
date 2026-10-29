"""Tools for assistants. One catalogue drives ``/api/agent/*`` (the MCP bridge), the web UI (``/api/ui/call``) and
``mcp_server.py``. ``screen_look`` returns the picture itself (an MCP image) so an assistant can see the other computer."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .errors import BairdError
from .hoard_link.agentkit import Empty, Tool, ann
from .hoard_link.agentkit import call_tool as _call_tool
from .hoard_link.agentkit import tool_catalog as _catalog
from .hoard_link.agentkit import uncapped  # noqa: F401 - re-exported for the UI routes
from .services import Services

AGENT_INSTRUCTIONS = """Baird's Hoard shows what another computer puts on the HDMI capture device plugged into this PC (an AVerMedia Live Gamer Portable Lite) and keeps the last minutes in memory.
screen_look returns the current picture of the other computer as an image (and saves it as a screenshot); use it to see what the user is looking at on that device. capture_status says whether there is a picture (state running), the input resolution and how many seconds are kept.
clip_save saves the last N seconds as an MP4 (instant, no re-encoding). record_start / record_stop record until stopped. media_list lists the screenshots, clips and recordings (newest first) with their paths; media_look shows a saved screenshot or a frame of a video. media_light_copy makes a smaller copy of a video for chats with upload limits. media_copy puts a file on the Windows clipboard so the user can paste it; media_reveal shows it in Explorer. media_delete sends a file to the Recycle Bin (confirm=true).
Pictures of the other computer can show private information of the user: describe only what the task needs."""


def _d(first: str, detail: str = "", synonyms: str = "") -> str:
    parts = [first]
    if detail:
        parts.append(detail)
    if synonyms:
        parts.append("Sinónimos: " + synonyms)
    return "\n".join(parts)


class LookArgs(BaseModel):
    save: bool = Field(True, description="Also keep it in the library as a screenshot.")
    max_side: int = Field(1568, ge=320, le=3840, description="Longest side of the image returned to the assistant.")


class SnapshotArgs(BaseModel):
    format: Optional[Literal["png", "jpg"]] = Field(None, description="png (exact pixels) or jpg; default from settings.")


class ClipArgs(BaseModel):
    seconds: Optional[float] = Field(None, ge=1, le=600, description="How many seconds back; default from settings (30).")


class ListArgs(BaseModel):
    kind: Literal["all", "image", "video"] = "all"
    limit: int = Field(50, ge=1, le=500)


class NameArg(BaseModel):
    name: str = Field(..., min_length=1, max_length=260, description="File name as media_list shows it.")


class LookFileArgs(NameArg):
    max_side: int = Field(1568, ge=320, le=3840)


class DeleteArgs(NameArg):
    confirm: bool = Field(False, description="Must be true: the file goes to the Recycle Bin.")


class RenameArgs(NameArg):
    new_name: str = Field(..., min_length=1, max_length=200)


class RevealArgs(BaseModel):
    name: Optional[str] = Field(None, max_length=260, description="Select this file; empty opens the folder.")


class SettingsArgs(BaseModel):
    clip_seconds: Optional[int] = Field(None, ge=3, le=600)
    buffer_seconds: Optional[int] = Field(None, ge=10, le=600)
    snapshot_format: Optional[Literal["png", "jpg"]] = None
    audio_input: Optional[Literal["hdmi", "line"]] = None
    capture_enabled: Optional[bool] = None
    media_dir: Optional[str] = Field(None, max_length=500, description="Folder for the files; empty string = default.")
    device: Optional[str] = Field(None, max_length=80)
    light_height: Optional[int] = Field(None, ge=240, le=1080)
    light_kbps: Optional[int] = Field(None, ge=300, le=20000)


def _look(svc: Services, a: LookArgs) -> dict[str, Any]:
    shot = svc.library.snapshot("png")
    from pathlib import Path
    image = svc.library.image_for_model(Path(shot["path"]), a.max_side)
    out = {"name": shot["name"], "path": shot["path"], "width": shot.get("width"), "height": shot.get("height"), "_image": image}
    if not a.save:
        svc.library.delete(shot["name"])
        out.update(name=None, path=None)
    return out


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
    if not changes:
        raise BairdError("invalid", "Nothing to change.")
    return svc.apply_settings(changes)


TOOLS: list[Tool] = [
    Tool("capture_status", _d("Whether the other computer's picture is coming in, its resolution and what is being recorded.",
                              synonyms="estado, señal, capturadora"),
         Empty, ann(read_only=True), lambda s, a: s.status()),
    Tool("screen_look", _d("See the other computer's screen now: returns the current picture as an image.",
                           "Saved as a screenshot unless save=false.", "ver pantalla, captura, qué hay en el otro ordenador"),
         LookArgs, ann(), _look, timeout_s=30, capped=False),
    Tool("snapshot_save", _d("Save a screenshot of the other computer to the library (no image returned).",
                             synonyms="captura, pantallazo"),
         SnapshotArgs, ann(draft_safe=True), lambda s, a: s.library.snapshot(a.format), timeout_s=30),
    Tool("clip_save", _d("Save the last N seconds of the other computer as an MP4 clip.",
                         "Instant (copied from memory, no re-encoding).", "clip, últimos segundos, repetición"),
         ClipArgs, ann(draft_safe=True), lambda s, a: s.library.clip(a.seconds), timeout_s=120),
    Tool("record_start", _d("Start recording the other computer (picture and sound) until record_stop.", synonyms="grabar"),
         Empty, ann(draft_safe=True), lambda s, a: s.library.recorder.start()),
    Tool("record_stop", _d("Stop the recording; the MP4 appears in the library a moment later.", synonyms="parar grabación"),
         Empty, ann(draft_safe=True), lambda s, a: s.library.recorder.stop()),
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
    Tool("jobs_list", _d("Background jobs (light copies) and their state."), Empty, ann(read_only=True),
         lambda s, a: {"jobs": s.library.jobs()}),
    Tool("settings_get", _d("Current settings (clip length, memory, folder, screenshot format, audio input)."), Empty,
         ann(read_only=True), lambda s, a: s.settings.get()),
    Tool("settings_set", _d("Change settings; changing device, audio input or capture on/off restarts capture."),
         SettingsArgs, ann(idempotent=True), _settings_set),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tool_catalog() -> list[dict[str, Any]]:
    return _catalog(TOOLS)


def call_tool(svc: Services, name: str, arguments: Optional[dict[str, Any]], caller: str = "agent") -> Any:
    return _call_tool(TOOLS_BY_NAME, svc, name, arguments)
