# Copilot on the monitor / Copiloto sobre el monitor

## Castellano

Elige **Overlay**, selecciona una pantalla y pulsa **Empezar a mirar**. Cuando el copiloto muestra un paso, Baird dibuja los puntos, recuadros, flechas, textos y la barra de instrucciones sobre esa pantalla. La barra del overlay muestra los últimos mensajes. La conversación completa y los controles **Hecho**, **Atrás**, **Ayuda** y **Parar** siguen en Baird. Se usan las mismas herramientas `copilot_*` con la capturadora, otra pantalla y el overlay.

**Ctrl+Alt+Mayús+F10** oculta la capa aunque estés trabajando en otra aplicación. En Baird, **Ocultar overlay** y **Mostrar overlay** permiten cambiar su visibilidad durante la guía. Mientras la capa esté oculta, las marcas se ven en el visor de Baird. La capa desaparece al completar el último paso, al parar la guía, al perder la imagen o al cerrar Baird. Si cambian la resolución o la posición de la pantalla, vuelve a elegirla e inicia una guía nueva.

Por defecto, Windows excluye la capa de la captura. Desde el menú Copilot de Baird puedes marcar **Incluir el overlay en la captura** para que la guía también forme parte de la imagen que ve el agente y de la grabación durante esa guía. La exclusión se restablece al acabar la guía, incluso si la siguiente continúa en la misma conversación. También se restablece al ocultar la capa o perder la fuente. Si Windows no aplica la exclusión, Baird deja las marcas en su visor. La capa requiere Windows 10 versión 2004 o posterior.

## English

Choose **Overlay**, select a display, and select **Start viewing**. When the copilot shows a step, Baird draws the dots, boxes, arrows, labels and instruction bar on that display. The overlay bar shows the latest messages. The full conversation and the **Done**, **Back**, **Help** and **Stop** controls stay in Baird. The capture card, other display and overlay use the same `copilot_*` tools.

**Ctrl+Alt+Shift+F10** hides the layer while another app has focus. **Hide overlay** and **Show overlay** in Baird control it during the guide. While the layer is hidden, the marks appear in Baird's viewer. The layer disappears when you finish the final step, stop the guide, lose the picture or close Baird. If the display resolution or position changes, select it again and start a new guide.

Windows excludes the layer from capture by default. Select **Include overlay in capture** in Baird's Copilot menu to include the current guide in the agent's image and in recordings. Capture exclusion is restored when the guide ends, even if the next guide continues the same conversation. Hiding the layer or losing the source also restores exclusion. If Windows cannot apply exclusion, Baird leaves the marks in its viewer. The layer requires Windows 10 version 2004 or later.

## Integration and test mode

The producer continues to supply the unmodified stream. The copilot publishes one session state, including normalized coordinates from the last look. The overlay projects those coordinates onto the selected display's **physical** bounds, including negative desktop origins. The monitor DPI scales the paint dimensions and type. The selected source provides its stable ID and bounds through `Services.sources.status()`.

Set `BAIRD_OVERLAY_TEST=1` to render the same state to a memory buffer. Isolated, demo and synthetic configurations use this buffer automatically. The window test replaces the Windows API before opening or registering anything. A real desktop capture and its capture-exclusion behaviour depend on the installed Windows compositor, capture backend and driver.
