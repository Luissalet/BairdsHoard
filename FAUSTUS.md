# Baird's Hoard — what it can and cannot do

App id `baird`, service `bairds-hoard`, port 5232, MCP stdio bridge `mcp_server.py` (27 tools, images as MCP image
content). Connector preset: its own `faustus-plugin.json` (adopted from Faustus with *Add* on the discovered app).

## Can

- Show another computer's screen live (HDMI → AVerMedia GL310 / C875) in the Hoard window or a browser tab, with
  sound, < 1 s delay, no re-encoding.
- Screenshot of the current frame (PNG/JPG) — button, key `C`, `snapshot_save`, or `screen_look` (returns the image
  to the assistant; `save=false` leaves no file).
- Save the last N seconds (default 30, up to the memory window of 10–600 s) as MP4 instantly — `B`, `clip_save`.
- Record picture and sound until stopped — `R`, `record_start` / `record_stop`; a recording stops by itself, keeping
  what was recorded, when the picture is lost.
- Library of screenshots, clips and recordings (`media_list`, newest first, with absolute paths): drag a card out
  (real file drag in the Hoard window, `DownloadURL` in a browser tab), copy the file to the Windows clipboard
  (`media_copy`), show in Explorer, rename, light 720p copy for chats (`media_light_copy`, NVENC or x264), delete to
  the Recycle Bin (`media_delete`, confirm=true).
- Look at a saved file as an image (`media_look`; a video shows its first frame).
- Settings (`settings_get` / `settings_set`): capture on/off, optional `capture_always_on`, device, audio input (HDMI/line), screenshot format, clip
  length, memory window, media folder, light-copy size and bitrate.
- Capture only a zone: drag a rectangle over the live view (key `Z`) or pass `region` (`{x,y,w,h,unit}`, pixels or
  fractions) to `snapshot_save` / `screen_look`.
- Annotate a capture (arrows, boxes, text) in the UI; it is saved as a new file "(anotada)" and the original is never
  touched.
- Send a capture or clip to Faustus (`send_to_faustus`, card button): a new chat opens with the file attached. Faustus
  is found from `BAIRD_FAUSTUS_URL`, the Hub's placement, or this PC (7000, 7001).
- Read the text of screenshots (OCR through Kafka and the Hub, in the background) and search by text
  (`media_search`, `media_ocr`, `ocr_reindex`, search bar). Needs the Hub and Kafka running; otherwise it retries.
- Watch the screen and alert (`watch_*`, "Vigilar" button): rules "something changes in a zone" (threshold %, or
  stays still N s) or "text appears/disappears" (OCR). An alert saves an "Aviso" capture, emits `baird.watch` on the
  family bus and sends a Hub notification.
- Always-on-top mini view (button / key `P`, browser Picture-in-Picture).
- Global shortcuts for capture (default Ctrl+Alt+C) and clip (Ctrl+Alt+X) even without focus, configurable in
  Settings (`hotkeys_status`). A small helper process registers them with Windows and dies with the app.
- Recover by itself after the device is unplugged, the other computer sleeps or the cable is pulled (states
  `no_device`, `no_signal`, `rate_limited`, back to `running` when the picture returns).
- Open capture on demand (live viewer, recording, enabled watch rule or picture tool), then release it after ten
  idle seconds. Consecutive failures back off at 15 s, 1 min, 5 min and 15 min maximum. The `capture_retry` tool
  advances the next attempt with demand and shares the 12-per-hour limit. The Hub skips automatic startup.
- Notice when the device's driver stops responding (state `stuck`, with the hint to unplug and replug the USB cable),
  wait for a stuck helper to exit, then retry if capture is still requested.
  The helper never outlives the app.

## Cannot (by design or by the hardware)

- Change the encoder's bitrate, resolution or frame rate: the GL310 accepts the vendor property but ignores it; the
  stream is what the device sends (1080p30, ~6–7 Mb/s, key frame every second).
- Control the other computer (keyboard/mouse): a capture device only receives a picture.
- Run on anything but Windows (DirectShow/KS capture).
- Share the device with another program while capture is on (turn capture off in Settings to free it).

## Where things are

- Files: `Videos\Baird` (setting `media_dir`). Data: `data/` (settings, media index, thumbnails, logs, `mcp-token`).
- Native helper: `native/bairdcap.cs` → `bairdcap.exe` (built on first start with the Windows C# compiler).
- Hardware notes: `docs/DEVICE.md`.

## Skills for the agents that connect

This app carries procedures from the skill library of the family's workspace for the agents that connect to it: `screen-capture-evidence`, `systematic-debugging`. They are named in what an agent reads on connecting, read with `skills_list` and `skill_read`, searched and assigned with `skills_library` and `skills_assign`, and offered as prompts. The person chooses them on the skills page (`/api/agent/skills/page`) or in the hub's Skills tab; the `skills` block of `faustus-plugin.json` holds what the app asks for by default.
