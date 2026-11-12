# Baird's Hoard

[Español](README.es.md)

Live view, screenshots, instant clips and recordings of another computer plugged into this PC through an HDMI
capture device, with files you can drag or paste straight into a chat, and an MCP bridge so assistants can look at
that screen too.

Part of the Hoard family (Hoard Hub, Faustus). Runs locally on Windows, port **5232**.

## What it is

Baird's Hoard turns an HDMI capture device into a tool for working with another computer's screen: you watch it live, grab screenshots, keep the last seconds, record, annotate, search the text on captured screens and watch for changes. Every file is ready to drag into a chat, and assistants get the same abilities through MCP.

## What it does

| | |
|---|---|
| **Live view** | The other computer's screen in a window, 1080p, with sound, under a second of delay. The device's own H.264/AAC is copied into fragmented MP4 and played by the browser's hardware decoder: no re-encoding, almost no CPU. |
| **Screenshot** (`C`) | The exact current frame as PNG (or JPG), saved in a tenth of a second. |
| **Last seconds** (`B`) | The last 30 s (configurable, up to the memory window) saved as MP4 instantly: the app keeps the recent past in memory, cut on key frames. |
| **Record** (`R`) | Picture and sound until you stop; starts from the key frame before the click. |
| **Recent files** | Every file appears at once in the side list: drag the card into a chat, a mail or a folder (inside the Hoard window this is a real file drag), or press **Copy** and paste with Ctrl+V. **Explorer** shows it, **Light** makes a 720p copy for chats with upload limits, **Delete** sends it to the Recycle Bin. |
| **Zone** (`Z`) | Drag a rectangle over the live view and save only that area (screenshot of a zone). Assistants pass `region`. |
| **Annotate** | Arrows, boxes and text over a screenshot; saved as a new "(anotada)" file, the original is never touched. Optionally send it straight to Faustus. |
| **Send to Faustus** | Opens a new Faustus chat with the screenshot or clip attached, ready to send. |
| **Search by text** | Screenshots are read in the background (OCR through the Hub and Kafka); the search bar finds them by the words on screen. |
| **Watch** | Rules that alert when a zone changes (or stays still) or when text appears/disappears; an alert saves a picture, emits `baird.watch` on the family bus and notifies through the Hub. |
| **Mini view** (`P`) | Always-on-top Picture-in-Picture window to keep an eye on the other computer. |
| **Global shortcuts** | Ctrl+Alt+C (screenshot) and Ctrl+Alt+X (last seconds) even when Baird is not focused; configurable in Settings. A small helper registers them with Windows and ends with the app. |
| **Assistants** | 27 MCP tools. `screen_look` returns the current picture as an image, so an assistant can see the other computer; `clip_save`, `record_start/stop`, `media_list`, `media_look`, `media_copy`… |

Other keys: `M` sound, `F` full screen (double click on the picture also).

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
device. It closes after 10 seconds without demand. The live view offers **Reintentar ahora** after signal loss and
shows the hourly opening limit when it is reached. The Hub skips Baird when it starts a profile at startup or
runs **Start all**. See [device notes](docs/DEVICE.md) for the retry policy.

## Settings

Capture on/off (off frees the device for other programs), optional always-on capture, device, audio input (HDMI or line in), screenshot format,
clip length, memory window (10–600 s), media folder, size and bitrate of light copies, automatic OCR on/off, global
shortcuts on/off and their key combinations (each needs a modifier and must differ).

## Connection with Faustus and Hoard Hub

The Hub starts and stops the app from its tile and the plugin manifest `faustus-plugin.json` registers it in Faustus. Screenshots and clips can be sent to a new Faustus chat, and watch alerts go out on the family bus and through the Hub.

`mcp_server.py` is a stdio MCP bridge (it starts the app when it is not running). Tools by group:

| Group | Tools |
|---|---|
| Capture | `capture_status`, `screen_look`, `snapshot_save`, `clip_save`, `record_start`, `record_stop` |
| Library | `media_list`, `media_look`, `media_light_copy`, `media_copy`, `media_reveal`, `media_rename`, `media_delete`, `jobs_list` |
| Text | `media_search`, `media_ocr`, `ocr_reindex` |
| Faustus | `send_to_faustus` |
| Watch | `watch_list`, `watch_add`, `watch_update`, `watch_remove`, `watch_check`, `watch_events`, `hotkeys_status` |
| Settings | `settings_get`, `settings_set` |

`snapshot_save` and `screen_look` accept a `region`. The same handlers serve the web UI (`POST /api/ui/call`) and `POST /api/agent/call` (Bearer token
from `data/mcp-token`).

This app carries procedures from the skill library of the family's workspace for the agents that connect to it: `screen-capture-evidence`, `systematic-debugging`. They are named in what an agent reads on connecting, read with `skills_list` and `skill_read`, searched and assigned with `skills_library` and `skills_assign`, and offered as prompts. The person chooses them on the skills page (`/api/agent/skills/page`) or in the Hub's Skills tab; the `skills` block of `faustus-plugin.json` holds what the app asks for by default.

## HTTP API

| Route | |
|---|---|
| `GET /api/health` | service, capture state, input resolution |
| `GET /api/status` | capture, recording, jobs, viewers and retry timing |
| `POST /api/capture/retry` | advance a pending retry while there is demand, within the hourly limit |
| `POST /api/snapshot`, `/api/clip`, `/api/record/start`, `/api/record/stop` | the three buttons |
| `GET /api/media`, `GET /media/{name}`, `GET /api/thumb/{name}` | library, files, thumbnails |
| `POST /api/media/{name}/copy` · `/reveal` · `/light` · `/rename`, `DELETE /api/media/{name}` | file actions |
| `POST /api/snapshot` with `{"region": {x,y,w,h,unit}}` | zone screenshot |
| `POST /api/media/{name}/annotate` · `/faustus` · `/ocr` | annotated copy (PNG data URL), send to Faustus, read text |
| `GET /api/search?q=`, `GET /api/ocr/status`, `POST /api/ocr/reindex` | text search over screenshots |
| `GET/POST /api/watch`, `PUT/DELETE /api/watch/{id}`, `POST /api/watch/{id}/check`, `GET /api/watch/events` | screen watch rules |
| `GET /api/hotkeys` | global shortcut helper status |
| `GET/PUT /api/settings`, `GET /api/devices` | settings |
| `WS /api/live` | live fragmented MP4 (`init` JSON, init segment, fragments; `reset` starts a new stream) |

## Tests

```
.venv\Scripts\python -m pytest
```

`BAIRD_FAKE=1` replaces the device with an ffmpeg test pattern (used by the tests and for demos).

## Project layout

| Path | Contents |
|---|---|
| `baird_hoard/` | Application: capture, live stream, media library, OCR, watch rules, hotkeys, API |
| `baird_hoard/static/` | Web interface |
| `native/` | `bairdcap` helper that reads the device (source and compiled binary) |
| `mcp_server.py` | MCP bridge |
| `docs/DEVICE.md` | How the capture works |
| `tests/` | Test suite |
| `data/` | Settings, thumbnails, logs and the MCP token |

## License

MIT. Third-party parts: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
