/* Passive status of the engine-owned Hermes connection pool. */
(() => {
  const element = document.querySelector('#hermes-gateway-status');
  const label = element.querySelector('.gateway-label');
  const labels = {
    waiting: 'gw_waiting', connecting: 'gw_connecting', connected: 'gw_connected',
    offline: 'gw_offline', error: 'gw_error', restart_required: 'gw_restart_required',
    unknown: 'gw_unknown',
  };
  let mode = null, state = 'unknown', received = 0, request = null, generation = 0;
  let engine = document.querySelector('#conn').dataset.state;
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;
  function render() {
    element.hidden = !allowed() || mode === null || mode === 'subprocess';
    const current = engine !== 'connected' || Date.now() - received > 15000 ? 'unknown' : state;
    element.dataset.state = current;
    label.textContent = window.t(labels[current] || labels.unknown);
  }
  async function refresh() {
    if (!allowed()) {
      generation += 1;
      request?.abort();
      mode = null; received = 0; render(); return;
    }
    if (request || engine !== 'connected' || document.hidden) { render(); return; }
    const controller = new AbortController(), mine = generation;
    request = controller;
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const value = await api('/system/hermes-gateway', {signal: controller.signal});
      if (mine !== generation || !allowed()) return;
      mode = value.mode;
      state = Object.hasOwn(labels, value.state) ? value.state : 'unknown';
      received = Date.now();
    } catch (_) {
      if (mine === generation) state = 'unknown';
    } finally {
      clearTimeout(timer);
      if (request === controller) request = null;
      render();
    }
  }
  window.HermesGatewayStatus = {
    engineConnection(value) {
      engine = value;
      if (value !== 'connected') { generation += 1; request?.abort(); }
      render();
      if (value === 'connected') refresh();
    },
  };
  document.addEventListener('visibilitychange', refresh);
  document.addEventListener('ui-language-changed', render);
  setInterval(refresh, 3000);
  refresh();
})();
