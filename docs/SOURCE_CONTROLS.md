# Capture source bar

The source bar offers **Capture card**, **Other display** and **Overlay**. The card keeps its existing capture settings. For a local source, choose a physical display by its stable identity and press **Start viewing**. **Turn off** stops capture. Selecting another mode or display leaves local capture off. Activation lives in the running Baird instance and is reset on restart. Baird shows text while viewing, recording or blocked by privacy. The source controls follow the browser's language or the workspace's explicit language.

The app checks the family's screen privacy settings before opening a local producer and rechecks during capture. Private mode, an excluded application on the selected display, an unconfirmed policy or a missing display stops local capture. Baird drops the incoming bytes before they reach the capture buffer and clears the live view. If recording, it keeps the portion already captured. Press **Start viewing** again after resolving the interruption.

## Agent tools

- **source_list** reports the selected mode, screen activation and available display identities.
- **source_select** chooses a mode and an optional display from **source_list**. It gives back an undo token when the selection changes.
- **source_undo** restores that previous selection while the token is current. A restored local source starts off.

The person turns on local viewing in Baird. Existing picture and recording tools work on the selected source once it is available.

## Connecting the screen producers and the Hub

Pass a **ScreenBackend** and a **read_screen_policy** callback when creating **Services**. The backend lists **Display** descriptors, builds a **CaptureSource** from the chosen descriptor, and reports the identities of applications visible on the chosen display. Include the layout identity in IDs so a replaced or repositioned display requires reselection. The visibility callback returns **None** when it cannot confirm the apps. A policy with configured exclusions then blocks capture.

The Hub callback returns a **ScreenPolicy** with a private-mode flag and excluded application identities. A failed query blocks capture. The callback is queried before each producer opens, and the engine refreshes the policy and display applicability at 500 ms intervals as capture bytes arrive. The Hub receives metadata queries. The local producer supplies media directly to Baird.

Use the same producer for Other display and Overlay. The overlay integration consumes the selected mode and display identity to decide where to draw guidance. The capture path goes through **SourceControls** for both modes. Without a backend, the source bar shows no displays. Supply a synthetic producer and a policy double for isolated instances.

## Castellano

La barra ofrece **Capturadora**, **Otra pantalla** y **Overlay**. La capturadora conserva sus ajustes. En un modo local, elige la pantalla y pulsa **Empezar a mirar**. **Apagar** detiene la captura. Al cambiar de modo o pantalla, o al reiniciar Baird, la captura local queda apagada. La barra indica si Baird está mirando, grabando o si la privacidad ha detenido la captura.

Si se activa el modo privado, aparece una aplicación excluida, la pantalla cambia o Baird no puede comprobar la política, el visor local se apaga y se vacía la imagen en memoria. Una grabación conserva el tramo anterior al corte. Después de resolver la causa, pulsa **Empezar a mirar** para volver a capturar.

Los agentes pueden listar las fuentes con **source_list**, elegirlas con **source_select** y volver a la selección anterior con **source_undo**. La activación de la pantalla local se hace desde Baird.
