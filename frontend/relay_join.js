function relayDirectionChoices(prefix) {
  return `<div class="relay-direction-choices" id="${prefix}-choices" role="group" aria-label="连接方向" data-i18n-aria-label="relay_direction_group">
    <button type="button" id="${prefix}-dial"><span data-i18n="relay_direction_dial">本机连云端</span><span class="relay-direction-flag" data-flag="dial" hidden></span></button>
    <button type="button" id="${prefix}-listen"><span data-i18n="relay_direction_listen">云端连本机</span><span class="relay-direction-flag" data-flag="listen" hidden></span></button>
  </div>`;
}

function createRelayDirectionDialog(parent, prefix, {id = prefix, title, badge = '', content, actions}) {
  const dialog = document.createElement('dialog');
  dialog.id = id;
  dialog.setAttribute('aria-labelledby', prefix + '-title');
  dialog.innerHTML = `
    <div class="relay-direction-head"><h3 id="${prefix}-title" data-i18n="${title}">${window.t(title)}</h3>${badge}</div>
    ${relayDirectionChoices(prefix)}
    ${content}
    <div class="dlg-btns">${actions}</div>`;
  parent.append(dialog);
  return dialog;
}

function createRelayJoinDialog(parent, {allowed, requireListen, registered, copy}) {
  const dialog = createRelayDirectionDialog(parent, 'relay-join', {
    id: 'relay-join-dialog', title: 'relay_add',
    content: `<form id="relay-join-form" hidden>
      <label for="relay-join-url" data-i18n="relay_cloud_pairing_url">云端 Hermes 配对链接</label>
      <div class="relay-pairing-line">
        <input id="relay-join-url" type="password" autocomplete="off" required placeholder="粘贴配对链接" data-i18n-placeholder="relay_paste_pairing">
        <button class="primary" data-i18n="relay_verify_identity">核验身份</button>
      </div>
    </form>
    <div id="relay-join-invite" hidden>
      <label id="relay-join-guide" for="relay-join-command" data-i18n="relay_join_guide">复制命令，发给远程 Hermes 执行。</label>
      <div class="relay-copy-row">
        <input id="relay-join-command" readonly type="password" autocomplete="off">
        <button id="relay-join-copy" data-i18n="relay_copy_command">复制命令</button>
      </div>
    </div>
    <p id="relay-join-identity" class="relay-pairing-preview" hidden></p>
    <p id="relay-join-status" class="profile-help" role="status" aria-live="polite"></p>`,
    actions: `<button id="relay-join-cancel" data-i18n="cancel">取消</button>
      <button id="relay-join-confirm" class="primary" data-i18n="relay_confirm_add" hidden>确认添加</button>`,
  });
  const $ = id => dialog.querySelector('#relay-join-' + id);
  let attempt = null, timer = 0, version = 0, busy = false;
  let renewal = null;
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
    renewal = null;
    $('title').textContent = window.t('relay_add');
    $('guide').textContent = window.t('relay_join_guide');
    $('cancel').textContent = window.t('cancel');
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
  function endRenewal(message) {
    attempt = null;
    $('invite').hidden = true;
    $('command').value = '';
    $('cancel').textContent = window.t('close');
    status(message, true);
  }
  function failure(view) {
    const device = view.host || view.member || window.t('relay_cloud_hermes');
    if (view.state === 'expired') return window.t('relay_join_expired');
    if (view.reason === 'name_conflict') return window.t('relay_join_name_conflict', [device]);
    const guidance = {dns: window.t('relay_join_dns'), tcp: window.t('relay_join_tcp'),
      tls: window.t('relay_join_tls'), identity: window.t('relay_join_identity'),
      auth: window.t('relay_join_auth'), confirmation: window.t('relay_join_confirmation')};
    return `${device} ${guidance[view.stage] || window.t('relay_join_generic_failed')}`;
  }
  async function display(view, token) {
    if (!current(token)) return;
    if (view.state === 'confirmed') {
      const member = view.node_member || view.member;
      const renewed = Boolean(renewal);
      dismiss();
      await registered(member, renewed);
      return;
    }
    if (!['waiting', 'pending_confirmation'].includes(view.state)) {
      if (renewal) {
        endRenewal(view.state === 'expired' ? window.t('relay_renew_expired')
          : view.state === 'cancelled' ? window.t('relay_renew_cancelled') : window.t('relay_renew_failed'));
        return;
      }
      reset();
      status(view.state === 'cancelled' ? window.t('relay_join_cancelled') : failure(view), true);
      return;
    }
    const pending = view.state === 'pending_confirmation';
    $('identity').hidden = !pending;
    $('confirm').hidden = !pending;
    if (pending) {
      $('invite').hidden = true;
      $('command').value = '';
      const endpoint = view.peer_ip || `${view.host}:${view.port} · ${window.t('relay_short_fingerprint')} ${view.fingerprint_short}`;
      $('identity').textContent = window.t('relay_verified_member', [view.member, endpoint]);
      status(window.t('relay_verify_before_confirm'));
    } else {
      status(renewal ? window.t('relay_renew_waiting') : window.t('relay_waiting_connect'));
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
          if (renewal) {
            endRenewal(window.t('relay_renew_result_unreadable'));
            return;
          }
          reset();
          status(window.t('relay_join_read_failed'), true);
        } else {
          status(window.t('relay_join_status_retry'), true);
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
    status(renewal ? window.t('relay_renew_generating') : inbound ? window.t('relay_invite_generating') : window.t('relay_verifying_cloud'));
    try {
      const path = renewal ? '/relay/nodes/' + encodeURIComponent(renewal.member) + '/renewal'
        : inbound ? base : base + '/preview';
      const view = await request(path, 'POST', inbound ? null : {pairing_url: link});
      if (!current(token)) return;
      if (!view.pairing_id) throw new Error();
      attempt = {path: base + '/' + encodeURIComponent(view.pairing_id), link: inbound ? '' : link};
      $('url').value = '';
      $('form').hidden = true;
      $('choices').hidden = true;
      if (inbound) {
        const profile = renewal && (renewal.host_profile || renewal.member);
        if (renewal && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(profile)) throw new Error('invalid profile');
        const hermes = renewal ? `hermes -p "${profile}"` : 'hermes';
        $('command').value = `${hermes} bot-group-relay register --engine-url "${view.engine_url}" --invite "${view.invite_token}"`;
        $('invite').hidden = false;
      }
      await display(inbound ? {state: 'waiting'} : view, token);
    } catch (error) {
      if (current(token)) status(renewal ? window.t('relay_renew_generate_failed')
        : error.status === 400 ? window.t('relay_join_bad_link')
        : error.pairing?.state ? failure(error.pairing)
        : inbound ? window.t('relay_invite_generate_failed') : window.t('relay_join_verify_failed'), true);
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
        status(window.t('relay_join_pending_verify'), true);
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
  $('copy').onclick = () => copy($('command').value, window.t('relay_connect_command'), $('status'));
  $('confirm').onclick = () => decide(true);
  $('cancel').onclick = () => decide(false);
  dialog.addEventListener('cancel', event => { event.preventDefault(); decide(false); });
  return {dismiss, open() { if (allowed()) { reset(); dialog.showModal(); } },
    renew(node, label) {
      requireListen(() => {
        if (!allowed() || !parent.open) return;
        reset();
        renewal = node;
        $('title').textContent = window.t('relay_renew_credential') + ' · ' + label;
        $('choices').hidden = true;
        $('guide').textContent = window.t('relay_renew_guide', [node.host_profile || node.member]);
        dialog.showModal();
        start(true);
      });
    }};
}
