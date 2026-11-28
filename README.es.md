# Baird's Hoard

[English](README.md)

Ver en directo, capturar, guardar los últimos segundos y grabar otro ordenador conectado a este PC por una capturadora HDMI; los archivos se arrastran o se pegan directamente en un chat, y un puente MCP deja que los asistentes también vean esa pantalla.

Forma parte de la familia Hoard (Hoard Hub, Faustus). Funciona en local en Windows, puerto **5232**.

## Qué es

Baird's Hoard convierte una capturadora HDMI en una herramienta para trabajar con la pantalla de otro ordenador: la ves en directo, mejoras la imagen con una GPU libre si quieres, haces capturas, guardas los últimos segundos, grabas, anotas, buscas el texto de las pantallas capturadas y vigilas cambios. Cada archivo queda listo para arrastrarlo a un chat, y los asistentes tienen las mismas capacidades por MCP. En modo copilot un asistente te guía en directo sobre esa pantalla: marca encima de la imagen dónde pulsar y qué escribir, te dice debajo qué hacer y avanza solo según lo vas haciendo.

## Qué hace

| | |
|---|---|
| **Directo** | La pantalla del otro ordenador en una ventana, 1080p con sonido y menos de un segundo de retardo. Se copia el H.264/AAC que ya codifica la capturadora a MP4 fragmentado y lo reproduce el decodificador por hardware del navegador: sin recodificar, casi sin CPU. |
| **Mejorar** | Cadena opcional en la GPU entre la señal y lo que ves y guardas, con un interruptor por efecto: reducción de ruido, superresolución (×1,5, ×2, ×3), nitidez, más fotogramas (mezcla para el directo, con movimiento compensado para vídeos guardados) y HDR10 para vídeos guardados. Los clips y las grabaciones salen de la imagen mejorada mientras está activa; las capturas y `screen_look` siguen siendo el original. Los efectos usan Vulkan (libplacebo, `nlmeans_vulkan`) y NVENC en la GPU que elijas, con alternativas por CPU; **Medir coste** dice cuántos milisegundos suma cada efecto por fotograma y si la cadena aguanta en tiempo real. Un vídeo guardado tiene su copia mejorada «(mejorado)» desde su tarjeta. |
| **Captura** (`C`) | El fotograma actual exacto en PNG (o JPG), guardado en una décima de segundo. |
| **Últimos segundos** (`B`) | Los últimos 30 s (configurable, hasta la ventana de memoria) en MP4 al instante: la app guarda el pasado reciente en memoria, cortado en fotogramas clave. |
| **Transcribir** | Graba el documento al pasar por él o pulsa **Transcribir grabación** en un MP4 de Recientes. Windows OCR guarda texto e imágenes durante la captura. Con UnlimitedOCR local, la lectura automática distingue código, documentos y esquemas. Cuando se ven la ruta y los números del editor, el informe reúne las líneas por archivo, señala huecos y enlaza los tramos con sus fotogramas. Puedes descargar el TXT de cada archivo o el ZIP con el índice de tiempos. **Revisar texto** permite cotejar y corregir cada vista. Durante la captura, Baird avisa si saltas líneas numeradas, si el texto pierde legibilidad o si avanzas sin solape. Al retroceder, actualiza las líneas pendientes. |
| **Grabar** (`R`) | Imagen y sonido hasta que pares; empieza en el fotograma clave anterior al clic. |
| **Recientes** | Cada archivo aparece al momento en la lista lateral: arrastra la tarjeta a un chat, un correo o una carpeta (en la ventana Hoard es un arrastre de archivo real), o pulsa **Copiar** y pega con Ctrl+V. **Explorador** lo muestra, **Ligera** hace una copia 720p para chats con límite de tamaño y **Borrar** lo manda a la papelera. Los archivos borrados fuera de Baird se quitan del índice de medios al actualizar la lista. |
| **Zona** (`Z`) | Arrastra un rectángulo sobre el directo y guarda solo esa zona. Los asistentes pasan `region`. |
| **Anotar** | Flechas, recuadros y texto sobre una captura; se guarda como archivo nuevo «(anotada)» y el original no se toca. Se puede mandar directo a Faustus. |
| **Enviar a Faustus** | Abre un chat nuevo de Faustus con la captura o el clip adjunto, listo para enviar. |
| **Buscar por texto** | Busca en capturas, transcripciones y texto visible de las grabaciones. La primera búsqueda indexa los informes y activa el OCR de Windows para los vídeos de la biblioteca. El índice se actualiza en segundo plano. Cada resultado incluye el fragmento y los segundos donde aparece. Pulsa un tiempo para abrir el vídeo o, si falta la grabación, su vista en el informe. |
| **Vigilar** | Reglas que avisan cuando una zona cambia (o se queda quieta) o cuando aparece o desaparece un texto; el aviso guarda una captura, emite `baird.watch` en el bus de la familia y notifica por el Hub. |
| **Vista mini** (`P`) | Ventana picture-in-picture siempre encima para vigilar el otro ordenador. |
| **Copilot** | Un asistente te guía en directo mientras sigues trabajando en el otro ordenador. Dibuja encima del directo: un punto rojo donde pulsar, recuadros, flechas para arrastrar, el texto que escribir junto a su campo, las teclas que pulsar. La instrucción de cada paso va de subtítulo debajo de la imagen (dentro de ella en pantalla completa), y al lado hay una conversación donde el asistente te habla y tú le contestas; un interruptor lo lee todo en voz alta. Baird pasa al paso siguiente solo: mira la imagen y lee su texto (el OCR que trae Windows, en este PC), así que al asistente solo se le llama cuando acaba un plan o pulsas **Ayuda**. Una marca puede nombrar el texto al que apunta (`"click": "Guardar"`): Baird lo encuentra, lo sigue si la página se desplaza y lo muestra en cuanto está en pantalla, de modo que se puede mandar un plan entero de una vez. El botón **Copilot** de la barra superior copia la dirección que se le pasa a cualquier chat, `http://127.0.0.1:5232/copilot`: ahí el asistente lee cómo se usa y empieza. |
| **Atajos globales** | Ctrl+Alt+C (captura) y Ctrl+Alt+X (últimos segundos) aunque Baird no tenga el foco; se cambian en Ajustes. Un pequeño ayudante los registra en Windows y termina con la app. |
| **Asistentes** | 40 herramientas MCP. `screen_look` devuelve la imagen actual para que el asistente vea el otro ordenador; `copilot_show`, `copilot_wait`, `copilot_look` y `copilot_find` guían a la persona en directo; también clip, grabación, biblioteca, copia al portapapeles, `enhance_set`, `enhance_status`, `enhance_benchmark`, `media_enhance`… |

