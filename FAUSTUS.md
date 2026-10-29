# Baird's Hoard — what it can and cannot do

App id `baird`, service `bairds-hoard`, port 5232, MCP stdio bridge `mcp_server.py` (16 tools, images as MCP image
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
- Settings (`settings_get` / `settings_set`): capture on/off, device, audio input (HDMI/line), screenshot format, clip
  length, memory window, media folder, light-copy size and bitrate.
- Recover by itself after the device is unplugged, the other computer sleeps or the cable is pulled (states
  `no_device`, `no_signal`, back to `running` when the picture returns).

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
