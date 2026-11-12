# Baird's Hoard

[English](README.md)

Ver en directo, capturar, guardar los últimos segundos y grabar otro ordenador conectado a este PC por una capturadora HDMI; los archivos se arrastran o se pegan directamente en un chat, y un puente MCP deja que los asistentes también vean esa pantalla.

Forma parte de la familia Hoard (Hoard Hub, Faustus). Funciona en local en Windows, puerto **5232**.

## Qué es

Baird's Hoard convierte una capturadora HDMI en una herramienta para trabajar con la pantalla de otro ordenador: la ves en directo, haces capturas, guardas los últimos segundos, grabas, anotas, buscas el texto de las pantallas capturadas y vigilas cambios. Cada archivo queda listo para arrastrarlo a un chat, y los asistentes tienen las mismas capacidades por MCP.

## Qué hace

| | |
|---|---|
| **Directo** | La pantalla del otro ordenador en una ventana, 1080p con sonido y menos de un segundo de retardo. Se copia el H.264/AAC que ya codifica la capturadora a MP4 fragmentado y lo reproduce el decodificador por hardware del navegador: sin recodificar, casi sin CPU. |
| **Captura** (`C`) | El fotograma actual exacto en PNG (o JPG), guardado en una décima de segundo. |
| **Últimos segundos** (`B`) | Los últimos 30 s (configurable, hasta la ventana de memoria) en MP4 al instante: la app guarda el pasado reciente en memoria, cortado en fotogramas clave. |
| **Grabar** (`R`) | Imagen y sonido hasta que pares; empieza en el fotograma clave anterior al clic. |
| **Recientes** | Cada archivo aparece al momento en la lista lateral: arrastra la tarjeta a un chat, un correo o una carpeta (en la ventana Hoard es un arrastre de archivo real), o pulsa **Copiar** y pega con Ctrl+V. **Explorador** lo muestra, **Ligera** hace una copia 720p para chats con límite de tamaño y **Borrar** lo manda a la papelera. |
| **Zona** (`Z`) | Arrastra un rectángulo sobre el directo y guarda solo esa zona. Los asistentes pasan `region`. |
| **Anotar** | Flechas, recuadros y texto sobre una captura; se guarda como archivo nuevo «(anotada)» y el original no se toca. Se puede mandar directo a Faustus. |
| **Enviar a Faustus** | Abre un chat nuevo de Faustus con la captura o el clip adjunto, listo para enviar. |
| **Buscar por texto** | Las capturas se leen en segundo plano (OCR vía Hub y Kafka) y la barra de búsqueda las encuentra por las palabras de la pantalla. |
| **Vigilar** | Reglas que avisan cuando una zona cambia (o se queda quieta) o cuando aparece o desaparece un texto; el aviso guarda una captura, emite `baird.watch` en el bus de la familia y notifica por el Hub. |
| **Vista mini** (`P`) | Ventana picture-in-picture siempre encima para vigilar el otro ordenador. |
| **Atajos globales** | Ctrl+Alt+C (captura) y Ctrl+Alt+X (últimos segundos) aunque Baird no tenga el foco; se cambian en Ajustes. Un pequeño ayudante los registra en Windows y termina con la app. |
| **Asistentes** | 27 herramientas MCP. `screen_look` devuelve la imagen actual para que el asistente vea el otro ordenador; también clip, grabación, biblioteca, copia al portapapeles… |

Otras teclas: `M` sonido, `F` pantalla completa (o doble clic en la imagen).

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

La captura se abre bajo demanda: un directo visible, una grabación, una regla de vigilancia activa o una herramienta de imagen pueden abrir la capturadora, y se libera tras 10 segundos sin demanda. Tras perder la señal, el directo ofrece **Reintentar ahora** y muestra el límite horario de aperturas cuando se alcanza. El Hub omite Baird al arrancar un perfil o al ejecutar **Arrancar todo**. Más en las [notas del dispositivo](docs/DEVICE.md).

## Ajustes

Captura activada o no (desactivada libera la capturadora para otros programas), captura siempre activa opcional, dispositivo, entrada de audio (HDMI o línea), formato de captura, duración del clip, ventana de memoria (10–600 s), carpeta de medios, tamaño y bitrate de las copias ligeras, OCR automático, atajos globales y su combinación (con un modificador y distintas entre sí).

