
# Objetivos futuros

Nada abierto: los siete objetivos anteriores (atajo global, captura de zona, anotar, enviar a Faustus, mini vista,
OCR con búsqueda y detección de cambios) están hechos; ver README y FAUSTUS.md.


## Captura bajo demanda (12-11-2026)

Implementada tras el incidente del controlador GL310. El dispositivo se libera tras diez segundos sin uso.
La pérdida de señal activa una espera creciente, con un máximo de doce aperturas por hora y aviso visible.
El modo siempre activo es opcional. El Hub omite Baird en el arranque automático. Diseño en [DEVICE.md](docs/DEVICE.md).
