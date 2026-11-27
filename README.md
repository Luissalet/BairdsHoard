# Baird's Hoard

[Español](README.es.md)

Live view, screenshots, instant clips and recordings of another computer plugged into this PC through an HDMI
capture device, with files you can drag or paste straight into a chat, and an MCP bridge so assistants can look at
that screen too.

Part of the Hoard family (Hoard Hub, Faustus). Runs locally on Windows, port **5232**.

## What it is

Baird's Hoard turns an HDMI capture device into a tool for working with another computer's screen: you watch it live, optionally enhance the picture on a spare GPU, grab screenshots, keep the last seconds, record, annotate, search the text on captured screens and watch for changes. Every file is ready to drag into a chat, and assistants get the same abilities through MCP. In copilot mode an assistant guides you live on that screen: it marks where to click and what to type on top of the picture, tells you what to do under it and moves on by itself as you go.

## What it does

| | |
|---|---|
| **Live view** | The other computer's screen in a window, 1080p, with sound, under a second of delay. A level meter beside the speaker button moves with the captured sound even while muted and says when nothing arrives. The device's own H.264/AAC is copied into fragmented MP4 and played by the browser's hardware decoder: no re-encoding, almost no CPU. |
| **Enhance** | Optional GPU chain between the signal and what you see and save, one switch per effect: noise reduction, upscaling (×1.5, ×2, ×3), sharpening, frame-rate interpolation (blend for the live view, motion-compensated for saved files) and HDR10 for saved files. Clips and recordings come from the enhanced picture while it is on; screenshots and `screen_look` stay the original. The effects run on Vulkan (libplacebo, `nlmeans_vulkan`) and NVENC on one chosen GPU, with CPU fallbacks; **Measure cost** reports the milliseconds each effect adds per frame and whether the chain keeps up in real time. A saved video gets an enhanced "(mejorado)" copy from its card. |
| **Screenshot** (`C`) | The exact current frame as PNG (or JPG), saved in a tenth of a second. |
| **Last seconds** (`B`) | The last 30 s (configurable, up to the memory window) saved as MP4 instantly: the app keeps the recent past in memory, cut on key frames. |
| **Transcribe** | Record a document while scrolling, or transcribe a saved MP4 from Recents. Windows OCR saves text and images during capture. With local UnlimitedOCR configured, automatic reading handles code, documents, and diagrams. Visible file paths and editor line numbers let the report assemble per-file text, mark gaps, and link ranges to their original frames. Download a single file or a ZIP with a timestamp index. Review individual views to compare and edit the text. Live feedback flags skipped numbered lines, loss of legibility and fast scrolling, and clears a gap once you recover it. |
| **Record** (`R`) | Picture and sound until you stop; starts from the key frame before the click. |
| **Recent files** | Every file appears at once in the side list: drag the card into a chat, a mail or a folder (inside the Hoard window this is a real file drag), or press **Copy** and paste with Ctrl+V. **Explorer** shows it, **Light** makes a 720p copy for chats with upload limits, **Delete** sends it to the Recycle Bin. |
| **Zone** (`Z`) | Drag a rectangle over the live view and save only that area (screenshot of a zone). Assistants pass `region`. |
| **Annotate** | Arrows, boxes and text over a screenshot; saved as a new "(anotada)" file, the original is never touched. Optionally send it straight to Faustus. |
| **Send to Faustus** | Opens a new Faustus chat with the screenshot or clip attached, ready to send. |
| **Search by text** | Search screenshots, transcripts and the visible text of library recordings. The first search indexes saved reports and starts local Windows OCR for existing videos. Indexing continues in the background. Each matching timestamp opens the video with an excerpt. If the video is unavailable, open the original view in the report. |
| **Watch** | Rules that alert when a zone changes (or stays still) or when text appears/disappears; an alert saves a picture, emits `baird.watch` on the family bus and notifies through the Hub. |
| **Mini view** (`P`) | Always-on-top Picture-in-Picture window to keep an eye on the other computer. |
| **Copilot** | An assistant guides you live while you keep working on the other computer. It draws on top of the live picture: a red dot where to click, boxes, arrows for a drag, the text to type next to its field, the keys to press. The instruction of each step is a subtitle under the picture (inside it in full screen), and a conversation beside it is where the assistant talks to you and you answer; a switch reads it all aloud. Baird goes to the next step by itself: it watches the picture and reads its text (the OCR that ships with Windows, on this PC), so the assistant is only called when a plan ends or you press **Ayuda**. A mark can name the text it points at (`"click": "Guardar"`): Baird finds it, follows it when the page scrolls and shows it as soon as it is on screen, so a whole plan can be sent at once. The **Copilot** button in the top bar copies the address to hand to any chat, `http://127.0.0.1:5232/copilot`: the assistant reads there how to use it and starts. |
| **Global shortcuts** | Ctrl+Alt+C (screenshot) and Ctrl+Alt+X (last seconds) even when Baird is not focused; configurable in Settings. A small helper registers them with Windows and ends with the app. |
| **Assistants** | MCP tools. `screen_look` returns the current picture as an image, so an assistant can see the other computer; `copilot_show`, `copilot_wait`, `copilot_look`, `copilot_find` guide the person live; `clip_save`, `record_start/pause/resume/stop`, `media_list`, `media_look`, `media_copy`, `enhance_set`, `enhance_status`, `enhance_benchmark`, `media_enhance`… |

