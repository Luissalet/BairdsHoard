"""Decode finite local recordings on demand, retaining the time of each sampled view."""
import io
import struct
import subprocess

from .native import NO_WINDOW, bind_to_app


def recorded_views(path, ffmpeg):
    from PIL import Image
    import numpy as np

    # Resample onto the playback timeline, including videos with variable frame rate.
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
           "-threads", "1", "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", "fps=2:start_time=0:round=up:eof_action=pass",
           "-threads", "1", "-c:v", "bmp", "-pix_fmt", "bgr24", "-f", "image2pipe", "pipe:1"]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=NO_WINDOW) as decoder:
        bind_to_app(decoder)
        try:
            previous = None
            index = 0
            while True:
                header = decoder.stdout.read(54)
                if not header:
                    break
                if len(header) != 54 or header[:2] != b"BM":
                    raise ValueError("FFmpeg entregó un fotograma incompleto.")
                size = struct.unpack_from("<I", header, 2)[0]
                width, height = struct.unpack_from("<ii", header, 18)
                if not (0 < width * abs(height) <= 34_000_000 and 54 <= size <= 103_000_000):
                    raise ValueError("El tamaño del vídeo supera el límite de lectura.")
                payload = decoder.stdout.read(size - 54)
                if len(payload) != size - 54:
                    raise ValueError("El vídeo se interrumpió durante la decodificación.")
                image = Image.open(io.BytesIO(header + payload)).convert("RGB")
                seconds = index / 2
                index += 1
                if index % 120 == 0:
                    print('{"reading":true}', flush=True)
                # Compare against the last emitted frame, including local edits in small tiles.
                small = np.asarray(image.resize((max(1, width//2), max(1, abs(height)//2))).convert("RGB"), dtype=np.int16)
                if previous is not None and previous.shape == small.shape:
                    delta = np.abs(small-previous).max(axis=2)
                    totals = np.add.reduceat(np.add.reduceat(delta, np.arange(0, len(delta), 8), axis=0),
                                             np.arange(0, delta.shape[1], 8), axis=1)
                    if totals.max() < 150:
                        continue
                previous = small
                yield seconds, image
            if decoder.wait() or not index:
                raise ValueError("No se pudo leer la pista de vídeo del MP4.")
        finally:
            decoder.stdout.close()
            if decoder.poll() is None:
                decoder.kill()
                decoder.wait()
