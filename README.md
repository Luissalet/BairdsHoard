# Baird's Hoard

Live view, screenshots, instant clips and recordings of another computer plugged into this PC through an HDMI
capture device, with files you can drag or paste straight into a chat, and an MCP bridge so assistants can look at
that screen too.

Part of the Hoard family (Hoard Hub, Faustus). Runs locally on Windows, port **5232**.

## What it does

| | |
|---|---|
| **Live view** | The other computer's screen in a window, 1080p, with sound, under a second of delay. The device's own H.264/AAC is copied into fragmented MP4 and played by the browser's hardware decoder: no re-encoding, almost no CPU. |
| **Screenshot** (`C`) | The exact current frame as PNG (or JPG), saved in a tenth of a second. |
| **Last seconds** (`B`) | The last 30 s (configurable, up to the memory window) saved as MP4 instantly: the app keeps the recent past in memory, cut on key frames. |
| **Record** (`R`) | Picture and sound until you stop; starts from the key frame before the click. |
| **Recent files** | Every file appears at once in the side list: drag the card into a chat, a mail or a folder (inside the Hoard window this is a real file drag), or press **Copy** and paste with Ctrl+V. **Explorer** shows it, **Light** makes a 720p copy for chats with upload limits, **Delete** sends it to the Recycle Bin. |
| **Assistants** | 16 MCP tools. `screen_look` returns the current picture as an image, so an assistant can see the other computer; `clip_save`, `record_start/stop`, `media_list`, `media_look`, `media_copy`… |

Other keys: `M` sound, `F` full screen (double click on the picture also).

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

## Settings

Capture on/off (off frees the device for other programs), device, audio input (HDMI or line in), screenshot format,
clip length, memory window (10–600 s), media folder, size and bitrate of light copies.

## For assistants

`mcp_server.py` is a stdio MCP bridge (it starts the app when it is not running). Tools: `capture_status`,
`screen_look`, `snapshot_save`, `clip_save`, `record_start`, `record_stop`, `media_list`, `media_look`,
`media_light_copy`, `media_copy`, `media_reveal`, `media_rename`, `media_delete`, `jobs_list`, `settings_get`,
`settings_set`. The same handlers serve the web UI (`POST /api/ui/call`) and `POST /api/agent/call` (Bearer token
from `data/mcp-token`).

## HTTP API

| Route | |
|---|---|
| `GET /api/health` | service, capture state, input resolution |
| `GET /api/status` | capture, recording, jobs, viewers |
| `POST /api/snapshot`, `/api/clip`, `/api/record/start`, `/api/record/stop` | the three buttons |
| `GET /api/media`, `GET /media/{name}`, `GET /api/thumb/{name}` | library, files, thumbnails |
| `POST /api/media/{name}/copy` · `/reveal` · `/light` · `/rename`, `DELETE /api/media/{name}` | file actions |
| `GET/PUT /api/settings`, `GET /api/devices` | settings |
| `WS /api/live` | live fragmented MP4 (`init` JSON, init segment, fragments; `reset` starts a new stream) |

## Tests

```
.venv\Scripts\python -m pytest
```

`BAIRD_FAKE=1` replaces the device with an ffmpeg test pattern (used by the tests and for demos).

## License

MIT. Third-party parts: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
