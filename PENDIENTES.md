# Pendientes, errores y cosas a revisar

Nada abierto.

## Resuelto (29-10-2026)

- La vista en directo no se veía: el fMP4 con `empty_moov` dejaba la pista AAC sin AudioSpecificConfig y Chrome la
  rechazaba («stream parsing failed»). Ahora el moov sale con el primer fragmento.
- Remux a MP4 sin `aac_adtstoasc` fallaba con el AAC en ADTS del dispositivo («Malformed AAC bitstream»).
- El transport stream no llevaba audio hasta enrutar en el crossbar la salida de audio a la entrada asociada al HDMI.
- El atributo `hidden` no ocultaba el aviso del escenario ni la marca de grabación (lo pisaba `display:flex`).
- Cerrar un WebSocket ya cerrado llenaba el log de excepciones.
