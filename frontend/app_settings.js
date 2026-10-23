/* Page-local checks run on entry, every twelve hours, and after an overdue resume. */
(() => {
  const interval = 12 * 60 * 60 * 1000;
  const menu = document.querySelector('#settings-menu');
  const update = document.querySelector('#btn-update');
  const name = document.querySelector('#topbar-user-name');
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-about'; dialog.setAttribute('aria-labelledby', 'about-title');
  dialog.innerHTML = `<h3 id="about-title">关于 bot-group</h3><p id="about-version">正在读取版本…</p>
    <p id="about-update" role="status"></p><p>使用安装目录中的 upgrade.bat（Windows）或 ./upgrade.sh（Linux/macOS）升级。</p>
    <div class="dlg-btns"><button id="about-check" type="button">检查更新</button><button id="about-close" type="button">关闭</button></div>`;
  document.body.appendChild(dialog);
  let result = null, lastCheck = 0, pending = false, timer = null;
  const active = () => S.isUserIdentity && document.querySelector('#login').hidden;
  function render() {
    name.hidden = !active(); name.textContent = S.userDisplayName || '用户';
    update.hidden = !active() || !result?.update_available;
    update.textContent = result?.update_available ? `新版本 ${result.latest_version}` : '';
    document.querySelector('#about-version').textContent = `当前版本：${result?.current_version || '开发版（未提供 VERSION）'}`;
    document.querySelector('#about-update').textContent = pending ? '正在检查更新…'
      : result?.status === 'unavailable' ? '暂时无法检查更新，可点击重试。'
      : result?.status === 'unknown_version' ? `最新正式版：${result.latest_version}。当前版本号未知，暂无法比较。`
      : result?.update_available ? `可更新至 ${result.latest_version}`
      : result?.status === 'ok' ? `已是最新或更高版本（最新正式版 ${result.latest_version}）` : '';
    document.querySelector('#about-check').disabled = pending || !active();
  }
  async function check(force = false) {
    if (!active() || pending || (!force && lastCheck && Date.now() - lastCheck < interval)) return;
    const token = S.token;
    pending = true; lastCheck = Date.now(); render();
    try {
      const data = await api('/system/update-check', {cache: 'no-store', signal: AbortSignal.timeout(35000)});
      if (active() && token === S.token) result = data;
    } catch {
      if (active() && token === S.token) result = {...result, status: 'unavailable'};
    } finally {
      pending = false; render(); clearTimeout(timer);
      if (active()) timer = setTimeout(() => check(), interval);
    }
  }
  async function about() {
    render(); dialog.showModal();
    if (!result?.current_version) {
      try { result = {...result, ...await api('/system/version', {cache: 'no-store'})}; render(); }
      catch { document.querySelector('#about-version').textContent = '当前版本读取失败'; }
    }
  }
  document.querySelector('#btn-about').addEventListener('click', about);
  update.addEventListener('click', about);
  document.querySelector('#about-close').onclick = () => dialog.close();
  document.querySelector('#about-check').onclick = () => check(true);
  menu.addEventListener('click', event => { if (event.target.closest('button')) queueMicrotask(() => { menu.open = false; }); });
  document.addEventListener('click', event => { if (!menu.contains(event.target)) menu.open = false; });
  menu.addEventListener('keydown', event => { if (event.key === 'Escape') { menu.open = false; menu.querySelector('summary').focus(); } });
  document.addEventListener('service-auth-changed', () => {
    if (!active()) { clearTimeout(timer); lastCheck = 0; result = null; if (dialog.open) dialog.close(); }
    render(); check();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  window.addEventListener('focus', () => check());
  document.addEventListener('user-display-name-loaded', render);
  render(); check();
})();
