# Pendientes, errores y cosas a revisar

Nada abierto. Los arreglos del 29-10-2026 (vista en directo fMP4, remux AAC, audio por el crossbar, avisos ocultos, cierre
de WebSocket) están en el commit d5ba419 y no necesitan seguimiento.

## Resuelto (04-11-2026)

- Tras perder la señal a las 00:55, `IMediaControl.Run` falló con `0x8007001F` y el proceso de captura se quedó
  atascado dentro del driver: Baird esperaba a que saliera sin límite y se quedaba para siempre en «No se pudo abrir
  la capturadora». Ahora mata el proceso, pasa al estado `stuck` con la indicación de desenchufar y enchufar el USB y
  arranca solo al reenchufar (ver docs/DEVICE.md, «When the device stops responding»).