Other keys: `M` sound, `F` full screen (double click on the picture also). While a copilot guides: `Space` (or `Enter`, `→`) step done, `←` back, `H` help.

If the device's driver stops responding (it can happen when the other computer goes to sleep), the status shows
**La capturadora no responde**: unplug the device's USB cable, wait five seconds and plug it back in; Baird retries
while capture is requested.

## Hardware

AVerMedia Live Gamer Portable Lite (GL310, USB id `07CA:C835`); the Live Gamer Portable (C875) uses the same path.
Only the device driver is needed — the WHQL-signed `AVer835_x64.inf` (v3.7.64.39) from AVerMedia's
`GL310_3.7.x.39.zip`. No vendor application is used or needed. See [docs/DEVICE.md](docs/DEVICE.md) for how the
capture works and why ordinary capture programs cannot open this device.

## Run

From Hoard Hub: the Baird's Hoard tile (start, stop, open in a window or a browser tab).

By hand:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m baird_hoard
```

Needs ffmpeg on PATH (or `BAIRD_FFMPEG`). The native helper `native/bairdcap.exe` is compiled on first start with
the C# compiler that ships with Windows (.NET Framework 4.8).

Files go to `Videos\Baird` by default (Settings → Folder). Settings, thumbnails and logs live in `data/`.

Capture opens on demand. A visible live view, a recording, an enabled watch rule or a picture tool can open the
device. It closes after 10 seconds without demand. The live view offers **Reintentar ahora** after signal loss.
When the hourly opening limit pauses automatic retries, **Apertura extra** opens the device once more. A computer
whose HDMI output is content-protected (HDCP) shows on a monitor connected to the pass-through but cannot be captured. The Hub skips Baird when it starts a profile at startup or
runs **Start all**. See [device notes](docs/DEVICE.md) for the retry policy.

## Settings

Capture on/off (off frees the device for other programs), optional always-on capture, device, audio input (HDMI or the 3.5 mm line in; the API also takes `spdif` or a crossbar input number), screenshot format,
clip length, memory window (10–600 s), media folder, size and bitrate of light copies, automatic OCR on/off, global
shortcuts on/off and their key combinations (each needs a modifier and must differ). The enhancement has its own block: on/off, clips and recordings from the enhanced picture, one setting per effect and the GPU it runs on (Vulkan and NVENC index, 2 by default). The optional Windows monitor source captures a selected display with system loopback and an optional microphone track. See [capture sources](docs/CAPTURE_SOURCES.md) for the process flag and selection API. The copilot has its own too (`copilot`): whether an assistant may draw over the live picture (the switch is in the Copilot button), the size of the picture it gets when it looks, the minutes after which a session nobody touches ends, and the language Windows reads the screen in.

The optional monitor producer requires an authoritative screen privacy adapter before opening the inputs. [Monitor privacy contract](docs/CAPTURE_SOURCES.md)

## Connection with Faustus and Hoard Hub

The Hub starts and stops the app from its tile and the plugin manifest `faustus-plugin.json` registers it in Faustus. Screenshots and clips can be sent to a new Faustus chat, and watch alerts go out on the family bus and through the Hub.

`mcp_server.py` is a stdio MCP bridge (it starts the app when it is not running). Tools by group:

| Group | Tools |
|---|---|
| Sources | `source_list`, `source_select`, `source_undo` |
| Capture | `capture_status`, `screen_look`, `snapshot_save`, `clip_save`, `record_start`, `record_pause`, `record_resume`, `record_stop` |
| Library | `media_list`, `media_look`, `media_light_copy`, `media_copy`, `media_reveal`, `media_rename`, `media_delete`, `jobs_list` |
| Text | `media_search`, `media_ocr`, `ocr_reindex` |
| Faustus | `send_to_faustus` |
| Watch | `watch_list`, `watch_add`, `watch_update`, `watch_remove`, `watch_check`, `watch_events`, `hotkeys_status` |
| Copilot | `copilot_show`, `copilot_wait`, `copilot_look`, `copilot_find`, `copilot_status`, `copilot_stop` |
| Settings | `settings_get`, `settings_set` |

`snapshot_save` and `screen_look` accept a `region`. The same handlers serve the web UI (`POST /api/ui/call`) and `POST /api/agent/call` (Bearer token
from `data/mcp-token`).

Use `source_list` to read the available modes and the selected display. `source_select` prepares a source and returns a token for `source_undo` when the choice changes. The local screen starts off. The person uses the switch next to the selected display in Baird's Settings. Capture, recording and copilot calls accept an optional `source` object with `mode` and `monitor` to check the expected source. Switching a local screen off discards pending screenshots and copilot looks. Saved library items use the media tools. The live desktop overlay and the combined selector and privacy integration are delivered separately. [Agent source contract](docs/AGENT_SOURCES.md)

This app carries procedures from the skill library of the family's workspace for the agents that connect to it: `screen-capture-evidence`, `systematic-debugging`. They are named in what an agent reads on connecting, read with `skills_list` and `skill_read`, searched and assigned with `skills_library` and `skills_assign`, and offered as prompts. The person chooses them on the skills page (`/api/agent/skills/page`) or in the Hub's Skills tab; the `skills` block of `faustus-plugin.json` holds what the app asks for by default.

## HTTP API

| Route | |
|---|---|
| `GET /api/health` | service, capture state, input resolution |
| `GET /api/status` | capture, recording, jobs, viewers and retry timing |
| `POST /api/capture/retry` | advance a pending retry while there is demand, within the hourly limit; `?manual=true` (the viewer's button) opens once past it |
| `POST /api/snapshot`, `/api/clip`, `/api/record/start`, `/api/record/pause`, `/api/record/resume`, `/api/record/stop` | capture and recording controls |
| `GET /api/media`, `GET /media/{name}`, `GET /api/thumb/{name}` | library, files, thumbnails |
| `POST /api/media/{name}/copy` · `/reveal` · `/light` · `/rename`, `DELETE /api/media/{name}` | file actions |
| `POST /api/snapshot` with `{"region": {x,y,w,h,unit}}` | zone screenshot |
| `POST /api/media/{name}/annotate` · `/faustus` · `/ocr` | annotated copy (PNG data URL), send to Faustus, read text |
| `GET /api/search?q=`, `GET /api/ocr/status`, `POST /api/ocr/reindex` | text search over screenshots |
| `GET/POST /api/watch`, `PUT/DELETE /api/watch/{id}`, `POST /api/watch/{id}/check`, `GET /api/watch/events` | screen watch rules |
| `GET /api/hotkeys` | global shortcut helper status |
| `GET/PUT /api/settings`, `GET /api/devices` | settings (`enhance` is an object; a partial update keeps the other keys) |
| `GET /copilot` | what an assistant reads to guide the person: where things stand and how to drive the copilot, as plain text |
| `POST /copilot/show`, `/copilot/wait`, `/copilot/stop` | show steps (or just say something), wait until the person needs the assistant, end the session; `show` and `wait` answer with the events and, when eyes are needed, `look_url` |
| `GET /copilot/look.jpg`, `GET /copilot/text` | the screen as a small JPEG (`?w=`, `?region=x,y,w,h`); every text on it with its place (`?q=` finds one) |
| `GET /copilot/state`, `WS /copilot/ws`, `POST /copilot/user` | what the page draws, pushed on every change, and the person's buttons (next, back, help, say, stop) |
| `WS /api/live` | live fragmented MP4 (`init` JSON, init segment, fragments; `reset` starts a new stream); the enhanced picture while the enhancement is on, `?view=raw` for the original |

An isolated Hub profile starts with an empty media folder under its data directory. Capture and global shortcuts default to off, and background watchers stay off in this profile.

## Tests

```
.venv\Scripts\python -m pytest
```

`BAIRD_FAKE=1` replaces the device with an ffmpeg test pattern (used by the tests and for demos); `BAIRD_FAKE_SOURCE=<video file>` loops that file instead.

## Project layout

| Path | Contents |
|---|---|
| `baird_hoard/` | Application: capture, live stream, enhancement chain, media library, OCR, watch rules, copilot, hotkeys, API |
| `baird_hoard/static/` | Web interface |
| `native/` | `bairdcap` helper that reads the device (source and compiled binary); `bairdocr.ps1`, the Windows OCR the copilot reads the screen with |
| `mcp_server.py` | MCP bridge |
| `docs/DEVICE.md` | How the capture works |
| `tests/` | Test suite |
| `data/` | Settings, thumbnails, logs and the MCP token |

## License

MIT. Third-party parts: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
