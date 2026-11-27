# Baird's Hoard — what it can and cannot do

App id `baird`, service `bairds-hoard`, port 5232, MCP stdio bridge `mcp_server.py` (40 tools, images as MCP image
content). Connector preset: its own `faustus-plugin.json` (adopted from Faustus with *Add* on the discovered app).

## Can

- Show another computer's screen live (HDMI → AVerMedia GL310 / C875) in the Hoard window or a browser tab, with
  sound, < 1 s delay, no re-encoding.
- Screenshot of the current frame (PNG/JPG) — button, key `C`, `snapshot_save`, or `screen_look` (returns the image
  to the assistant; `save=false` leaves no file).
- Save the last N seconds (default 30, up to the memory window of 10–600 s) as MP4 instantly — `B`, `clip_save`.
- Transcribe a document as it scrolls with `transcribe_start` (optional `region`) and `transcribe_stop`. Poll `capture_status.transcription` for progress and the final HTML path. The full recording stays in the library. Views are sampled twice per second and changed pictures are kept with local Windows OCR. Select the document area and scroll with overlap for a cleaner transcript. Optional `detail` values are `document` and `code`. The GPU pass starts after the Windows HTML is saved. Poll `capture_status.transcription.detail`, or use `transcribe_reread(name, mode)` on an existing Baird HTML and `transcribe_cancel_reread` to stop. The code mode expects a crop starting at the code margin. It flags uncertain lines. An unflagged line is still machine-read.
- To enable the local second pass, set `unlimited-ocr.json` in Baird's data directory with absolute `python` and `model` paths and optionally a `gpu` index or list. The existing runtime needs PyTorch CUDA, Transformers with native `UnlimitedOcrForConditionalGeneration`, Pillow, NumPy and SciPy. The model directory contains the official Baidu Unlimited-OCR base weights and tokenizer. The worker loads local files, requests a GPU lease and exits after the job.
- Record picture and sound with `record_start`, `record_pause`, `record_resume` and `record_stop`. The paired pause and resume tools accept the same recording target. Baird saves the active spans to MP4 and keeps the recording when the picture is lost. `R` starts or stops from the page.
- Library of screenshots, clips and recordings (`media_list`, newest first, with absolute paths): drag a card out
  (real file drag in the Hoard window, `DownloadURL` in a browser tab), copy the file to the Windows clipboard
  (`media_copy`), show in Explorer, rename, light 720p copy for chats (`media_light_copy`, NVENC or x264), delete to
  the Recycle Bin (`media_delete`, confirm=true).
- Enhance the picture on a spare GPU (`enhance_set`, `enhance_status`, `enhance_benchmark`, `media_enhance`; the "Mejorar" button in the UI): noise
  reduction (`light` on the CPU, `medium` and `strong` on the GPU), upscaling ×1.5/×2/×3 (up to 3840 px wide), sharpening 0..1, frame-rate
  interpolation (`blend` live, `mci` for saved files, target `interpolate_fps`) and HDR10 (saved files only: HEVC Main10, PQ, BT.2020). Each effect has
  its own setting. While `enabled`, the live view, clips and recordings use the enhanced picture (`record` off keeps them original); screenshots and
  `screen_look` are always the original; a clip whose span the live chain has not covered yet is made from the original and enhanced with the same chain, so it is as long as asked; the WebSocket accepts `?view=raw`. The chain is ffmpeg: Vulkan filters (libplacebo, `nlmeans_vulkan`) and
  NVENC on the GPU in `enhance.gpu` (default 2), x264 and CPU filters when Vulkan or NVENC are missing; what was left out and why is in
  `enhance_status.live.chain.skipped`. `enhance_benchmark` (source `demo`, `live` or a saved video) gives ms per frame of the baseline and of each
  effect and whether the whole chain fits the frame time. `media_enhance` writes "<name> (mejorado).mp4" as a background job (the original stays); with
  effects named it uses only those, with none it uses the saved ones.
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
- Guide the person live on the other computer's screen, copilot mode (`copilot_show`, `copilot_wait`, `copilot_look`, `copilot_find`,
  `copilot_status`, `copilot_stop`; the same over HTTP under `/copilot`, and `GET /copilot` is the page an assistant reads to start: what
  it is, where things stand, how to drive it). `copilot_show` takes steps: `say` (the subtitle under the picture), `hint`, and marks drawn
  over the live picture: `click` / `dbl` / `right` (a red dot), `type` (the text to type, at its field), `keys`, `box`, `arrow` (a drag),
  `note`. A place is coordinates of the last `copilot_look` or a text on screen (`"click": "Guardar"`, `{"find", "nth", "near", "side",
  "dx", "dy"}`): Baird finds it with the OCR of Windows on this PC, follows it when the page scrolls, shows it when it comes on screen
  and says when the same text is there twice (`several_on_screen`) or never comes (`anchor_missing`). Baird ends each step by itself
  (`until`: `auto` = the screen changes around the mark or anywhere but a notification corner, or the typed text shows up; `manual`;
  `{"text"}`, `{"gone"}`, `{"after"}`, `{"change": [x, y, w, h]}`), so one call shows a whole plan and waits (`wait_s`); it answers with
  the events (advanced, done, help, say, stalled, anchor_missing, stopped) and the picture when eyes are needed. `message` is the
  assistant talking in the conversation beside the picture; the person answers there (event `say`), presses Hecho / Atrás / Ayuda /
  Parar, and can have it all read aloud. `copilot_look` returns a small picture (1024 px by default, `region` zooms, `text=true` lists
  every text with its place, `image=false` only the list) and saves nothing. Text that arrives garbled from a Windows console is put
  right. A session nobody touches ends after `copilot.idle_minutes` (15); `copilot.enabled` (the switch in the Copilot button) turns it
  all off.
