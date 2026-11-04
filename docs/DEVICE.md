# How Baird reads the AVerMedia GL310

## The device

- AVerMedia Live Gamer Portable Lite, model GL310, USB `VID_07CA&PID_C835` (USB 2.0). It has an H.264 hardware
  encoder: the PC never receives raw video, it receives an MPEG-2 transport stream.
- USB class `0xFF` (vendor specific): it is **not** a UVC webcam, so Windows shows it with error 28 (no driver) until
  the vendor driver is installed.

## Driver

`GL310_3.7.x.39.zip` from AVerMedia's download storage (`storage.avermedia.com/web_release_www/GL310/`), folder
`Win10_x64`, is an NSIS installer that holds `AVer835_x64.inf`, `AVer330USB.sys`, the catalog `aver330usb.cat`
(signed by *Microsoft Windows Hardware Compatibility Publisher*, so it installs on Windows 11 with Secure Boot), and
the chip firmware `qpvidfwusb.bin` / `qpaudfwusb.bin`. Installing only the INF is enough:

```
pnputil /add-driver AVer835_x64.inf /install      (elevated)
```

After that Device Manager shows *AVerMedia GL310 USB HD Capture Device* (class MEDIA) and DirectShow lists:

- `AVerMedia C835 Capture` (video input category) — pins: analog video in, analog audio in, `Virtual Video Out`
  (`MEDIATYPE_Stream`, vendor subtype `c5c928be-6f6d-42b6-b6e0-43b562d973c1`), `VBI`, `PCM Out`.
- `AVerMedia C835 H264 Encoder` (`KSCATEGORY_ENCODER`).

## Why ordinary capture programs fail

`Virtual Video Out` is a kernel-streaming bridge pin: it only instantiates when connected to the encoder filter that
shares its *medium*. Connecting it to anything else fails with `ERROR_NO_MATCH (0x80070491)`; asking it for
`MEDIASUBTYPE_MPEG2_TRANSPORT` directly fails with `VFW_E_TYPE_NOT_ACCEPTED`. ffmpeg's dshow input does not list the
pin at all (it is not a video pin).

The vendor's "Stream Engine" exposes a virtual camera instead, but its filter keeps the device in a process-wide
singleton: any program that merely enumerates cameras (browsers' video-capture services do this in the background)
can leave it holding the device, and every other program then hangs while opening it. Baird does not use it.

## The graph Baird builds (`native/bairdcap.cs`)

```
crossbar ─► AVerMedia C835 Capture ─► AVerMedia C835 H264 Encoder ─► Sample Grabber ─► Null Renderer
 (HDMI video → decoder,               Virtual Video Out              MPEG-2 TS           (bytes to stdout)
  its related audio → audio decoder)
```

- Crossbar inputs: 0 `Video_SerialDigital` (HDMI), 1 `Video_RGB`, 2 `Audio_Line` (related to HDMI: the HDMI sound),
  3 `Audio_SPDIFDigital`, 4 `Audio_Line` (the 3.5 mm input). Without routing audio output 1 to input 2 the transport
  stream carries no audio packets.
- Output: transport stream, PAT/PMT on 0x00/0x42, **H.264 High@4.0 on PID 0x44**, **AAC-LC 48 kHz stereo on PID
  0x45**, a key frame every second, ~6–7 Mb/s at 1080p30.
- Encoder settings use the property set AVerMedia published for third-party software
  (`AVER_HW_ENCODE_PROPERTY {1bd55918-baf5-4781-8d76-e0a0a5e1d2b8}`). On the GL310 the *get* of the input
  resolution works (used for the status); *set* of bitrate/GOP is accepted but has no effect, so Baird does not offer
  quality settings.

## From transport stream to files and the browser

- `tsbuf.py` cuts the stream into key-frame segments (PAT+PMT prepended to each) and keeps the last N seconds: a clip
  is "the segments since T" remuxed to MP4, a screenshot is the newest segment decoded to its last complete frame.
- `live.py` remuxes to fragmented MP4 with ffmpeg (`-c copy -bsf:a aac_adtstoasc`, `frag_keyframe+default_base_moof`)
  and sends it over a WebSocket to Media Source Extensions. `empty_moov` is not used: it writes the moov before the
  first AAC packet, the audio track then lacks its AudioSpecificConfig and Chrome refuses the stream.
- `aac_adtstoasc` is needed in every MP4 remux: the device's AAC is ADTS.

## When the device stops responding

After a signal loss the GL310 driver can wedge: `IMediaControl.Run` fails with `0x8007001F` (*a device attached to
the system is not functioning*), the helper's process cannot finish exiting because a request is stuck in the driver,
and while it exists the device cannot be opened again (`no AVerMedia encoder filter accepted the device`) or
restarted (`pnputil /restart-device` asks for a reboot). Only unplugging the USB cable (or a reboot) releases it.

What Baird does about it:

- `bairdcap` never waits on the driver to leave: stopping and releasing the graph get four seconds, then the process
  terminates itself.
- The engine reads the helper's output on its own thread and never waits without a limit: a helper that does not
  leave four seconds after an error, sends nothing for 30 s or goes silent for 20 s is killed; one that is still
  there six seconds after the kill marks the device `stuck`. A `run_failed 0x8007001F`, or three failures in a row,
  also count as `stuck`.
- In `stuck` it polls the device list every three seconds and starts again once the device disappears and comes
  back, once the stuck helper is gone, or every minute anyway.
- Every helper runs in a Windows job that is closed with the app, so none outlives Baird; helpers left by an earlier
  run are ended at start-up.
- A helper that cannot exit keeps `bairdcap.exe` open; rebuilding the helper renames it to `bairdcap.old.exe`
  (Windows allows renaming a running program) instead of failing.
