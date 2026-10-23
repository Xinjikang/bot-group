/* Administrator settings, version checks and durable upgrade receipts. */
(() => {
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
  dialog.innerHTML = `<h3 id="app-about-title">关于 BOT GROUP</h3>
    <p id="app-about-version"></p><p id="app-about-detail" role="status" aria-live="polite"></p>
    <p id="app-upgrade-progress" role="status" aria-live="polite"></p>
    <div class="dlg-btns"><button type="button" id="app-about-close">关闭</button>
    <button type="button" id="app-check-update">检查新版本</button>
    <button type="button" id="app-run-upgrade" class="primary" hidden>升级</button>
    <button type="button" id="app-upgrade-refresh" class="primary" hidden>刷新界面</button></div>`;
  document.body.append(dialog);
  const detail = dialog.querySelector('#app-about-detail');
  const progressText = dialog.querySelector('#app-upgrade-progress');
  const checkButton = dialog.querySelector('#app-check-update');
  const upgradeButton = dialog.querySelector('#app-run-upgrade');
  const refreshButton = dialog.querySelector('#app-upgrade-refresh');
  dialog.querySelector('#app-about-close').onclick = () => dialog.close();
  refreshButton.onclick = () => location.reload();
  const phases = {queued: '准备升级…', downloading: '下载并校验新版本…', stopping: '等待 Bot 收尾并停止服务…',
    backing_up: '备份程序与数据…', installing: '更新程序文件…', dependencies: '安装依赖…',
    migrating: '验证数据库迁移…', checking: '检查运行环境…', rolling_back: '恢复原版本…',
    rolled_back: '原版本已恢复…', rollback_failed: '恢复失败，请查看日志', restarting: '启动服务并验证…'};
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
    dialog.querySelector('#app-about-version').textContent = `当前版本：${version?.version_label || info?.version_label || '读取中…'}`;
    detail.textContent = pending ? '正在检查新版本…' : info?.status === 'ok'
      ? (info.update_available ? `发现新版本 ${info.latest_version}` : '当前已是最新或更高版本。')
      : info?.status === 'development' ? `可用正式版本：${info.latest_version}；当前为 Git 开发版，请按提交记录核对版本差异。`
      : info?.status === 'unknown' ? `可用正式版本：${info.latest_version}；当前源码缺少版本信息，暂时无法比较。`
      : info?.status === 'unavailable' ? '检查新版本失败，请重试。' : '点击“检查新版本”获取最新版本。';
    if (info?.upgrade_blocked_reason) detail.textContent += `\n${info.upgrade_blocked_reason}`;
    checkButton.disabled = !!pending || submitting || monitoring;
    upgradeButton.hidden = !info?.update_available;
    upgradeButton.disabled = !!pending || submitting || monitoring || info?.can_upgrade === false || info?.status !== 'ok';
    upgradeButton.textContent = info?.update_available ? `升级到 ${info.latest_version}` : '升级';
  }
  async function showAbout() {
    if (!allowed()) return;
    if (!dialog.open) dialog.showModal();
    render();
    try {
      version = await api('/system/version', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (allowed()) render();
    } catch { detail.textContent = '版本信息读取失败，请重试。'; }
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
        notice.textContent = data.update_available ? `新版本 ${data.latest_version}` : '';
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
      if (!await showConfirm(`升级到 ${targetVersion}？\n系统将校验下载内容、暂停服务并备份程序与数据，然后更新并自动启动服务。Bot 收尾最多等待 120 秒；升级失败时自动恢复原版本。`)) return;
      if (!allowed() || generation !== epoch) return;
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
      let operation = {id, target_version: targetVersion, created_at: Date.now() / 1000};
      save(operation);
      try {
        operation = await api('/system/upgrade', {method: 'POST', body: JSON.stringify({request_id: id, version: targetVersion}), signal: AbortSignal.timeout(35000)});
      } catch (error) {
        if (error.status) { clear(); throw error; }
        // A lost response may already have launched the helper; reconcile by receipt ID.
      }
      await monitor(operation);
    } catch (error) { await showAlert(error.message); }
    finally { submitting = false; render(); }
  }
  async function monitor(operation) {
    if (monitoring || !allowed()) return;
    monitoring = true; save(operation);
    S.upgrading = true; S.restarting = true; S.es?.close();
    const restartButton = document.querySelector('#btn-restart');
    restartButton.disabled = true;
    if (!dialog.open) dialog.showModal();
    refreshButton.hidden = true; render();
    const generation = epoch, deadline = Date.now() + 30 * 60 * 1000;
    const progress = (message, failed = false) => {
      progressText.textContent = message; progressText.dataset.error = String(failed);
      S.restartMessage = message;
      setConn('connecting', message);
    };
    progress('准备升级…');
    try {
      while (allowed() && generation === epoch && Date.now() < deadline) {
        try {
          const receipt = await api('/system/upgrade', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
          if (receipt.id === operation.id) {
            if (receipt.state === 'failed') { clear(); progress(receipt.error || '升级失败，请查看 logs/app-upgrade.log。', true); return; }
            if (receipt.state === 'succeeded') {
              const health = await api('/health', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
              if (health.status === 'ok' && health.instance_id === receipt.new_instance_id && health.program_root === receipt.program_root && health.db_path === receipt.db_path) {
                clear(); notice.hidden = true;
                progress(`已升级到 ${receipt.target_version}，服务已恢复。请刷新界面加载新版本。`);
                refreshButton.hidden = false;
                version = {version_label: receipt.target_version};
                info = {status: 'ok', update_available: false};
                return;
              }
            } else progress(phases[receipt.phase] || '正在升级…');
          } else if (receipt.id) { clear(); progress('升级请求已变化，请重新打开“关于”查看状态。', true); return; }
        } catch (error) {
          if (error.status === 401 || error.status === 403) return;
          progress('升级服务暂时断开，正在等待恢复…');
        }
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      if (generation === epoch && allowed()) progress('等待升级结果超时；重新打开“关于”可继续查看。服务未恢复时请检查 logs/app-upgrade.log。', true);
    } finally {
      monitoring = false; S.upgrading = false; S.restarting = false; S.restartMessage = '';
      restartButton.disabled = false; render();
      if (allowed()) connectSSE();
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
  about.hidden = !allowed(); check(); restoreOperation();
})();