- Always-on-top mini view (button / key `P`, browser Picture-in-Picture).
- Global shortcuts for capture (default Ctrl+Alt+C) and clip (Ctrl+Alt+X) even without focus, configurable in
  Settings (`hotkeys_status`). A small helper process registers them with Windows and dies with the app.
- Recover by itself after the device is unplugged, the other computer sleeps or the cable is pulled (states
  `no_device`, `no_signal`, `rate_limited`, back to `running` when the picture returns).
- Open capture on demand (live viewer, recording, enabled watch rule or picture tool), then release it after ten
  idle seconds. Consecutive failures back off at 15 s, 1 min, 5 min and 15 min maximum. The `capture_retry` tool
  advances the next attempt with demand and shares the 12-per-hour limit; the viewer's **Apertura extra** button
  opens once past it. The Hub skips automatic startup.
- Notice when the device's driver stops responding (state `stuck`, with the hint to unplug and replug the USB cable),
  wait for a stuck helper to exit, then retry if capture is still requested.
  The helper never outlives the app.

## Cannot (by design or by the hardware)

- Change the encoder's bitrate, resolution or frame rate: the GL310 accepts the vendor property but ignores it; the
  stream is what the device sends (1080p30, ~6–7 Mb/s, key frame every second). The enhancement re-encodes its own copy; the device's stream is untouched.
- Show HDR in the live view (the browser path is SDR H.264) or run `mci` interpolation live (about 400 ms per 720p frame on the CPU; the live chain uses `blend`).
- Control the other computer (keyboard/mouse): a capture device only receives a picture. The copilot shows the person what to do; it
  does not do it.
- Know that a step is done when doing it changes nothing on screen, or only inside a right-hand corner: the person presses Hecho
  (Space). A screen that never goes still (a video playing) also leaves every step to the person.
- Draw the copilot's marks in the mini view (Picture-in-Picture shows only the picture) or read the screen's text off Windows
  (marks by coordinates still work there).
- Run on anything but Windows (DirectShow/KS capture).
- Share the device with another program while capture is on (turn capture off in Settings to free it).

## Where things are

- Files: `Videos\Baird` (setting `media_dir`). Data: `data/` (settings, media index, thumbnails, logs, `mcp-token`).
- Native helper: `native/bairdcap.cs` → `bairdcap.exe` (built on first start with the Windows C# compiler). Screen text for the
  copilot: `native/bairdocr.ps1` (Windows PowerShell 5.1 and `Windows.Media.Ocr`; started on first use, stopped when idle).
- Hardware notes are in `docs/DEVICE.md`. The capture contract, monitor selection, loopback and optional microphone track are in `docs/CAPTURE_SOURCES.md`.

## Skills for the agents that connect

This app carries procedures from the skill library of the family's workspace for the agents that connect to it: `screen-capture-evidence`, `systematic-debugging`. They are named in what an agent reads on connecting, read with `skills_list` and `skill_read`, searched and assigned with `skills_library` and `skills_assign`, and offered as prompts. The person chooses them on the skills page (`/api/agent/skills/page`) or in the hub's Skills tab; the `skills` block of `faustus-plugin.json` holds what the app asks for by default.
