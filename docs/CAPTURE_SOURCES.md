# Capture sources / Fuentes de captura

Baird's source feeds H.264 video and AAC audio in an MPEG-2 transport stream to the capture engine. A process-backed source implements `CaptureSource` in `baird_hoard/capture_source.py`. Its command writes binary TS to stdout, with repeated PAT and PMT and a decodable key frame every second. The engine can read optional JSON lines on stderr for `ready`, `signal` and `stats`. Pass a source to `CaptureEngine` when building the services. The engine owns demand and process lifetime, feeds the same `TsBuffer` and notifies the same listeners for every source.

The default is the HDMI helper. Its hourly opening budget, watchdog, retry policy and helper commands apply as before. `BAIRD_FAKE` still enables the original demo and accepts `BAIRD_FAKE_SOURCE` for a looping file. With `BAIRD_SYNTHETIC_CAPTURE=1`, Baird uses a separate test source on demand. It produces a 1280×720 picture at 30 fps, a key frame every 30 frames, and a 440 Hz stereo tone at 48 kHz. The expected coordinates in source pixels are (208, 168) at the centre of the red patch and (1070, 554) at the centre of the green patch.

## Castellano

La fuente entrega vídeo H.264 y audio AAC en MPEG-2 TS al motor. El contrato `CaptureSource` pide al proceso que escriba los paquetes binarios por stdout, repita PAT y PMT e incluya un fotograma clave por segundo. Puede publicar metadatos en líneas JSON por stderr. El motor abre y cierra el proceso según la demanda y alimenta el mismo búfer del que se sirven el visor, el copiloto y la biblioteca.

El ayudante HDMI es el predeterminado y conserva sus límites de apertura, vigilancia, reintentos y órdenes. La demo anterior conserva `BAIRD_FAKE` y `BAIRD_FAKE_SOURCE`. El interruptor de arranque `BAIRD_SYNTHETIC_CAPTURE` está apagado por defecto. Al activarlo, la fuente de prueba genera 1280×720 a 30 fotogramas/s con las marcas roja (208, 168) y verde (1070, 554), un fotograma clave por segundo y un tono estéreo de 440 Hz a 48 kHz.


## Windows desktop source

The desktop worker starts with the checkout root as its working directory so Python can load the package when Baird runs from source. The HDMI helper starts in `native/`.

The pre-release desktop source is opt-in through the process flag `BAIRD_MONITOR_CAPTURE`. Use `GET /api/monitors` for the current names and physical bounds, and put the returned `selection` in the `monitor` setting. The `monitor_microphone` setting defaults to off. An explicit selection and a capture consumer are required. A layout change stops the stream, clears the buffer and requires a fresh selection. Cloned outputs with ambiguous physical identity are unavailable.

Desktop Duplication uses the adapter and output resolved from the selected display. The encoder tries the hardware path for that adapter when FFmpeg offers it, then uses software if initialization fails before a stream starts. A restarted encoder gets a new transport stream. WASAPI in shared loopback mode reads the default Windows output. An enabled microphone becomes a second stereo AAC track in saved clips and recordings. The viewer plays system audio, and the meters read the captured levels even while playback is muted. [Desktop Duplication](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api) · [WASAPI loopback](https://learn.microsoft.com/en-gb/windows/win32/coreaudio/loopback-recording)

Pause and resume are available on the recording toolbar and through `record_pause` and `record_resume`. Pausing releases the recorder's demand. Resume starts at the next key frame and stop joins the active spans into one MP4. The paired tools accept the same target name, so a delayed pause or resume cannot change another recording.

## Fuente de monitor en Windows

El productor de escritorio se inicia en la raíz del repositorio para que Python encuentre el paquete al ejecutar Baird desde el código fuente. El ayudante HDMI se inicia en `native/`.

La fuente de monitor se activa con el ajuste de proceso `BAIRD_MONITOR_CAPTURE`. `GET /api/monitors` devuelve los nombres, tamaños y posiciones, y una selección que se guarda en el ajuste `monitor`. `monitor_microphone` empieza apagado. La captura se abre al pedir imagen o grabar. Si cambia la disposición, se detiene y hay que volver a elegir monitor.

La captura de escritorio usa la gráfica que alimenta ese monitor. Se intenta codificar por hardware en esa gráfica y se usa software si falla la apertura del codificador. El sonido del sistema se lee por WASAPI en bucle de la salida predeterminada, en modo compartido. Al activar el micrófono, los clips y grabaciones guardan una segunda pista AAC estéreo. El visor reproduce la pista del sistema y muestra los niveles capturados. [Duplicación de escritorio](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api) · [Sonido en bucle](https://learn.microsoft.com/en-gb/windows/win32/coreaudio/loopback-recording)

La grabadora guarda un MP4 con los tramos activos. Se puede pausar y reanudar con los botones del visor o con `record_pause` y `record_resume`. Mientras está en pausa, la grabadora libera su demanda de captura. La reanudación empieza en el siguiente fotograma clave.
