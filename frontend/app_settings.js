/* Administrator settings, version checks and durable upgrade receipts. */
(() => {
  const t = window.t;
  const root = document.querySelector('#app-settings');
  const toggle = document.querySelector('#btn-settings');
  const menu = document.querySelector('#app-settings-menu');
  const about = document.querySelector('#btn-about');
  const notice = document.querySelector('#app-update-notice');
  const interval = 12 * 60 * 60 * 1000, retry = 5 * 60 * 1000;
  let nextCheck = 0, pending = null, epoch = 0, info = null, version = null;
  let submitting = false, monitoring = false;
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;
  const key = 'ge_app_upgrade';
  const save = value => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch {} };
  const read = () => { try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; } };
  const clear = () => { try { sessionStorage.removeItem(key); } catch {} };
  const dialog = document.createElement('dialog');
  dialog.id = 'app-about';
  dialog.setAttribute('aria-labelledby', 'app-about-title');
  dialog.innerHTML = `<h3 id="app-about-title" data-i18n="about_title">关于 BOT GROUP</h3>
    <p id="app-about-version"></p><p id="app-about-detail" role="status" aria-live="polite"></p>
    <p id="app-upgrade-progress" role="status" aria-live="polite"></p>
    <div class="dlg-btns"><button type="button" id="app-about-close" data-i18n="close">关闭</button>
    <button type="button" id="app-check-update" data-i18n="check_update">检查新版本</button>
    <button type="button" id="app-run-upgrade" class="primary" data-i18n="upgrade" hidden>升级</button>
    <button type="button" id="app-upgrade-refresh" class="primary" data-i18n="refresh_ui" hidden>刷新界面</button></div>`;
  document.body.append(dialog);
  const detail = dialog.querySelector('#app-about-detail');
  const progressText = dialog.querySelector('#app-upgrade-progress');
  const checkButton = dialog.querySelector('#app-check-update');
  const upgradeButton = dialog.querySelector('#app-run-upgrade');
  const refreshButton = dialog.querySelector('#app-upgrade-refresh');
  dialog.querySelector('#app-about-close').onclick = () => dialog.close();
  refreshButton.onclick = () => location.reload();
  const phases = {queued: 'phase_queued', downloading: 'phase_downloading', stopping: 'phase_stopping',
    backing_up: 'phase_backing_up', installing: 'phase_installing', dependencies: 'phase_dependencies',
    migrating: 'phase_migrating', checking: 'phase_checking', rolling_back: 'phase_rolling_back',
    rolled_back: 'phase_rolled_back', rollback_failed: 'rollback_failed', restarting: 'phase_restarting'};
  function showProgress(message, failed = false) {
    progressText.textContent = message;
    progressText.dataset.error = String(failed);
  }
  function requestError(error) {
    const message = String(error.message || '').trim();
    const reason = !message || /^(Internal Server Error|Service Unavailable|Bad Gateway|Gateway Timeout)$/i.test(message)
      ? t('server_error_retry') : message;
    return `${error.status ? `HTTP ${error.status}：` : ''}${reason}`;
  }
  function close(focus = false) {
    menu.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    if (focus) toggle.focus();
  }
  toggle.addEventListener('click', () => {
    menu.hidden = !menu.hidden;
    toggle.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', event => { if (!root.contains(event.target)) close(); });
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); }
  });
  root.addEventListener('focusout', () => setTimeout(() => {
    if (!root.contains(document.activeElement)) close();
  }, 0));
  menu.addEventListener('click', event => { if (event.target.closest('button')) close(); });
  function render() {
    dialog.querySelector('#app-about-version').textContent = `${t('current_version')}${version?.version_label || info?.version_label || t('reading_version')}`;
    detail.textContent = pending ? t('checking_update') : info?.status === 'ok'
      ? (info.update_available ? `${t('new_version')}${info.latest_version}` : t('up_to_date'))
      : info?.status === 'development' ? window.t('about_dev_version', [info.latest_version])
      : info?.status === 'unknown' ? `${t('formal_version')}${info.latest_version}${t('version_info_missing')}`
      : info?.status === 'unavailable' ? t('version_check_failed') : t('version_click_hint');
    if (info?.upgrade_blocked_reason) detail.textContent += `\n${info.upgrade_blocked_reason}`;
    checkButton.disabled = !!pending || submitting || monitoring;
    upgradeButton.hidden = !info?.update_available;
    upgradeButton.disabled = !!pending || submitting || monitoring || info?.can_upgrade === false || info?.status !== 'ok';
    upgradeButton.textContent = info?.update_available ? `${t('upgrade_to')} ${info.latest_version}` : t('upgrade');
  }
  async function showAbout() {
    if (!allowed()) return;
    if (!dialog.open) dialog.showModal();
    render();
    try {
      version = await api('/system/version', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (allowed()) render();
    } catch { detail.textContent = t('version_read_failed'); }
  }
  async function check(force = false) {
    if (!allowed() || pending || monitoring || (!force && Date.now() < nextCheck)) return;
    const generation = epoch;
    pending = api('/system/updates', {cache: 'no-store', signal: AbortSignal.timeout(35000)});
    render();
    try {
      const data = await pending;
      if (generation !== epoch || !allowed()) return;
      info = data;
      nextCheck = Date.now() + (data.status === 'unavailable' ? retry : interval);
      if (data.status !== 'unavailable') {
        notice.hidden = !data.update_available;
        notice.textContent = data.update_available ? `${t('new_version')}${data.latest_version}` : '';
      }
    } catch {
      if (generation === epoch) { nextCheck = Date.now() + retry; info = {status: 'unavailable'}; }
    } finally {
      pending = null; render();
      if (generation !== epoch) check();
    }
  }
  async function confirmUpgrade() {
    if (!allowed() || submitting || monitoring) return;
    if (!info?.update_available || info.status !== 'ok') { await showAbout(); await check(true); return; }
    if (info.can_upgrade === false) { await showAbout(); return; }
    submitting = true; render();
    const targetVersion = info.latest_version, generation = epoch;
    try {
      if (!await showConfirm(t('upgrade_confirm', [targetVersion]))) return;
      if (!allowed() || generation !== epoch) return;
      if (!dialog.open) dialog.showModal();
      refreshButton.hidden = true;
      showProgress(t('submitting_upgrade'));
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
      let operation = {id, target_version: targetVersion, created_at: Date.now() / 1000};
      let initialMessage = t('phase_queued');
      save(operation);
      try {
        operation = await api('/system/upgrade', {method: 'POST', body: JSON.stringify({request_id: id, version: targetVersion}), signal: AbortSignal.timeout(35000)});
      } catch (error) {
        if (error.status) { clear(); throw error; }
        // A lost response may already have launched the helper; reconcile by receipt ID.
        initialMessage = t('upgrade_response_lost');
      }
      await monitor(operation, initialMessage);
    } catch (error) {
      if (allowed() && generation === epoch) {
        const message = t('upgrade_submit_failed') + `\n${requestError(error)}`;
        showProgress(message, true);
        await showAlert(message);
      }
    }
    finally { submitting = false; render(); }
  }
  async function monitor(operation, initialMessage = t('phase_queued')) {
    if (monitoring || !allowed()) return;
    monitoring = true; save(operation);
    S.upgrading = true; S.restarting = true; S.es?.close();
    const restartButton = document.querySelector('#btn-restart');
    restartButton.disabled = true;
    if (!dialog.open) dialog.showModal();
    refreshButton.hidden = true; render();
    const generation = epoch, deadline = Date.now() + 30 * 60 * 1000;
    let reloading = false;
    const progress = (message, failed = false) => {
      showProgress(message, failed);
      S.restartMessage = message;
      setConn('connecting', message);
    };
    progress(initialMessage);
    try {
      while (allowed() && generation === epoch && Date.now() < deadline) {
        let requestPath = '/system/upgrade';
        try {
          const receipt = await api('/system/upgrade', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
          if (receipt.id === operation.id) {
            if (receipt.state === 'failed') { clear(); progress(receipt.error || t('upgrade_failed_logs'), true); return; }
            if (receipt.state === 'succeeded') {
              requestPath = '/health';
              const health = await api('/health', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
              if (health.status === 'ok' && health.instance_id === receipt.new_instance_id && health.program_root === receipt.program_root && health.db_path === receipt.db_path) {
                clear(); notice.hidden = true;
                progress(t('upgraded_refreshing', [receipt.target_version]));
                refreshButton.hidden = false;
                version = {version_label: receipt.target_version};
                info = {status: 'ok', update_available: false};
                reloading = true;
                location.reload();
                return;
              }
            } else progress(t(phases[receipt.phase]) || t('upgrading'));
          } else if (receipt.id) { clear(); progress(t('upgrade_request_changed'), true); return; }
        } catch (error) {
          if (error.status === 401 || error.status === 403) return;
          if (error.status) progress(t('upgrade_status_failed', [requestPath, requestError(error)]), true);
          else progress(t('upgrade_service_down'));
        }
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      if (generation === epoch && allowed()) progress(t('upgrade_timeout'), true);
    } finally {
      if (!reloading) {
        monitoring = false; S.upgrading = false; S.restarting = false; S.restartMessage = '';
        restartButton.disabled = false; render();
        if (allowed()) connectSSE();
      }
    }
  }
  async function restoreOperation() {
    if (!allowed() || monitoring || submitting) return;
    const saved = read();
    if (saved) { monitor(saved); return; }
    const generation = epoch;
    try {
      const receipt = await api('/system/upgrade', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (generation === epoch && allowed() && ['starting', 'running'].includes(receipt.state)) monitor(receipt);
    } catch {}
  }
  dialog.addEventListener('close', () => toggle.focus({preventScroll: true}));
  about.addEventListener('click', () => { showAbout(); restoreOperation(); });
  notice.addEventListener('click', () => { if (monitoring) showAbout(); else confirmUpgrade(); });
  checkButton.addEventListener('click', () => check(true));
  upgradeButton.addEventListener('click', confirmUpgrade);
  document.addEventListener('service-auth-changed', () => {
    epoch++; nextCheck = 0; info = null; notice.hidden = true;
    if (dialog.open) dialog.close();
    about.hidden = !allowed(); close(); check(); restoreOperation();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  window.addEventListener('focus', () => check());
  window.addEventListener('pageshow', () => check());
  setInterval(() => check(), 60000);
  document.addEventListener('ui-language-changed', render);
  about.hidden = !allowed(); check(); restoreOperation();
})();
