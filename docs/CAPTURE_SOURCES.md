# Capture sources / Fuentes de captura

Baird's source feeds H.264 video and AAC audio in an MPEG-2 transport stream to the capture engine. A process-backed source implements `CaptureSource` in `baird_hoard/capture_source.py`. Its command writes binary TS to stdout, with repeated PAT and PMT and a decodable key frame every second. The engine can read optional JSON lines on stderr for `ready`, `signal` and `stats`. Pass a source to `CaptureEngine` when building the services. The engine owns demand and process lifetime, feeds the same `TsBuffer` and notifies the same listeners for every source.

The default is the HDMI helper. Its hourly opening budget, watchdog, retry policy and helper commands apply as before. `BAIRD_FAKE` still enables the original demo and accepts `BAIRD_FAKE_SOURCE` for a looping file. With `BAIRD_SYNTHETIC_CAPTURE=1`, Baird uses a separate test source on demand. It produces a 1280×720 picture at 30 fps, a key frame every 30 frames, and a 440 Hz stereo tone at 48 kHz. The expected coordinates in source pixels are (208, 168) at the centre of the red patch and (1070, 554) at the centre of the green patch.

See [Capture source bar](SOURCE_CONTROLS.md) for source selection, the activation and privacy checks, and the backend contract.

## Castellano

La fuente entrega vídeo H.264 y audio AAC en MPEG-2 TS al motor. El contrato `CaptureSource` pide al proceso que escriba los paquetes binarios por stdout, repita PAT y PMT e incluya un fotograma clave por segundo. Puede publicar metadatos en líneas JSON por stderr. El motor abre y cierra el proceso según la demanda y alimenta el mismo búfer del que se sirven el visor, el copiloto y la biblioteca.

El ayudante HDMI es el predeterminado y conserva sus límites de apertura, vigilancia, reintentos y órdenes. La demo anterior conserva `BAIRD_FAKE` y `BAIRD_FAKE_SOURCE`. El interruptor de arranque `BAIRD_SYNTHETIC_CAPTURE` está apagado por defecto. Al activarlo, la fuente de prueba genera 1280×720 a 30 fotogramas/s con las marcas roja (208, 168) y verde (1070, 554), un fotograma clave por segundo y un tono estéreo de 440 Hz a 48 kHz.


## Windows desktop source

The desktop worker starts with the checkout root as its working directory so Python can load the package when Baird runs from source. See the source bar guide for choosing and confirming a local display. The HDMI helper starts in `native/`.

The pre-release desktop source is opt-in through the process flag `BAIRD_MONITOR_CAPTURE`. Use `GET /api/monitors` for the current names and physical bounds, and put the returned `selection` in the `monitor` setting. The `monitor_microphone` setting defaults to off. An explicit selection and a capture consumer are required. A layout change stops the stream, clears the buffer and requires a fresh selection. Cloned outputs with ambiguous physical identity are unavailable.

