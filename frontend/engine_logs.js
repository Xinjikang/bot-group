/* Bounded engine log snapshots with dialog-scoped polling. */
(() => {
  const entry = document.querySelector('#btn-engine-logs');
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;
  const dialog = document.createElement('dialog');
  dialog.id = 'engine-logs';
  dialog.setAttribute('aria-labelledby', 'engine-logs-title');
  dialog.innerHTML = `<h3 id="engine-logs-title">引擎日志</h3>
    <div class="engine-logs-tools"><label><input id="engine-logs-auto" type="checkbox" checked>自动刷新（每 2 秒）</label>
    <button id="engine-logs-refresh" type="button">刷新</button></div>
    <p id="engine-logs-status" role="status" aria-live="polite"></p>
    <pre id="engine-logs-content" tabindex="0" aria-label="引擎日志内容"></pre>
    <div class="dlg-btns"><button id="engine-logs-close" type="button">关闭</button></div>`;
  document.body.append(dialog);
  const content = dialog.querySelector('#engine-logs-content');
  const status = dialog.querySelector('#engine-logs-status');
  const auto = dialog.querySelector('#engine-logs-auto');
  const refresh = dialog.querySelector('#engine-logs-refresh');
  let timer, request, epoch = 0, first = true;
  function stop() {
    epoch++;
    clearTimeout(timer);
    request?.abort(); request = null;
    refresh.disabled = false;
  }
  function schedule() {
    clearTimeout(timer);
    if (dialog.open && allowed() && auto.checked && !document.hidden)
      timer = setTimeout(load, 2000);
  }
  async function load() {
    if (!dialog.open || !allowed() || request) return;
    clearTimeout(timer);
    const generation = epoch;
    const controller = new AbortController();
    request = controller; refresh.disabled = true;
    const timeout = setTimeout(() => controller.abort(), 10000);
    status.textContent = '正在读取引擎日志…'; status.dataset.error = 'false';
    try {
      const data = await api('/system/logs', {cache: 'no-store', signal: controller.signal});
      if (generation !== epoch || !dialog.open || !allowed()) return;
      const follow = first || content.scrollHeight - content.scrollTop - content.clientHeight < 32;
      const oldTop = content.scrollTop;
      content.textContent = data.text || (data.available ? '暂无引擎日志。' : '引擎日志尚未生成。');
      content.scrollTop = follow ? content.scrollHeight : oldTop;
      first = false;
      status.textContent = `最近 ${data.lines} 行${data.truncated ? ' · 已截取日志末尾' : ''} · 更新于 ${new Date().toLocaleTimeString()}`;
    } catch (error) {
      if (generation !== epoch || !dialog.open) return;
      status.textContent = `读取失败：${error.name === 'AbortError' ? '请求超时' : error.message}。可点击刷新重试。`;
      status.dataset.error = 'true';
    } finally {
      clearTimeout(timeout);
      if (generation === epoch) { request = null; refresh.disabled = false; schedule(); }
    }
  }
  entry.addEventListener('click', () => {
    if (!allowed()) return;
    stop(); first = true; auto.checked = true; content.textContent = '';
    dialog.showModal(); load();
  });
  refresh.addEventListener('click', load);
  auto.addEventListener('change', schedule);
  dialog.querySelector('#engine-logs-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    stop(); content.textContent = ''; status.textContent = '';
    document.querySelector('#btn-settings').focus({preventScroll: true});
  });
  document.addEventListener('service-auth-changed', () => {
    stop(); content.textContent = ''; status.textContent = '';
    entry.hidden = !allowed();
    if (dialog.open) dialog.close();
  });
  document.addEventListener('visibilitychange', schedule);
  entry.hidden = !allowed();
})();
