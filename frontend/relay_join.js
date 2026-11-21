function createRelayJoinDialog(parent, {allowed, requireListen, registered, copy}) {
  const dialog = document.createElement('dialog');
  dialog.id = 'relay-join-dialog';
  dialog.setAttribute('aria-labelledby', 'relay-join-title');
  dialog.innerHTML = `
    <h3 id="relay-join-title">添加远程 Hermes</h3>
    <div class="relay-direction-choices" id="relay-join-choices">
      <button id="relay-join-dial">本机连云端</button>
      <button id="relay-join-listen">云端连本机</button>
    </div>
    <form id="relay-join-form" hidden>
      <label for="relay-join-url">云端 Hermes 配对链接</label>
      <div class="relay-pairing-line">
        <input id="relay-join-url" type="password" autocomplete="off" required placeholder="粘贴配对链接">
        <button class="primary">核验身份</button>
      </div>
    </form>
    <div id="relay-join-invite" hidden>
      <label for="relay-join-command">在云端 Hermes 运行</label>
      <div class="relay-copy-row">
        <input id="relay-join-command" readonly type="password" autocomplete="off">
        <button id="relay-join-copy">复制命令</button>
      </div>
    </div>
    <p id="relay-join-identity" class="relay-pairing-preview" hidden></p>
    <p id="relay-join-status" class="profile-help" role="status" aria-live="polite"></p>
    <div class="dlg-btns">
      <button id="relay-join-cancel">取消</button>
      <button id="relay-join-confirm" class="primary" hidden>确认添加</button>
    </div>`;
  parent.append(dialog);
  const $ = id => dialog.querySelector('#relay-join-' + id);
  let attempt = null, timer = 0, version = 0, busy = false;
  const current = token => dialog.open && parent.open && allowed() && version === token;
  const request = (path, method = 'GET', data) => api(path, {
    method, cache: 'no-store', signal: AbortSignal.timeout(20000),
    body: data ? JSON.stringify(data) : undefined,
  });
  function status(text, error = false) {
    $('status').textContent = text;
    $('status').dataset.error = String(error);
  }
  function lock(value) {
    busy = value;
    for (const control of dialog.querySelectorAll('button, input:not([readonly])')) control.disabled = value;
  }
  function reset() {
    ++version;
    clearTimeout(timer);
    attempt = null;
    lock(false);
    $('choices').hidden = false;
    for (const id of ['form', 'invite', 'identity', 'confirm']) $(id).hidden = true;
    $('url').value = '';
    $('command').value = '';
    $('identity').textContent = '';
    status('');
  }
  function dismiss() {
    reset();
    if (dialog.open) dialog.close();
  }
  function failure(view) {
    const device = view.host || view.member || '云端 Hermes';
    if (view.state === 'expired') return '本次配对已过期。请重新核验有效链接，或重新生成邀请。';
    if (view.reason === 'name_conflict') return `${device} 的成员名已被使用。请核对已有节点，或更改云端成员名后重试。`;
    const guidance = {dns: '地址解析失败，请检查云端域名。', tcp: '连接失败，请检查云端监听地址和端口。',
      tls: '证书核验失败，请从云端 Hermes 重新复制配对链接。', identity: '成员身份不匹配，请核对云端 Hermes。',
      auth: '配对认证失败，请核对插件版本并重新获取链接。', confirmation: '确认失败，请重新核验配对链接。'};
    return `${device} ${guidance[view.stage] || '配对失败，请重新获取链接或邀请。'}`;
  }
  async function display(view, token) {
    if (!current(token)) return;
    if (view.state === 'confirmed') {
      const member = view.member;
      dismiss();
      await registered(member);
      return;
    }
    if (!['waiting', 'pending_confirmation'].includes(view.state)) {
      reset();
      status(view.state === 'cancelled' ? '本次配对已取消。' : failure(view), true);
      return;
    }
    const pending = view.state === 'pending_confirmation';
    $('identity').hidden = !pending;
    $('confirm').hidden = !pending;
    if (pending) {
      $('invite').hidden = true;
      $('command').value = '';
      const endpoint = view.peer_ip || `${view.host}:${view.port} · 指纹 ${view.fingerprint_short}`;
      $('identity').textContent = `已核验成员　${view.member}　·　${endpoint}`;
      status('请核对远端身份后确认添加。');
    } else {
      status('等待云端 Hermes 连接。');
    }
    poll(token);
  }
  function poll(token) {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!current(token) || !attempt || busy) return;
      const path = attempt.path;
      try {
        const view = await request(path);
        if (current(token) && !busy && attempt?.path === path) await display(view, token);
      } catch (error) {
        if (!current(token) || busy) return;
        if (error.status === 404 || error.status === 403) {
          reset();
          status('本机无法读取配对记录，请核对管理员身份并重新配对。', true);
        } else {
          status('本机读取配对状态失败，正在重试。', true);
          poll(token);
        }
      }
    }, 1000);
  }
  async function start(inbound) {
    if (busy || attempt || !allowed() || !dialog.open) return;
    const token = ++version;
    const base = inbound ? '/relay/invitations' : '/relay/pairings';
    const link = $('url').value.trim();
    lock(true);
    status(inbound ? '正在生成邀请…' : '正在核验云端 Hermes…');
    try {
      const view = await request(inbound ? base : base + '/preview', 'POST', inbound ? null : {pairing_url: link});
      if (!current(token)) return;
      if (!view.pairing_id) throw new Error();
      attempt = {path: base + '/' + encodeURIComponent(view.pairing_id), link: inbound ? '' : link};
      $('url').value = '';
      $('form').hidden = true;
      $('choices').hidden = true;
      if (inbound) {
        $('command').value = `hermes bot-group-relay register --engine-url "${view.engine_url}" --invite "${view.invite_token}"`;
        $('invite').hidden = false;
      }
      await display(inbound ? {state: 'waiting'} : view, token);
    } catch (error) {
      if (current(token)) status(error.status === 400 ? '本机无法解析配对链接，请从云端 Hermes 重新复制。'
        : error.pairing?.state ? failure(error.pairing)
        : inbound ? '本机生成邀请失败，请重新选择云端连本机。' : '本机核验失败，请检查云端配对链接并重试。', true);
    } finally {
      if (current(token)) lock(false);
    }
  }
  async function decide(confirm) {
    if (busy || !allowed()) return;
    if (!attempt) { dismiss(); return; }
    const token = ++version;
    clearTimeout(timer);
    lock(true);
    try {
      const body = confirm && attempt.link ? {pairing_url: attempt.link} : undefined;
      const view = await request(attempt.path + (confirm ? '/confirm' : '/cancel'), 'POST', body);
      if (!current(token)) return;
      if (!confirm && view.state === 'cancelled') dismiss();
      else await display(view, token);
    } catch (error) {
      if (!current(token)) return;
      if (error.pairing?.state) await display(error.pairing, token);
      else {
        status('本机尚未取得操作结果，正在核对配对状态。', true);
        poll(token);
      }
    } finally {
      if (current(token)) lock(false);
    }
  }
  $('dial').onclick = () => {
    $('choices').hidden = true;
    $('form').hidden = false;
    status('');
    $('url').focus();
  };
  $('listen').onclick = () => requireListen(() => start(true));
  $('form').onsubmit = event => { event.preventDefault(); start(false); };
  $('copy').onclick = () => copy($('command').value, '连接命令', $('status'));
  $('confirm').onclick = () => decide(true);
  $('cancel').onclick = () => decide(false);
  dialog.addEventListener('cancel', event => { event.preventDefault(); decide(false); });
  return {dismiss, open() { if (allowed()) { reset(); dialog.showModal(); } }};
}
