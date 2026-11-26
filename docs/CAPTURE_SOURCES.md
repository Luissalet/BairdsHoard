# Capture sources / Fuentes de captura

Baird's source feeds H.264 video and AAC audio in an MPEG-2 transport stream to the capture engine. A process-backed source implements `CaptureSource` in `baird_hoard/capture_source.py`. Its command writes binary TS to stdout, with repeated PAT and PMT and a decodable key frame every second. The engine can read optional JSON lines on stderr for `ready`, `signal` and `stats`. Pass a source to `CaptureEngine` when building the services. The engine owns demand and process lifetime, feeds the same `TsBuffer` and notifies the same listeners for every source.

The default is the HDMI helper. Its hourly opening budget, watchdog, retry policy and helper commands apply as before. `BAIRD_FAKE` still enables the original demo and accepts `BAIRD_FAKE_SOURCE` for a looping file. With `BAIRD_SYNTHETIC_CAPTURE=1`, Baird uses a separate test source on demand. It produces a 1280×720 picture at 30 fps, a key frame every 30 frames, and a 440 Hz stereo tone at 48 kHz. The expected coordinates in source pixels are (208, 168) at the centre of the red patch and (1070, 554) at the centre of the green patch.

See [Capture source bar](SOURCE_CONTROLS.md) for source selection, the activation and privacy checks, and the backend contract.

## Castellano

La fuente entrega vídeo H.264 y audio AAC en MPEG-2 TS al motor. El contrato `CaptureSource` pide al proceso que escriba los paquetes binarios por stdout, repita PAT y PMT e incluya un fotograma clave por segundo. Puede publicar metadatos en líneas JSON por stderr. El motor abre y cierra el proceso según la demanda y alimenta el mismo búfer del que se sirven el visor, el copiloto y la biblioteca.

El ayudante HDMI es el predeterminado y conserva sus límites de apertura, vigilancia, reintentos y órdenes. La demo anterior conserva `BAIRD_FAKE` y `BAIRD_FAKE_SOURCE`. El interruptor de arranque `BAIRD_SYNTHETIC_CAPTURE` está apagado por defecto. Al activarlo, la fuente de prueba genera 1280×720 a 30 fotogramas/s con las marcas roja (208, 168) y verde (1070, 554), un fotograma clave por segundo y un tono estéreo de 440 Hz a 48 kHz.