## Conexión con Faustus y Hoard Hub

El Hub arranca y para la app desde su ficha, y el manifiesto `faustus-plugin.json` la registra en Faustus. Las capturas y clips se pueden mandar a un chat nuevo de Faustus, y los avisos de vigilancia salen por el bus de la familia y por el Hub.

`mcp_server.py` es un puente MCP stdio (arranca la app si no está en marcha). Herramientas:

| Grupo | Herramientas |
|---|---|
| Captura | `capture_status`, `screen_look`, `snapshot_save`, `clip_save`, `record_start`, `record_stop` |
| Biblioteca | `media_list`, `media_look`, `media_light_copy`, `media_copy`, `media_reveal`, `media_rename`, `media_delete`, `jobs_list` |
| Texto | `media_search`, `media_ocr`, `ocr_reindex` |
| Faustus | `send_to_faustus` |
| Vigilancia | `watch_list`, `watch_add`, `watch_update`, `watch_remove`, `watch_check`, `watch_events`, `hotkeys_status` |
| Ajustes | `settings_get`, `settings_set` |

`snapshot_save` y `screen_look` aceptan `region`. Los mismos manejadores sirven a la interfaz web (`POST /api/ui/call`) y a `POST /api/agent/call` (token Bearer en `data/mcp-token`).

La app lleva además procedimientos de la biblioteca de skills de la familia para los agentes que se conectan (`screen-capture-evidence`, `systematic-debugging`): aparecen en lo que el agente lee al conectarse, se leen con `skills_list` y `skill_read`, se buscan y asignan con `skills_library` y `skills_assign`, y se ofrecen como prompts. Se eligen en `/api/agent/skills/page` o en la pestaña Skills del Hub.

## API HTTP

| Ruta | |
|---|---|
| `GET /api/health` | servicio, estado de la captura, resolución de entrada |
| `GET /api/status` | captura, grabación, trabajos, visores y tiempos de reintento |
| `POST /api/capture/retry` | adelanta un reintento pendiente mientras haya demanda, dentro del límite horario |
| `POST /api/snapshot`, `/api/clip`, `/api/record/start`, `/api/record/stop` | los tres botones |
| `GET /api/media`, `GET /media/{name}`, `GET /api/thumb/{name}` | biblioteca, archivos, miniaturas |
| `POST /api/media/{name}/copy` · `/reveal` · `/light` · `/rename`, `DELETE /api/media/{name}` | acciones sobre archivos |
| `POST /api/snapshot` con `{"region": {x,y,w,h,unit}}` | captura de una zona |
| `POST /api/media/{name}/annotate` · `/faustus` · `/ocr` | copia anotada (PNG como data URL), envío a Faustus, lectura de texto |
| `GET /api/search?q=`, `GET /api/ocr/status`, `POST /api/ocr/reindex` | búsqueda por texto en las capturas |
| `GET/POST /api/watch`, `PUT/DELETE /api/watch/{id}`, `POST /api/watch/{id}/check`, `GET /api/watch/events` | reglas de vigilancia |
| `GET /api/hotkeys` | estado del ayudante de atajos globales |
| `GET/PUT /api/settings`, `GET /api/devices` | ajustes |
| `WS /api/live` | MP4 fragmentado en directo (`init` JSON, segmento de inicio, fragmentos; `reset` empieza un flujo nuevo) |

## Pruebas

```
.venv\Scripts\python -m pytest
```

`BAIRD_FAKE=1` sustituye la capturadora por una carta de ajuste de ffmpeg (la usan las pruebas y las demostraciones).

## Estructura del proyecto

| Ruta | Contenido |
|---|---|
| `baird_hoard/` | Aplicación: captura, flujo en directo, biblioteca de medios, OCR, reglas de vigilancia, atajos y API |
| `baird_hoard/static/` | Interfaz web |
| `native/` | Ayudante `bairdcap` que lee el dispositivo (fuente y binario compilado) |
| `mcp_server.py` | Puente MCP |
| `docs/DEVICE.md` | Cómo funciona la captura |
| `tests/` | Pruebas |
| `data/` | Ajustes, miniaturas, registros y token de MCP |

## Licencia

MIT. Componentes de terceros en [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
