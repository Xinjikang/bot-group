/* Settings actions retain their existing handlers and administrator boundaries. */
(() => {
  const root = document.querySelector('#app-settings');
  const toggle = document.querySelector('#btn-settings');
  const menu = document.querySelector('#app-settings-menu');
  const about = document.querySelector('#btn-about');
  const notice = document.querySelector('#app-update-notice');
  const interval = 12 * 60 * 60 * 1000, retry = 5 * 60 * 1000;
  let nextCheck = 0, pending = null, epoch = 0, info = null;
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;
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
  menu.addEventListener('click', event => {
    if (event.target.closest('button')) close();
  });
  async function showAbout() {
    try {
      const version = await api('/system/version', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!allowed()) return;
      const detail = info?.status === 'ok' ? (info.update_available
        ? `可用正式版本：${info.latest_version}。请由管理员按发布指南手动升级。` : '暂无更高正式版本。')
        : info?.status === 'development' ? `可用正式版本：${info.latest_version}；当前为 Git 开发版，请按提交记录核对版本差异。`
        : info?.status === 'unknown' ? `可用正式版本：${info.latest_version}；当前源码缺少版本信息，暂时无法比较。`
        : '更新检查暂不可用，将自动重试。';
      showAlert(`BOT GROUP\n当前版本：${version.version_label}\n${detail}`);
    } catch { showAlert('版本信息读取失败，请稍后重试。'); }
  }
  about.addEventListener('click', showAbout);
  notice.addEventListener('click', showAbout);
  async function check() {
    if (!allowed() || pending || Date.now() < nextCheck) return;
    const generation = epoch;
    pending = api('/system/updates', {cache: 'no-store', signal: AbortSignal.timeout(35000)});
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
      if (generation === epoch) nextCheck = Date.now() + retry;
    } finally {
      pending = null;
      if (generation !== epoch) check();
    }
  }
  document.addEventListener('service-auth-changed', () => {
    epoch++; nextCheck = 0; info = null; notice.hidden = true;
    about.hidden = !allowed(); close(); check();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  window.addEventListener('focus', check);
  window.addEventListener('pageshow', check);
  setInterval(check, 60000);
  about.hidden = !allowed();
  check();
})();
