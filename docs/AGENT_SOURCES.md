# Capture sources for agents

**List, choose, activate.** Start with `source_list` and read `mode`, `modes`, `monitor`, `active`, and `blocked`. Each available monitor has a stable `id`, name and pixel bounds. Listing sources reads metadata only. The default profile uses HDMI. The opt-in Windows monitor profile exposes its monitor inventory and uses the same capture consumers.

Use `source_select(mode, monitor)` with an available mode and the ID of a local display. A changed choice returns `undo_token`. `source_undo(undo_token)` restores the previous selection and leaves a restored local screen off. Undo expires after another source change or a later change to local activation. The person enables the local display in Baird.

**Bind a call to the picture.** Live capture, recording and copilot tools accept `source` as an optional expected selection. Without it the call uses the currently selected source. This example asks for a look on an already active monitor.

```json
{"name":"copilot_look","arguments":{"source":{"mode":"monitor","monitor":"display-id"}}}
```

| Code | Meaning |
| --- | --- |
| `source_off` | The person can turn on the selected local source in Baird. The live handler has not run. |
| `source_mismatch` | The requested mode or display differs from the current choice. Call `source_list` again. |
| `source_changed` | The selection or activation changed during a live call. Start a fresh look. |
| `source_activation_required` | A tool tried to change the local capture setting. The person owns activation. |
| `capture_unavailable` | This build or profile cannot select that mode. |
| `monitor_reselect` | The physical layout no longer matches the saved monitor selection. |

MCP writes require the app's Bearer token. Off and source mismatch checks run before asking the engine for pixels, audio or OCR. The look routes on `/copilot` check the same current source, and turning off or reselecting a monitor expires the cached looks. Capture and copilot status remain available. Pause, stop and ending guidance can finish an existing operation without requesting a fresh picture. `record_stop` accepts the `target` returned by start, as do pause and resume. Library media already saved during an active session is accessed through the existing media tools.

## Integrating the source bar and desktop overlay

- Use the selector's `SourceControls` as `Services.sources` in place of `ProfileSources`. Both controllers expose `status(list_displays=...)`, `select(mode, monitor)` and `undo(token)`. Keep one registration of `source_list`, `source_select` and `source_undo` and keep the source-aware input models and live checks. Add a monotonic `revision` to the shared controller status on selection, activation and block transitions. Use that revision to reject stale undo tokens and in-flight results.
- Use the monitor catalogue to build `ScreenBackend.displays`. Bind its selected identity and layout to `MonitorSource.command_for(selection, settings)` in the producer factory. In the composed app, the monitor flag can expose the backend inventory. The source bar and privacy gate control the local opening. Preserve the monitor worker's quit, metadata readiness and reselection. Shared code touches `services.py`, `capture.py` and `agent_tools.py`.
- Both monitor and overlay use the same capture permission and picture. The overlay follows the selected monitor and the copilot state from its branch. Wire `Copilot.clear_looks()` into local deactivation, topology loss and changes away from a local source before publishing another source. The shared points for this are `copilot.py`, `api/copilot.py` and the services state hook.
- The native overlay and the real Hub policy adapter are separate integration work. The fixed monitor profile and the agent contracts can be exercised with test sources before that integration.
