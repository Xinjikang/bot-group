/* Remote Hermes relay node administration. */
(() => {
  const entry = document.querySelector('#btn-relay-nodes');
  const anchor = document.querySelector('#btn-settings');
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;

  const dialog = document.createElement('dialog');
  dialog.id = 'relay-nodes-dialog';
  dialog.setAttribute('aria-labelledby', 'relay-nodes-title');
  dialog.innerHTML = `
    <h3 id="relay-nodes-title">远程节点管理</h3>
    <p class="profile-help">管理 Hermes 远程节点的连接凭据；凭据只在签发成功后显示一次，关闭或刷新后不再显示。</p>
    <p id="relay-readonly" class="profile-help" role="note" hidden>当前为 Bot 身份，仅可查看说明；远程节点管理需要管理员登录。</p>
    <div id="relay-admin-body">
      <div class="relay-toolbar">
        <button type="button" id="relay-refresh">刷新</button>
        <span id="relay-nodes-status" class="profile-help" role="status" aria-live="polite"></span>
      </div>
      <div id="relay-node-list" aria-live="polite"></div>
      <section id="relay-register" class="relay-register">
        <h4>登记新节点</h4>
        <label for="relay-member">Hermes 成员</label>
        <select id="relay-member"></select>
        <p class="profile-help">凭据由引擎生成；登记成功后一次性展示成员标识和凭据。已登记成员请使用节点卡片中的“重新签发”。</p>
        <div class="relay-register-actions">
          <button type="button" id="relay-register-submit" class="primary" disabled>登记</button>
          <span id="relay-register-status" class="profile-help" role="status" aria-live="polite"></span>
        </div>
      </section>
      <section id="relay-credential" class="relay-credential" hidden aria-labelledby="relay-credential-title">
        <h4 id="relay-credential-title">请立即保存凭据</h4>
        <p class="profile-help" role="alert">凭据只显示这一次；关闭窗口、刷新页面或重新打开后无法再次查看。</p>
        <label for="relay-issued-member">成员标识</label>
        <div class="relay-copy-row">
          <input id="relay-issued-member" readonly autocomplete="off" spellcheck="false" aria-label="成员标识">
          <button type="button" id="relay-copy-member">复制</button>
        </div>
        <label for="relay-issued-credential">连接凭据</label>
        <div class="relay-copy-row">
          <input id="relay-issued-credential" readonly autocomplete="off" spellcheck="false" aria-label="连接凭据">
          <button type="button" id="relay-copy-credential">复制</button>
        </div>
      </section>
    </div>
    <div class="dlg-btns"><button type="button" id="relay-close">关闭</button></div>`;
  document.body.append(dialog);

  const $ = selector => dialog.querySelector(selector);
  const body = $('#relay-admin-body');
  const readonly = $('#relay-readonly');
  const refresh = $('#relay-refresh');
  const nodesStatus = $('#relay-nodes-status');
  const list = $('#relay-node-list');
  const memberSelect = $('#relay-member');
  const register = $('#relay-register-submit');
  const registerStatus = $('#relay-register-status');
  const credentialPanel = $('#relay-credential');
  const issuedMember = $('#relay-issued-member');
  const issuedCredential = $('#relay-issued-credential');
  const close = $('#relay-close');

  let epoch = 0, busy = false, nodes = [], members = [], issued = null;

  function report(target, message, error = false) {
    target.textContent = message;
    target.dataset.error = String(error);
    target.setAttribute('role', error ? 'alert' : 'status');
  }
  function message(error) {
    const text = String(error?.message || '').trim();
    return text && !/^(Internal Server Error|Service Unavailable|Bad Gateway|Gateway Timeout)$/i.test(text)
      ? text : '服务端处理请求失败，请稍后重试。';
  }
  function fmtHeartbeat(value) {
    const ts = Number(value);
    if (!Number.isFinite(ts) || ts <= 0) return '—';
    return new Date(ts * 1000).toLocaleString();
  }
  function selectedMember() {
    const profile = memberSelect.value;
    return members.find(member => member.profile === profile) || null;
  }
  function candidateMembers() {
    const registered = new Set(nodes.map(node => node.member));
    return members.filter(member => !registered.has(member.profile));
  }
  function refreshControls() {
    refresh.disabled = busy;
    memberSelect.disabled = busy;
    close.disabled = busy;
    register.disabled = busy || !selectedMember();
    for (const button of list.querySelectorAll('button')) button.disabled = busy;
  }
  function clearIssued() {
    issued = null;
    credentialPanel.hidden = true;
    issuedMember.value = '';
    issuedCredential.value = '';
  }
  function showIssued(member, credential) {
    issued = {member, credential};
    issuedMember.value = member;
    issuedCredential.value = credential;
    credentialPanel.hidden = false;
    credentialPanel.scrollIntoView({block: 'nearest'});
  }
  function renderMembers() {
    const available = candidateMembers();
    memberSelect.replaceChildren();
    if (!available.length) {
      memberSelect.append(new Option(nodes.length ? '全部 Hermes 成员已登记' : '暂无可登记的 Hermes 成员', ''));
    } else {
      memberSelect.append(new Option('选择 Hermes 成员', ''));
      for (const member of available) {
        const label = `${member.display_name || member.profile}（${member.profile}）${member.active === 0 ? ' · 已停用' : ''}`;
        memberSelect.append(new Option(label, member.profile));
      }
    }
    refreshControls();
  }
  function renderNodes() {
    list.replaceChildren();
    if (!nodes.length) {
      const empty = document.createElement('p');
      empty.className = 'profile-help relay-empty';
      empty.textContent = '暂无已登记节点。选择 Hermes 成员完成首次登记。';
      list.append(empty);
      return;
    }
    for (const node of nodes) {
      const card = document.createElement('article');
      card.className = 'relay-node';
      card.dataset.relayMember = node.member;

      const head = document.createElement('div');
      head.className = 'relay-node-head';
      const name = document.createElement('strong');
      name.className = 'relay-node-name';
      name.textContent = node.display_name || node.member;
      const badge = document.createElement('span');
      const revoked = !!node.credential_revoked;
      const online = node.state === 'online' && !revoked;
      badge.className = `relay-badge ${revoked ? 'revoked' : online ? 'online' : 'offline'}`;
      badge.textContent = revoked ? '凭据已吊销' : online ? '在线' : '离线';
      head.append(name, badge);

      const meta = document.createElement('div');
      meta.className = 'relay-node-meta';
      const fields = [
        ['成员', node.member],
        ['代次', node.generation ?? '—'],
        ['最近心跳', fmtHeartbeat(node.last_heartbeat)],
        ['占用', node.capacity_used ?? 0],
        ['MCP 指纹', node.mcp_fingerprint || '未上报'],
      ];
      if (node.code_version) fields.push(['版本', node.code_version]);
      for (const [key, value] of fields) {
        const item = document.createElement('span');
        const label = document.createElement('span');
        label.className = 'relay-key';
        label.textContent = key;
        const text = document.createElement('span');
        text.textContent = String(value);
        item.append(label, text);
        meta.append(item);
      }

      const actions = document.createElement('div');
      actions.className = 'relay-node-actions';
      if (!revoked) {
        const revoke = document.createElement('button');
        revoke.type = 'button';
        revoke.className = 'relay-danger';
        revoke.dataset.action = 'revoke';
        revoke.textContent = '吊销凭据';
        actions.append(revoke);
      }
      const reissue = document.createElement('button');
      reissue.type = 'button';
      reissue.dataset.action = 'reissue';
      reissue.textContent = '重新签发';
      actions.append(reissue);
      card.append(head, meta, actions);
      list.append(card);
    }
    refreshControls();
  }
  function renderReadonly() {
    epoch++;
    clearIssued();
    nodes = [];
    members = [];
    list.replaceChildren();
    report(nodesStatus, '');
    report(registerStatus, '');
    readonly.hidden = false;
    body.hidden = true;
  }
  function renderAdmin() {
    readonly.hidden = true;
    body.hidden = false;
    renderNodes();
    renderMembers();
  }
  async function loadNodes(generation, quiet = false) {
    refresh.disabled = true;
    if (!quiet) report(nodesStatus, '正在读取远程节点…');
    try {
      const data = await api('/relay/nodes', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (generation !== epoch || !dialog.open) return;
      if (!Array.isArray(data)) throw new Error('远程节点响应格式异常');
      nodes = data;
      renderNodes();
      if (!quiet) report(nodesStatus, nodes.length
        ? `已加载 ${nodes.length} 个节点 · 更新于 ${new Date().toLocaleTimeString()}`
        : '暂无已登记节点。');
    } catch (error) {
      if (generation !== epoch || !dialog.open) return;
      report(nodesStatus, `读取远程节点失败：${message(error)}。点击刷新重试。`, true);
    } finally {
      if (generation === epoch) refreshControls();
    }
  }
  async function loadMembers(generation) {
    try {
      const data = await api('/members', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (generation !== epoch || !dialog.open) return;
      members = (data.members || []).filter(member =>
        member && member.profile && member.profile !== 'user' && member.runtime === 'hermes');
      renderMembers();
    } catch (error) {
      if (generation !== epoch || !dialog.open) return;
      members = [];
      renderMembers();
      report(registerStatus, `Hermes 成员列表读取失败：${message(error)}。`, true);
    }
  }
  async function reload(quiet = false) {
    const generation = ++epoch;
    await Promise.all([loadNodes(generation, quiet), loadMembers(generation)]);
  }
  async function open() {
    if (dialog.open) return;
    dialog.showModal();
    clearIssued();
    report(nodesStatus, '');
    report(registerStatus, '');
    if (!allowed()) {
      renderReadonly();
      close.focus();
      return;
    }
    renderAdmin();
    await reload();
    if (dialog.open && allowed()) memberSelect.focus();
  }
  async function registerNode() {
    if (busy || !allowed()) return;
    const member = selectedMember();
    if (!member) {
      report(registerStatus, '请选择 Hermes 成员。', true);
      return;
    }
    if (nodes.some(node => node.member === member.profile)) {
      report(registerStatus, '该成员已有节点，请使用节点卡片中的“重新签发”。', true);
      return;
    }
    busy = true;
    refreshControls();
    report(registerStatus, '正在登记…');
    try {
      const data = await api('/relay/nodes', {
        method: 'POST',
        body: JSON.stringify({member: member.profile, display_name: member.display_name || member.profile}),
      });
      if (!dialog.open || !allowed()) return;
      if (data.member !== member.profile || !data.credential) throw new Error('登记响应缺少成员标识或凭据');
      showIssued(member.profile, data.credential);
      report(registerStatus, `已登记 ${member.profile}；凭据仅显示一次，请立即复制。`);
      await reload();
    } catch (error) {
      if (dialog.open && allowed()) {
        const lost = !error.status;
        report(registerStatus, lost
          ? `登记请求结果未确认：${message(error)}。请先点击刷新核对节点列表，勿直接重复登记。`
          : `登记失败：${message(error)}。`, true);
      }
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function revokeNode(member) {
    if (busy || !allowed() || !nodes.some(node => node.member === member)) return;
    if (!await showConfirm(`吊销 ${member} 的连接凭据？当前在线连接会立即断开，节点需重新签发凭据才能上线。`)) return;
    if (busy || !allowed() || !dialog.open) return;
    busy = true;
    refreshControls();
    report(nodesStatus, `正在吊销 ${member} 的凭据…`);
    try {
      await api(`/relay/nodes/${encodeURIComponent(member)}/revoke`, {method: 'POST'});
      if (issued?.member === member) clearIssued();
      if (dialog.open) report(nodesStatus, `已吊销 ${member} 的凭据；节点将离线，需重新签发才能上线。`);
      await reload(true);
    } catch (error) {
      if (dialog.open) report(nodesStatus, `吊销失败：${message(error)}。`, true);
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function reissueNode(member) {
    if (busy || !allowed() || !nodes.some(node => node.member === member)) return;
    if (!await showConfirm(`重新签发 ${member} 的连接凭据？旧凭据立即失效，新凭据只显示一次。`)) return;
    if (busy || !allowed() || !dialog.open) return;
    const node = nodes.find(item => item.member === member);
    busy = true;
    refreshControls();
    report(nodesStatus, `正在重新签发 ${member} 的凭据…`);
    try {
      const data = await api('/relay/nodes', {
        method: 'POST',
        body: JSON.stringify({member, display_name: node.display_name || member}),
      });
      if (!dialog.open || !allowed()) return;
      if (data.member !== member || !data.credential) throw new Error('重新签发响应缺少凭据');
      showIssued(member, data.credential);
      report(nodesStatus, `已重新签发 ${member} 的凭据；旧凭据立即失效，新凭据仅显示一次。`);
      await reload(true);
    } catch (error) {
      if (dialog.open && allowed()) {
        const lost = !error.status;
        report(nodesStatus, lost
          ? `重新签发结果未确认：${message(error)}。请先刷新节点列表核对，勿直接重复签发。`
          : `重新签发失败：${message(error)}。`, true);
      }
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function copy(value, label) {
    try {
      await navigator.clipboard.writeText(value);
      report(registerStatus, `已复制${label}。`);
    } catch {
      try {
        const area = document.createElement('textarea');
        area.value = value;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.append(area);
        area.select();
        const ok = document.execCommand('copy');
        area.remove();
        if (!ok) throw new Error('copy failed');
        report(registerStatus, `已复制${label}。`);
      } catch {
        report(registerStatus, `复制${label}失败，请手动选择文本。`, true);
      }
    }
  }

  entry.addEventListener('click', open);
  close.addEventListener('click', () => dialog.close());
  refresh.addEventListener('click', () => {
    if (busy || !allowed()) return;
    clearIssued();
    report(registerStatus, '');
    reload();
  });
  register.addEventListener('click', registerNode);
  memberSelect.addEventListener('change', refreshControls);
  list.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button || busy) return;
    const card = button.closest('[data-relay-member]');
    if (!card) return;
    if (button.dataset.action === 'revoke') revokeNode(card.dataset.relayMember);
    else if (button.dataset.action === 'reissue') reissueNode(card.dataset.relayMember);
  });
  $('#relay-copy-member').addEventListener('click', () => {
    if (issued) copy(issued.member, '成员标识');
  });
  $('#relay-copy-credential').addEventListener('click', () => {
    if (issued) copy(issued.credential, '连接凭据');
  });
  dialog.addEventListener('cancel', event => {
    if (busy) event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    epoch++;
    clearIssued();
    list.replaceChildren();
    nodes = [];
    members = [];
    report(nodesStatus, '');
    report(registerStatus, '');
    anchor?.focus({preventScroll: true});
  });
  function visibility() {
    const loginVisible = !document.querySelector('#login').hidden;
    entry.hidden = loginVisible;
    if (loginVisible && dialog.open) dialog.close();
  }
  document.addEventListener('service-auth-changed', () => {
    visibility();
    if (dialog.open && !allowed()) renderReadonly();
  });
  visibility();
})();
