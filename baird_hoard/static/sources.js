'use strict';

// The rest of the application can keep its existing language. The source bar
// follows the browser, or the language supplied by the surrounding workspace.
const sourceControls = (() => {
  const lang = new URLSearchParams(location.search).get('lang') || navigator.language;
  const english = /^en(?:-|$)/i.test(lang);
  const text = english ? {
    source: 'Source', hdmi: 'Capture card', monitor: 'Other display', overlay: 'Overlay',
    display: 'Display', choose: 'Choose a display', noDisplays: 'No displays available',
    settings: 'Confirm in settings',
    enable: 'Start viewing', disable: 'Turn off', off: 'Capture off', looking: 'Viewing',
    startHint: 'Use Start viewing to show this display.', connecting: 'Starting capture…', waiting: 'Waiting for a picture', saving: 'Saving recording…',
    recording: 'Recording', paused: 'Recording paused', idle: 'On standby',
    privacy_blocked: 'Private mode on', application_excluded: 'Excluded app on this display',
    monitor_reselect: 'Choose the display again',
    private: 'Private mode on', excluded: 'Excluded app on this display',
    policy_unavailable: 'Screen privacy settings unavailable', missing_display: 'Choose the display again',
    recording_in_progress: 'Finish the recording before changing source',
    capture_unavailable: 'This source is unavailable', request_failed: 'Could not update the capture source',
  } : {
    source: 'Fuente', hdmi: 'Capturadora', monitor: 'Otra pantalla', overlay: 'Overlay',
    display: 'Pantalla', choose: 'Elige una pantalla', noDisplays: 'No hay pantallas disponibles',
    settings: 'Confirmar en ajustes',
    enable: 'Empezar a mirar', disable: 'Apagar', off: 'Captura apagada', looking: 'Mirando',
    startHint: 'Pulsa Empezar a mirar para ver esta pantalla.', connecting: 'Iniciando captura…', waiting: 'Esperando imagen', saving: 'Guardando grabación…',
    recording: 'Grabando', paused: 'Grabación en pausa', idle: 'En espera',
    privacy_blocked: 'Modo privado activado', application_excluded: 'Hay una aplicación excluida en esta pantalla',
    monitor_reselect: 'Vuelve a elegir la pantalla',
    private: 'Modo privado activado', excluded: 'Hay una aplicación excluida en esta pantalla',
    policy_unavailable: 'Ajustes de privacidad de pantalla no disponibles', missing_display: 'Vuelve a elegir la pantalla',
    recording_in_progress: 'Termina la grabación antes de cambiar de fuente',
    capture_unavailable: 'Esta fuente no está disponible', request_failed: 'No se pudo cambiar la fuente de captura',
  };

  const bar = document.getElementById('source-bar');
  const mode = document.getElementById('source-mode');
  const display = document.getElementById('source-display');
  const toggle = document.getElementById('source-toggle');
  const indicator = document.getElementById('source-indicator');
  const stageIndicator = document.getElementById('source-stage-indicator');
  const failure = document.getElementById('source-failure');
  let status;
  let busy = false;
  let displayKey = '';

  bar.lang = stageIndicator.lang = english ? 'en' : 'es-ES';
  document.getElementById('source-label').textContent = text.source;
  display.setAttribute('aria-label', text.display);
  for (const option of mode.options) option.textContent = text[option.value];

  function update(st) {
    if (!st.source) return;
    status = st;
    const {source, capture, recording} = st;
    const local = source.mode !== 'hdmi';
    const recordState = recording.state;
    const recordingActive = ['recording', 'paused', 'finishing'].includes(recordState);
    const monitors = source.monitors || [];
    const nextDisplayKey = JSON.stringify(monitors);
    if (nextDisplayKey !== displayKey) {
      display.replaceChildren(new Option(monitors.length ? text.choose : text.noDisplays, ''));
      for (const item of monitors) {
        display.add(new Option(item.name + ' · ' + item.width + '×' + item.height +
          ' (' + item.left + ', ' + item.top + ')', item.id));
      }
      displayKey = nextDisplayKey;
    }
    const live = capture.state === 'running';
    const activity = source.blocked || (recordState === 'recording' ? 'recording' :
      recordState === 'paused' ? 'paused' : recordState === 'finishing' ? 'saving' :
      live ? 'looking' : !source.active ? 'off' :
      capture.state === 'starting' ? 'connecting' : capture.state === 'idle' ? 'idle' : 'waiting');
    const message = text[activity] || text.waiting;
    indicator.dataset.activity = stageIndicator.dataset.activity = activity;
    if (indicator.textContent !== message) indicator.textContent = message;
    if (stageIndicator.textContent !== message) stageIndicator.textContent = message;
    stageIndicator.hidden = !local || !['looking', 'recording', 'paused'].includes(activity);
    mode.value = source.mode;
    for (const option of mode.options) option.disabled = !source.modes.includes(option.value);
    mode.disabled = busy || recordingActive;
    const needsSelection = ['missing_display', 'monitor_reselect'].includes(source.blocked);
    display.value = needsSelection ? '' : source.monitor || '';
    display.hidden = !local;
    display.disabled = busy || recordingActive || !monitors.length;
    toggle.hidden = !local;
    toggle.textContent = source.requires_confirmation ? text.settings : source.active ? text.disable : text.enable;
    const chosenDisplay = !needsSelection && monitors.some(item => item.id === source.monitor);
    toggle.disabled = busy || (!source.active && (!chosenDisplay || recordingActive));
    bar.hidden = false;
    if (local) return [message, !chosenDisplay ? text.choose : activity === 'off' ?
      (source.requires_confirmation ? text.settings : text.startHint) : ''];
  }

  async function change(path, body) {
    if (busy || !status) return;
    busy = true;
    failure.hidden = true;
    update(status);
    try {
      await api(path, {method: 'POST', body});
      await refreshStatus();
    } catch (err) {
      await refreshStatus();
      if (status.source.blocked !== err.code) {
        failure.textContent = text[err.code] || text.request_failed;
        failure.hidden = false;
      }
    } finally {
      busy = false;
      update(status);
    }
  }

  mode.addEventListener('change', () =>
    change('/api/sources/select', {mode: mode.value, monitor: mode.value === 'hdmi' ? null : status.source.monitor}));
  display.addEventListener('change', () =>
    change('/api/sources/select', {mode: mode.value, monitor: display.value || null}));
  toggle.addEventListener('click', () => {
    if (status.source.requires_confirmation) openSettings();
    else change('/api/sources/active', {active: !status.source.active});
  });

  return {update};
})();
