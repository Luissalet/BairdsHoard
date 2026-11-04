# Baird's Hoard

Ver en directo, capturar, guardar los últimos segundos y grabar otro ordenador conectado a este PC por una
capturadora HDMI; los archivos se arrastran o se pegan directamente en un chat, y un puente MCP deja que los
asistentes también vean esa pantalla.

Forma parte de la familia Hoard (Hoard Hub, Faustus). Funciona en local en Windows, puerto **5232**.

## Qué hace

- **Directo**: la pantalla del otro ordenador en una ventana, 1080p con sonido y menos de un segundo de retardo. Se
  copia el H.264/AAC que ya codifica la capturadora (sin recodificar, casi sin CPU).
- **Captura** (`C`): el fotograma actual exacto en PNG (o JPG) en una décima de segundo.
- **Últimos segundos** (`B`): los últimos 30 s (configurable) en MP4 al instante; la app guarda el pasado reciente en
  memoria, cortado en fotogramas clave.
- **Grabar** (`R`): imagen y sonido hasta que pares; empieza en el fotograma clave anterior al clic.
- **Recientes**: cada archivo aparece al momento en la columna derecha. Arrastra la tarjeta a un chat, un correo o una
  carpeta (en la ventana Hoard es un arrastre de archivo real), o pulsa **Copiar** y pega con Ctrl+V. **Explorador**
  lo muestra, **Ligera** hace una copia 720p para chats con límite de tamaño y **Borrar** lo manda a la papelera.
- **Asistentes**: 16 herramientas MCP. `screen_look` devuelve la imagen actual para que el asistente vea el otro
  ordenador; también clip, grabación, lista, copiar al portapapeles…

Otras teclas: `M` sonido, `F` pantalla completa (o doble clic en la imagen).

Si el driver de la capturadora deja de responder (puede pasar cuando el otro ordenador entra en reposo), el estado
dice **La capturadora no responde**: desenchufa su USB, espera cinco segundos y vuelve a enchufarlo; Baird vuelve a
arrancar solo.

## Dispositivo

AVerMedia Live Gamer Portable Lite (GL310, USB `07CA:C835`); la Live Gamer Portable (C875) va por el mismo camino.
Solo hace falta el driver (`AVer835_x64.inf` v3.7.64.39, firmado WHQL, del paquete `GL310_3.7.x.39.zip` de
AVerMedia). No se usa ninguna aplicación del fabricante. Detalles en [docs/DEVICE.md](docs/DEVICE.md).

## Arrancar

Desde Hoard Hub, con la ficha de Baird's Hoard (arrancar, parar, abrir en ventana o en el navegador). Necesita ffmpeg.
El ayudante nativo `native/bairdcap.exe` se compila solo la primera vez con el compilador de C# que trae Windows.

Los archivos van a `Vídeos\Baird` (se cambia en Ajustes → Carpeta).

## Ajustes

Captura activada o no (desactivada libera la capturadora para otros programas), capturadora, audio (HDMI o entrada de
línea), formato de captura, duración del clip, memoria (10–600 s), carpeta, tamaño y bitrate de las copias ligeras.

Licencia MIT.
