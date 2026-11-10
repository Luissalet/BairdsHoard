# Pendientes, errores y cosas a revisar

## Por comprobar con señal real (10-11-2026)

Se probó todo en modo demo (API, MCP, atajos registrados en Windows, interfaz con Playwright), pero Chromium sin
códec H.264 no reproduce el directo, así que con la capturadora conectada falta ver una vez, en la ventana Hoard:
arrastrar la zona sobre el directo (`Z`), la vista mini (`P`) y pulsar de verdad el atajo global Ctrl+Alt+C.
El atajo de clip por defecto es Ctrl+Alt+X porque Ctrl+Alt+B ya lo usa otro programa del PC (Ajustes lo cambia).

Nada más abierto. Los arreglos del 29-10-2026 (vista en directo fMP4, remux AAC, audio por el crossbar, avisos ocultos, cierre
de WebSocket) están en el commit d5ba419 y no necesitan seguimiento.

## Resuelto (04-11-2026)

- Tras perder la señal a las 00:55, `IMediaControl.Run` falló con `0x8007001F` y el proceso de captura se quedó
  atascado dentro del driver: Baird esperaba a que saliera sin límite y se quedaba para siempre en «No se pudo abrir
  la capturadora». Ahora mata el proceso, pasa al estado `stuck` con la indicación de desenchufar y enchufar el USB y
  arranca solo al reenchufar (ver docs/DEVICE.md, «When the device stops responding»).
