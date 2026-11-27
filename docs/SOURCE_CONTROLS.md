# Capture source bar

The source bar offers **Capture card**, **Other display** and **Overlay**. The card keeps its existing capture settings. In the Windows desktop profile, choose a display and open **Confirm in settings**, then turn on capture and approve the displayed shortcut. The same switch stops capture. Changing mode or display leaves capture off. Activation lives in the running Baird instance and is reset on restart. Baird shows text while viewing, recording or blocked by privacy. The source controls follow the browser's language or the workspace's explicit language.

The app checks the family's screen privacy settings before opening a local producer and rechecks during capture. Private mode, an excluded application on the selected display, an unconfirmed policy or a missing display stops local capture. Baird drops the incoming bytes before they reach the capture buffer and clears the live view. If recording, it keeps the portion already captured. If the authority initially cannot confirm permission and subsequently reports private mode or an exclusion for that display, the indicator updates to the confirmed reason. Confirm capture again after resolving the interruption.

## Agent tools

- **source_list** reports the selected mode, screen activation and available display identities.
- **source_select** chooses a mode and an optional display from **source_list**. It gives back an undo token when the selection changes.
- **source_undo** restores that previous selection while the token is current. A restored local source starts off.

The person turns on local viewing in Baird. Existing picture and recording tools work on the selected source once it is available.

## Connecting the screen producers and the Hub

Pass a **ScreenBackend** and a **read_screen_policy** callback when creating **Services**. The backend lists **Display** descriptors, builds a **CaptureSource** from the chosen descriptor, and reports the identities of applications visible on the chosen display. Supply a layout fingerprint alongside the display ID so a replaced or repositioned display requires reselection. The visibility callback returns **None** when it cannot confirm the apps. A policy with configured exclusions then blocks capture.

The Hub callback returns a **ScreenPolicy** with a private-mode flag and excluded application identities. A failed query blocks capture. Baird requests current policy before every transport read, before accepting its result and before each consumer receives a block. The supervisor also checks while the stream is silent. On-demand pictures and clips stay in a temporary directory off the library's file routes. Baird publishes completed files with a rename after the final privacy check and discards pending files when the source is revoked. The Hub adapter supplies **subscribe_screen_policy(display_id, on_revoke)** through Services for push revocation. It calls **on_revoke** synchronously for the selected display when its permission changes to private, excluded, or unknown, before acknowledging the privacy change. An unknown state or lost subscription uses **None**. The subscription returns an unsubscribe callback. Baird latches the block immediately, closes the affected local producer and clears the buffer. Transport reads and consumer calls share a session gate. Baird confirms the revocation after admitted work has left the gate. If a read or delivery fails to drain, the callback raises a timeout and capture stays off. The person must start viewing again. For physical capture, the producer's acquisition guard requires a lease for each hardware read. The Windows parent uses the same scope and authoritative revision for activation, consumer delivery and final publication. The production Hub adapter must coordinate leases across processes. The Hub receives metadata queries. The local producer supplies media directly to Baird.

Use the same producer for Other display and Overlay. The overlay integration consumes the selected mode and display identity to decide where to draw guidance. The capture path goes through **SourceControls** for both modes. Without a backend, the source bar shows no displays. Supply a synthetic producer and a policy double for isolated instances.

## Castellano

La barra ofrece **Capturadora**, **Otra pantalla** y **Overlay**. La capturadora conserva sus ajustes. En el perfil de escritorio de Windows, elige la pantalla, pulsa **Confirmar en ajustes**, activa la captura y confirma con el atajo indicado. El mismo interruptor detiene la captura. Cambiar de modo o pantalla deja la captura apagada. La barra indica si Baird está mirando, grabando o si la privacidad ha detenido la captura.

Si se activa el modo privado, aparece una aplicación excluida, la pantalla cambia o Baird no puede comprobar la política, el visor local se apaga y se vacía la imagen en memoria, aunque la fuente haya dejado de enviar datos. Las capturas y los clips se preparan fuera del listado y de las descargas. Baird publica el archivo completo después de la última comprobación de privacidad y descarta los guardados en curso cuando se corta la fuente. Una grabación conserva el tramo anterior al corte. Baird consulta la política vigente antes de cada lectura del transporte y antes de entregar los datos a cada consumidor. Un aviso del Hub bloquea la fuente, detiene el productor y vacía el búfer de inmediato. Las lecturas y las entregas a consumidores comparten el bloqueo de la sesión. Baird espera a que terminen las lecturas y entregas autorizadas antes de confirmar el corte. Si la política se desconoce, la fuente queda apagada. Después de resolver la causa, vuelve a confirmar la captura.

Los agentes pueden listar las fuentes con **source_list**, elegirlas con **source_select** y volver a la selección anterior con **source_undo**. La activación de la pantalla local se hace desde Baird.

Si la autoridad confirma después el modo privado o una exclusión en esa pantalla, el indicador pasa a mostrar el motivo confirmado. La captura requiere una nueva confirmación.