Desktop Duplication uses the adapter and output resolved from the selected display. The producer acquires desktop frames independently of audio. PCM is drained independently and drives the input clock. When the desktop is unchanged or the encoder is behind, an empty video slot preserves elapsed time without encoding another full frame. The video has variable cadence up to 30 fps and the audio retains continuous timestamps. The source waits for a usable picture and an audio packet before it reports ready. The encoder tries the hardware path for that adapter when FFmpeg offers it, then uses software if initialization fails before a stream starts. A restarted encoder gets a new transport stream. WASAPI in shared loopback mode reads the default Windows output. An enabled microphone becomes a second stereo AAC track in saved clips and recordings. The viewer plays system audio, and the meters read the captured levels even while playback is muted. [Desktop Duplication](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api) · [WASAPI loopback](https://learn.microsoft.com/en-gb/windows/win32/coreaudio/loopback-recording)

While the viewer page is open and the selected local source is armed, the audio meter keeps the producer active even in a background tab. The helper measures the captured samples every 50 ms on the acquisition thread. The audio meter reaches `/api/status` and the meter WebSocket while the video is still starting or the encoding pipe is backed up. Capture permission applies to both streams. A level expires after 250 ms without a fresh sample and resets when capture stops.

The encoder preference is hardware on the selected display adapter (`h264_qsv` on Intel, `h264_nvenc` on NVIDIA, `h264_amf` on AMD), then `libx264` if the hardware encoder cannot open before the stream starts. This keeps encoding on the display's adapter and avoids a second upload to a different graphics card. Diagnostic runs that force software measure only the software path.

An installed profile can use `python -m baird_hoard.production_policy --profile <data-dir> bind --talos-db <database> --hub-root <hub-root> --backup <new-backup>`. This makes an online SQLite backup and saves the absolute paths in the Baird profile. Baird prepares the selected display identity and layout only after the person confirms the local shortcut. Talos must already have a saved private-mode value. The worker receives the same database and library paths, so both the selector and worker use the same authority. With Baird stopped, `python -m baird_hoard.production_policy --profile <data-dir> remove` removes the binding and the settled integration tables and triggers. Talos preferences, exclusions and media remain in the database.

Each `AcquireNextFrame` and each WASAPI `GetBuffer` requires a lease confirmed against the current revision and selected display. The authority orders cutoff with outstanding acquisitions, including the audio helper's acknowledgement of the physical call. The worker retains the lease until the helper acknowledges or its exit is confirmed. An unavailable policy blocks the source before acquisition. Trusted launchers can still supply an explicit `BAIRD_MONITOR_POLICY_PROVIDER` as `module:factory`.

At cutoff the worker notifies the engine, drops pending transport, and closes both inputs. Baird empties the live buffer and keeps the already captured part of an open recording. Recovery requires turning the local capture switch off, resolving the privacy condition and turning it on again. Physical validation and the cost of the texture readback remain pending.

On normal shutdown the worker waits for admitted display pulls and audio acknowledgements to settle. With the Hub's SQLite authority, an atomic publication checks the same scope and revision without issuing a native acquisition grant.

Pause and resume are available on the recording toolbar and through `record_pause` and `record_resume`. Pausing releases the recorder's demand. Resume starts at the next key frame and stop joins the active spans into one MP4. The paired tools accept the same target name, so a delayed pause or resume cannot change another recording.

A background tab disconnects its live viewer to release its capture demand. Picture-in-Picture keeps the viewer connected. An active recording holds its own demand and continues with the tab hidden, with the tab closed or without a viewer. With no remaining consumer, the source returns to idle.

## Fuente de monitor en Windows

El productor de escritorio se inicia en la raíz del repositorio para que Python encuentre el paquete al ejecutar Baird desde el código fuente. El ayudante HDMI se inicia en `native/`.

La fuente de monitor se activa con el ajuste de proceso `BAIRD_MONITOR_CAPTURE`. `GET /api/monitors` devuelve los nombres, tamaños y posiciones, y una selección que se guarda en el ajuste `monitor`. `monitor_microphone` empieza apagado. La captura se abre al pedir imagen o grabar. Si cambia la disposición, se detiene y hay que volver a elegir monitor.

La captura de escritorio usa la gráfica que alimenta ese monitor. El productor toma la imagen por separado del sonido. El reloj del PCM avanza por separado. Si la pantalla sigue estática o el codificador se retrasa, un intervalo de vídeo vacío conserva el tiempo sin codificar otra imagen entera. El vídeo tiene frecuencia variable hasta 30 fps y se conserva la continuidad temporal del audio. Antes de anunciar la fuente disponible, se comprueba que hay imagen utilizable y un paquete de audio. Se intenta codificar por hardware en esa gráfica y se usa software si falla la apertura del codificador. El sonido del sistema se lee por WASAPI en bucle de la salida predeterminada, en modo compartido. Al activar el micrófono, los clips y grabaciones guardan una segunda pista AAC estéreo. El visor reproduce la pista del sistema y muestra los niveles capturados. [Duplicación de escritorio](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api) · [Sonido en bucle](https://learn.microsoft.com/en-gb/windows/win32/coreaudio/loopback-recording)

Mientras la página del visor siga abierta y la fuente local esté activada, el medidor mantiene activo el productor también con la pestaña en segundo plano. El ayudante mide las muestras capturadas cada 50 ms en el hilo de adquisición. El nivel llega a `/api/status` y al WebSocket del medidor aunque el vídeo esté arrancando o la tubería del codificador se atasque. El permiso de captura se aplica a ambas pistas. El nivel caduca tras 250 ms sin muestras nuevas y vuelve a cero al parar.

Se intenta primero el codificador de la gráfica de la pantalla seleccionada (`h264_qsv` en Intel, `h264_nvenc` en NVIDIA o `h264_amf` en AMD). Si falla antes de publicar el directo, se usa `libx264`. Conservar la misma gráfica evita subir las imágenes a una segunda tarjeta. Una prueba que fuerce software mide únicamente la ruta de CPU.

En una instalación, el comando de mantenimiento `baird_hoard.production_policy bind` guarda en el perfil las rutas absolutas de la base Pantalla de Talos y del proveedor del Hub. Primero crea una copia SQLite. Baird prepara la identidad y la disposición del monitor elegido cuando la persona confirma el atajo local. Talos debe tener un valor guardado para modo privado. El selector y el productor usan la misma base y biblioteca. Con Baird detenido, `baird_hoard.production_policy remove` retira el enlace y las tablas y disparadores de la integración una vez liquidados los permisos. Se conservan los ajustes, las exclusiones y los medios de Talos.

El productor pide permiso antes de cada fotograma y cada bloque de audio. La autoridad ordena el corte con las adquisiciones pendientes. El ayudante conserva el permiso hasta su acuse o hasta que se confirma su salida. Si la política falta o resulta inaccesible, la fuente se bloquea antes de abrir las entradas. Los lanzadores de confianza aún pueden inyectar un proveedor mediante `BAIRD_MONITOR_POLICY_PROVIDER`. Al cortar, se vacía el directo y se conserva el tramo ya grabado. Para volver a mirar, apaga la fuente, resuelve la causa y vuelve a confirmar la captura.

En un cierre normal, el productor espera a que se liquiden las lecturas de vídeo admitidas y los acuses de audio. Con la autoridad SQLite del Hub, la publicación atómica comprueba ese ámbito y esa revisión sin emitir otro permiso de adquisición.

La grabadora guarda un MP4 con los tramos activos. Se puede pausar y reanudar con los botones del visor o con `record_pause` y `record_resume`. Mientras está en pausa, la grabadora libera su demanda de captura. La reanudación empieza en el siguiente fotograma clave.

Al pasar a segundo plano, la pestaña desconecta su visor y libera su demanda de captura. El minivisor de imagen en imagen mantiene la conexión. Una grabación activa mantiene su propia demanda y continúa con la pestaña en segundo plano, cerrada o sin visor. La fuente vuelve a reposo cuando deja de haber consumidores.