Otras teclas: `M` sonido, `F` pantalla completa (o doble clic en la imagen). Mientras guía un copilot: `Espacio` (o `Intro`, `→`) paso hecho, `←` atrás, `H` ayuda.

Si el driver de la capturadora deja de responder (puede pasar cuando el otro ordenador entra en reposo), el estado dice **La capturadora no responde**: desenchufa su USB, espera cinco segundos y vuelve a enchufarlo; Baird reintenta mientras haya demanda de captura.

## Dispositivo

AVerMedia Live Gamer Portable Lite (GL310, USB `07CA:C835`); la Live Gamer Portable (C875) va por el mismo camino. Solo hace falta el driver: `AVer835_x64.inf` (v3.7.64.39, firmado WHQL) del paquete `GL310_3.7.x.39.zip` de AVerMedia. No se usa ninguna aplicación del fabricante. [docs/DEVICE.md](docs/DEVICE.md) explica cómo funciona la captura y por qué los programas de captura habituales no pueden abrir este dispositivo.

## Cómo ejecutarlo

Desde Hoard Hub, con la ficha de Baird's Hoard (arrancar, parar, abrir en una ventana o en una pestaña del navegador).

A mano:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m baird_hoard
```

Necesita ffmpeg en el PATH (o `BAIRD_FFMPEG`). El ayudante nativo `native/bairdcap.exe` se compila la primera vez con el compilador de C# que trae Windows (.NET Framework 4.8).

Los archivos van a `Vídeos\Baird` por defecto (Ajustes → Carpeta). Los ajustes, miniaturas y registros están en `data/`.

La captura se abre bajo demanda: un directo visible, una grabación, una regla de vigilancia activa o una herramienta de imagen pueden abrir la capturadora, y se libera tras 10 segundos sin demanda. Tras perder la señal, el directo ofrece **Reintentar ahora**; cuando el límite horario pausa los reintentos automáticos, **Apertura extra** abre la capturadora una vez más. Un ordenador cuya salida HDMI va protegida (HDCP) se ve en el monitor conectado a la salida de la capturadora, pero no se puede capturar. El Hub omite Baird al arrancar un perfil o al ejecutar **Arrancar todo**. Más en las [notas del dispositivo](docs/DEVICE.md).

## Ajustes

La capturadora física empieza apagada en perfiles nuevos. Actívala en Ajustes desde este PC y confirma el atajo de Windows. Baird recuerda esa elección al reiniciar y conserva el arranque habitual para los perfiles anteriores que usaban HDMI con la capturadora activada. Volver desde otra pantalla u overlay, o apagar **Pantallas de este PC**, apaga la capturadora. Pulsa **Capturadora apagada · Encender** en la barra de fuentes para abrir Ajustes.

La fuente de monitor de Windows es opcional. Captura la pantalla elegida con sonido del sistema en bucle y una pista de micrófono si se activa. El [contrato de fuentes](docs/CAPTURE_SOURCES.md) explica el ajuste de proceso y la API de selección.

Captura activada o no (desactivada libera la capturadora para otros programas), captura siempre activa opcional, dispositivo, entrada de audio (HDMI o línea), formato de captura, duración del clip, ventana de memoria (10–600 s), carpeta de medios, tamaño y bitrate de las copias ligeras, OCR automático, atajos globales y su combinación (con un modificador y distintas entre sí). El copilot tiene su bloque (`copilot`): si un asistente puede dibujar sobre el directo (el interruptor está en el botón Copilot), el tamaño de la imagen que recibe al mirar, los minutos tras los que se cierra una sesión que nadie toca y el idioma en que Windows lee la pantalla.

El productor de monitor requiere un adaptador de privacidad antes de abrir la pantalla o el audio. [Contrato de privacidad](docs/CAPTURE_SOURCES.md)

## Conexión con Faustus y Hoard Hub

El Hub arranca y para la app desde su ficha, y el manifiesto `faustus-plugin.json` la registra en Faustus. Las capturas y clips se pueden mandar a un chat nuevo de Faustus, y los avisos de vigilancia salen por el bus de la familia y por el Hub.

`mcp_server.py` es un puente MCP stdio (arranca la app si no está en marcha). Herramientas:

| Grupo | Herramientas |
|---|---|
| Fuentes | `source_list`, `source_select`, `source_undo` |
| Captura | `capture_status`, `screen_look`, `snapshot_save`, `clip_save`, `record_start`, `record_pause`, `record_resume`, `record_stop` |
| Biblioteca | `media_list`, `media_look`, `media_light_copy`, `media_copy`, `media_reveal`, `media_rename`, `media_delete`, `jobs_list` |
| Texto | `media_search`, `media_ocr`, `ocr_reindex` |
| Faustus | `send_to_faustus` |
| Vigilancia | `watch_list`, `watch_add`, `watch_update`, `watch_remove`, `watch_check`, `watch_events`, `hotkeys_status` |
| Copilot | `copilot_show`, `copilot_wait`, `copilot_look`, `copilot_find`, `copilot_status`, `copilot_stop` |
| Ajustes | `settings_get`, `settings_set` |

`snapshot_save` y `screen_look` aceptan `region`. Los mismos manejadores sirven a la interfaz web (`POST /api/ui/call`) y a `POST /api/agent/call` (token Bearer en `data/mcp-token`).

`source_list` devuelve las fuentes disponibles y la pantalla elegida. `source_select` prepara la fuente y devuelve un token para `source_undo` si cambia la elección. La pantalla local empieza apagada. Para encenderla, usa el interruptor junto a la pantalla elegida en Ajustes de Baird y pulsa el atajo de Windows que aparece. Las herramientas de captura, grabación y copiloto aceptan `source` con `mode` y `monitor` para comprobar qué fuente van a usar. Al apagar la pantalla local se descartan las capturas, los clips y las miradas del copiloto pendientes. Los archivos ya guardados se consultan en la biblioteca. El overlay de escritorio y la integración conjunta del selector y la privacidad se entregan por separado. [Contrato de fuentes para agentes](docs/AGENT_SOURCES.md)

La app lleva además procedimientos de la biblioteca de skills de la familia para los agentes que se conectan (`screen-capture-evidence`, `systematic-debugging`): aparecen en lo que el agente lee al conectarse, se leen con `skills_list` y `skill_read`, se buscan y asignan con `skills_library` y `skills_assign`, y se ofrecen como prompts. Se eligen en `/api/agent/skills/page` o en la pestaña Skills del Hub.

## API HTTP

| Ruta | |
|---|---|
| `GET /api/health` | servicio, estado de la captura, resolución de entrada |
| `GET /api/status` | captura, grabación, trabajos, visores y tiempos de reintento |
| `POST /api/capture/retry` | adelanta un reintento pendiente mientras haya demanda, dentro del límite horario; `?manual=true` (el botón del directo) abre una vez por encima de él |
| `POST /api/snapshot`, `/api/clip`, `/api/record/start`, `/api/record/pause`, `/api/record/resume`, `/api/record/stop` | controles de captura y grabación |
| `GET /api/media`, `GET /media/{name}`, `GET /api/thumb/{name}` | biblioteca, archivos, miniaturas |
| `POST /api/media/{name}/copy` · `/reveal` · `/light` · `/rename`, `DELETE /api/media/{name}` | acciones sobre archivos |
| `POST /api/snapshot` con `{"region": {x,y,w,h,unit}}` | captura de una zona |
| `POST /api/media/{name}/annotate` · `/faustus` · `/ocr` | copia anotada (PNG como data URL), envío a Faustus, lectura de texto |
| `GET /api/search?q=`, `GET /api/ocr/status`, `POST /api/ocr/reindex` | búsqueda por texto en las capturas |
| `GET/POST /api/watch`, `PUT/DELETE /api/watch/{id}`, `POST /api/watch/{id}/check`, `GET /api/watch/events` | reglas de vigilancia |
| `GET /api/hotkeys` | estado del ayudante de atajos globales |
| `GET/PUT /api/settings`, `GET /api/devices` | ajustes |
| `GET /copilot` | lo que lee un asistente para guiar a la persona: cómo están las cosas y cómo se maneja el copilot, en texto plano |
| `POST /copilot/show`, `/copilot/wait`, `/copilot/stop` | mostrar pasos (o solo decir algo), esperar a que la persona necesite al asistente, terminar la sesión; `show` y `wait` responden con los eventos y, cuando hacen falta ojos, `look_url` |
| `GET /copilot/look.jpg`, `GET /copilot/text` | la pantalla en un JPEG pequeño (`?w=`, `?region=x,y,w,h`); cada texto que hay en ella con su sitio (`?q=` busca uno) |
| `GET /copilot/state`, `WS /copilot/ws`, `POST /copilot/user` | lo que dibuja la página, enviado en cada cambio, y los botones de la persona (next, back, help, say, stop) |
| `WS /api/live` | MP4 fragmentado en directo (`init` JSON, segmento de inicio, fragmentos; `reset` empieza un flujo nuevo) |

Un perfil aislado del Hub arranca con una carpeta de medios vacía dentro de sus datos. La captura y los atajos globales vienen desactivados, y la vigilancia en segundo plano permanece parada en ese perfil.

## Pruebas

```
.venv\Scripts\python -m pytest
```

`BAIRD_FAKE=1` sustituye la capturadora por una carta de ajuste de ffmpeg (la usan las pruebas y las demostraciones).

## Estructura del proyecto

| Ruta | Contenido |
|---|---|
| `baird_hoard/` | Aplicación: captura, flujo en directo, biblioteca de medios, OCR, reglas de vigilancia, copilot, atajos y API |
| `baird_hoard/static/` | Interfaz web |
| `native/` | Ayudante `bairdcap` que lee el dispositivo (fuente y binario compilado); `bairdocr.ps1`, el OCR de Windows con el que el copilot lee la pantalla |
| `mcp_server.py` | Puente MCP |
| `docs/DEVICE.md` | Cómo funciona la captura |
| `tests/` | Pruebas |
| `data/` | Ajustes, miniaturas, registros y token de MCP |

## Licencia

MIT. Componentes de terceros en [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
