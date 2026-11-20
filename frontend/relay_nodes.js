/* Remote Hermes relay node administration with member-level source bindings. */
(() => {
  const entry = document.querySelector('#btn-relay-nodes');
  const anchor = document.querySelector('#btn-settings');
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;

  const dialog = document.createElement('dialog');
  dialog.id = 'relay-nodes-dialog';
  dialog.setAttribute('aria-labelledby', 'relay-nodes-title');
  dialog.innerHTML = `
    <h3 id="relay-nodes-title">远程节点管理</h3>
    <p class="profile-help">管理 Hermes 远程节点的连接凭据和来源 IP；凭据只在签发成功后显示一次，关闭或刷新后不再显示。</p>
    <p id="relay-readonly" class="profile-help" role="note" hidden>当前为 Bot 身份，仅可查看说明；远程节点管理需要管理员登录。</p>
    <div id="relay-admin-body">
      <div class="relay-toolbar">
        <button type="button" id="relay-refresh">刷新</button>
        <span id="relay-nodes-status" class="profile-help" role="status" aria-live="polite"></span>
      </div>
      <section id="relay-listen" class="relay-listen" aria-labelledby="relay-listen-title">
        <div class="relay-listen-head">
          <h4 id="relay-listen-title">引擎远程监听</h4>
          <span id="relay-listen-badge" class="relay-badge offline">读取中</span>
        </div>
        <p id="relay-listen-summary" class="profile-help" role="status" aria-live="polite"></p>
        <div class="relay-listen-controls">
          <label class="relay-field" for="relay-listen-address">本机网卡地址
            <select id="relay-listen-address"></select>
          </label>
          <button type="button" id="relay-listen-save" class="primary" disabled>保存并开启监听</button>
          <button type="button" id="relay-listen-disable" disabled>停用远程监听</button>
        </div>
        <div id="relay-listen-url-row" class="relay-copy-row" hidden>
          <input id="relay-listen-url" readonly autocomplete="off" spellcheck="false"
                 aria-label="ECS 插件引擎地址">
          <button type="button" id="relay-listen-copy">复制地址</button>
        </div>
        <p id="relay-listen-sources" class="profile-help"></p>
        <p class="profile-help">只绑定所选网卡的具体单播地址，禁止 0.0.0.0 与 ::；本机回环始终可用。启用前需至少绑定一个来源 IP。</p>
        <p id="relay-listen-status" class="profile-help" role="status" aria-live="polite"></p>
      </section>
      <div id="relay-node-list" aria-live="polite"></div>
      <section id="relay-register" class="relay-register">
        <h4>登记新节点</h4>
        <p class="profile-help">一名 Hermes 成员对应一个节点；同时登记该节点允许的来源 IP。已登记成员请在节点卡片中重新签发或添加 IP。</p>
        <div class="relay-register-line">
          <label class="relay-field" for="relay-member">Hermes 成员
            <select id="relay-member"></select>
          </label>
          <label class="relay-field" for="relay-register-ip">来源 IP
            <input id="relay-register-ip" autocomplete="off" spellcheck="false" placeholder="例如 100.64.0.5">
          </label>
          <button type="button" id="relay-register-submit" class="primary" disabled>登记</button>
        </div>
        <p id="relay-register-gate" class="profile-help" role="status" aria-live="polite"></p>
        <p id="relay-register-status" class="profile-help" role="status" aria-live="polite"></p>
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
  const listenBadge = $('#relay-listen-badge');
  const listenSummary = $('#relay-listen-summary');
  const listenAddress = $('#relay-listen-address');
  const listenSave = $('#relay-listen-save');
  const listenDisable = $('#relay-listen-disable');
  const listenUrlRow = $('#relay-listen-url-row');
  const listenUrl = $('#relay-listen-url');
  const listenSources = $('#relay-listen-sources');
  const listenStatus = $('#relay-listen-status');
  const listenCopy = $('#relay-listen-copy');
  const memberSelect = $('#relay-member');
  const registerIp = $('#relay-register-ip');
  const register = $('#relay-register-submit');
  const registerGate = $('#relay-register-gate');
  const registerStatus = $('#relay-register-status');
  const credentialPanel = $('#relay-credential');
  const issuedMember = $('#relay-issued-member');
  const issuedCredential = $('#relay-issued-credential');
  const close = $('#relay-close');

  let epoch = 0, busy = false, nodes = [], members = [], issued = null;
  let bindings = new Map();
  let bindingsError = '';
  let nodesReady = false, membersReady = false, bindingsReady = false;
  let loading = false, writeUncertain = false;
  let interfaces = [], listenState = null, interfacesReady = false, statusReady = false;
  let listenLoading = false, listenUncertain = false, listenError = '';

  function report(target, message, error = false) {
    target.textContent = message;
    target.dataset.error = String(error);
    target.setAttribute('role', error ? 'alert' : 'status');
  }
  function message(error) {
    if (error?.status === 403) return '当前身份无权管理远程节点（需要管理员登录）';
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
  function bindingFor(member) {
    return bindings.get(member) || [];
  }
  function addLocalBinding(member, ip) {
    if (!bindingFor(member).includes(ip)) bindings.set(member, [...bindingFor(member), ip]);
  }
  function registrationReady() {
    return nodesReady && membersReady && bindingsReady && !loading && !writeUncertain;
  }
  function isLoopback(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return normalized === '127.0.0.1' || normalized === '::1'
      || normalized === '::ffff:127.0.0.1';
  }
  function isIp(value) {
    const text = String(value || '').trim();
    if (!text || /\s/.test(text)) return false;
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(text)) {
      return text.split('.').every(part => Number(part) <= 255);
    }
    if (!text.includes(':')) return false;
    try {
      const parsed = new URL(`http://[${text}]`);
      return parsed.hostname.startsWith('[') && parsed.hostname.endsWith(']');
    } catch {
      return false;
    }
  }
  function hostPort(address, port) {
    if (!address) return '';
    const host = String(address).includes(':') ? `[${address}]` : String(address);
    return port ? `${host}:${port}` : host;
  }
  function engineHttpUrl(state) {
    return state?.remote_enabled && state.remote_address && state.remote_port
      ? `http://${hostPort(state.remote_address, state.remote_port)}` : '';
  }
  function listenReady() {
    return interfacesReady && statusReady && !listenLoading && !listenUncertain;
  }
  function allowedSourceIps() {
    const seen = new Set();
    for (const ips of bindings.values()) {
      for (const ip of ips) seen.add(ip);
    }
    return [...seen].sort();
  }
  function renderListenOptions() {
    const previous = listenAddress.value || listenState?.remote_address || '';
    listenAddress.replaceChildren();
    if (!interfacesReady) {
      listenAddress.append(new Option('正在读取本机网卡地址…', ''));
      return;
    }
    const list = interfaces.filter(item => item && item.address);
    if (!list.length) {
      listenAddress.append(new Option('未发现可用网卡地址', ''));
      return;
    }
    listenAddress.append(new Option('选择本机网卡地址', ''));
    const values = new Set();
    for (const item of list) {
      values.add(item.address);
      listenAddress.append(new Option(`${item.address} · ${item.family || 'IP'}`, item.address));
    }
    if (previous && !values.has(previous) && listenState?.remote_enabled) {
      listenAddress.append(new Option(`${previous}（当前监听）`, previous));
      values.add(previous);
    }
    listenAddress.value = values.has(previous) ? previous : '';
  }
  function renderListen() {
    const enabled = !!(listenState?.remote_enabled && listenState.remote_address);
    listenBadge.className = `relay-badge ${enabled && !listenUncertain ? 'online' : 'offline'}`;
    listenBadge.textContent = listenUncertain ? '结果未确认'
      : listenLoading && !listenState ? '读取中' : enabled ? '已开启' : '仅本机';
    let summary = '', summaryError = false;
    if (listenUncertain) {
      summary = '监听切换结果未确认；请点击刷新核对实际监听状态。';
      summaryError = true;
    } else if (listenError) {
      summary = `监听配置读取失败：${listenError}。请刷新重试。`;
      summaryError = true;
    } else if (listenLoading && !listenState) {
      summary = '正在读取监听状态…';
    } else if (!listenState) {
      summary = '监听状态未知，请刷新重试。';
      summaryError = true;
    } else if (enabled) {
      summary = `实际监听 ${hostPort(listenState.remote_address, listenState.remote_port)}（运行结果）`;
    } else {
      summary = `未开启远程监听；本机回环 ${listenState.loopback || '127.0.0.1'} 可用。`;
      if (listenState.error) {
        summary = `未开启远程监听：${listenState.error}`;
        summaryError = true;
      }
    }
    report(listenSummary, summary, summaryError);

    const url = listenUncertain ? '' : engineHttpUrl(listenState);
    listenUrl.value = url;
    listenUrlRow.hidden = !url;
    listenCopy.disabled = busy || !url;

    if (!bindingsReady) {
      report(listenSources, bindingsError
        ? `允许来源读取失败：${bindingsError}。`
        : '允许来源：正在读取…', !!bindingsError);
    } else {
      const ips = allowedSourceIps();
      report(listenSources, ips.length
        ? `允许来源：${ips.join('、')}（本机回环始终放行）。`
        : '允许来源：尚未绑定；请先在节点卡片绑定来源 IP，再开启远程监听。', false);
    }
    renderListenOptions();
  }
  function updateGate() {
    let text = '', error = false;
    if (loading) {
      text = '正在读取节点、成员和来源 IP，登记暂不可用。';
    } else if (!nodesReady) {
      text = '节点列表读取失败，登记暂不可用；请刷新成功后再登记。';
      error = true;
    } else if (!membersReady) {
      text = '成员列表读取失败，登记暂不可用；请刷新重试。';
      error = true;
    } else if (!bindingsReady) {
      text = '来源 IP 绑定读取失败，登记暂不可用；请刷新重试。';
      error = true;
    } else if (writeUncertain) {
      text = '上次操作结果未确认，请先刷新核对，暂不可重复登记。';
      error = true;
    }
    report(registerGate, text, error);
  }
  function refreshControls() {
    const ready = registrationReady();
    refresh.disabled = busy || loading;
    memberSelect.disabled = busy;
    registerIp.disabled = busy || loading || !ready;
    close.disabled = busy;
    register.disabled = busy || !ready || !selectedMember() || !registerIp.value.trim();
    for (const button of list.querySelectorAll('button')) {
      button.disabled = busy || !ready;
    }
    for (const input of list.querySelectorAll('input')) {
      input.disabled = busy || !ready;
    }
    const lready = listenReady();
    listenAddress.disabled = busy || loading || !lready;
    listenSave.disabled = busy || loading || !lready || !listenAddress.value;
    listenDisable.disabled = busy || loading || !lready || !listenState?.remote_enabled;
    listenCopy.disabled = busy || !listenUrl.value;
    updateGate();
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
    credentialPanel.scrollIntoView({block: 'center'});
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
  function renderNodesUnavailable() {
    list.replaceChildren();
    const empty = document.createElement('p');
    empty.className = 'profile-help relay-empty';
    empty.textContent = '节点列表暂不可用；请刷新重试。';
    list.append(empty);
  }
  function sourceSection(node) {
    const section = document.createElement('div');
    section.className = 'relay-node-source';
    const head = document.createElement('div');
    head.className = 'relay-source-head';
    const title = document.createElement('span');
    title.className = 'relay-source-title';
    title.textContent = '允许来源 IP';
    const add = document.createElement('button');
    add.type = 'button';
    add.dataset.action = 'add-ip';
    add.textContent = '添加 IP';
    head.append(title, add);

    const ips = bindingFor(node.member);
    const ipList = document.createElement('div');
    ipList.className = 'relay-ip-list';
    if (!bindingsReady) {
      const note = document.createElement('span');
      note.className = 'profile-help';
      note.textContent = bindingsError ? `来源 IP 读取失败：${bindingsError}` : '正在读取来源 IP…';
      ipList.append(note);
    } else if (!ips.length) {
      const note = document.createElement('span');
      note.className = 'profile-help relay-source-empty';
      note.textContent = '未绑定来源 IP；该节点无法从远程连接。';
      ipList.append(note);
    } else {
      for (const ip of ips) {
        const chip = document.createElement('span');
        chip.className = 'relay-ip-chip';
        const text = document.createElement('span');
        text.textContent = ip;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.dataset.action = 'remove-ip';
        remove.dataset.ip = ip;
        remove.setAttribute('aria-label', `移除来源 IP ${ip}`);
        remove.textContent = '×';
        chip.append(text, remove);
        ipList.append(chip);
      }
    }

    const form = document.createElement('form');
    form.className = 'relay-ip-add';
    form.dataset.ipForm = 'true';
    form.hidden = true;
    const input = document.createElement('input');
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.placeholder = '来源 IP';
    input.setAttribute('aria-label', `为 ${node.member} 添加来源 IP`);
    const save = document.createElement('button');
    save.type = 'submit';
    save.textContent = '保存';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.dataset.action = 'cancel-ip';
    cancel.textContent = '取消';
    const status = document.createElement('span');
    status.className = 'profile-help relay-source-status';
    status.dataset.role = 'ip-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    form.append(input, save, cancel, status);

    section.append(head, ipList, form);
    return section;
  }
  function renderNodes() {
    list.replaceChildren();
    if (!nodes.length) {
      const empty = document.createElement('p');
      empty.className = 'profile-help relay-empty';
      empty.textContent = '暂无已登记节点。选择 Hermes 成员并填写来源 IP 完成首次登记。';
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
      badge.textContent = revoked ? '离线 · 凭据已吊销' : online ? '在线' : '离线';
      head.append(name, badge);

      const meta = document.createElement('div');
      meta.className = 'relay-node-meta';
      const fields = [
        ['成员', node.member],
        ['代次', node.generation ?? '—'],
        ['最近心跳', fmtHeartbeat(node.last_heartbeat)],
        ['占用', node.capacity_used ?? 0],
        ['MCP 指纹', node.mcp_fingerprint || '未上报'],
        ['实际连接 IP', node.peer_ip || '尚未连接'],
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
      card.append(head, meta, sourceSection(node), actions);
      list.append(card);
    }
    refreshControls();
  }
  function renderReadonly() {
    epoch++;
    clearIssued();
    nodes = [];
    members = [];
    bindings = new Map();
    bindingsError = '';
    nodesReady = false;
    membersReady = false;
    bindingsReady = false;
    loading = false;
    writeUncertain = false;
    interfaces = [];
    listenState = null;
    interfacesReady = false;
    statusReady = false;
    listenLoading = false;
    listenUncertain = false;
    listenError = '';
    list.replaceChildren();
    renderListen();
    report(listenStatus, '');
    report(nodesStatus, '');
    report(registerStatus, '');
    report(registerGate, '');
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
      if (generation !== epoch || !dialog.open) return false;
      if (!Array.isArray(data)) throw new Error('远程节点响应格式异常');
      nodes = data;
      renderNodes();
      if (!quiet) report(nodesStatus, nodes.length
        ? `已加载 ${nodes.length} 个节点 · 更新于 ${new Date().toLocaleTimeString()}`
        : '暂无已登记节点。');
      return true;
    } catch (error) {
      if (generation !== epoch || !dialog.open) return false;
      nodes = [];
      renderNodesUnavailable();
      report(nodesStatus, `读取远程节点失败：${message(error)}。点击刷新重试。`, true);
      return false;
    } finally {
      if (generation === epoch) refreshControls();
    }
  }
  async function loadMembers(generation) {
    try {
      const data = await api('/members', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (generation !== epoch || !dialog.open) return false;
      members = (data.members || []).filter(member =>
        member && member.profile && member.profile !== 'user' && member.runtime === 'hermes');
      renderMembers();
      return true;
    } catch (error) {
      if (generation !== epoch || !dialog.open) return false;
      members = [];
      renderMembers();
      report(registerStatus, `Hermes 成员列表读取失败：${message(error)}。`, true);
      return false;
    }
  }
  async function loadBindings(generation) {
    try {
      const data = await api('/admin/source-ips', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (generation !== epoch || !dialog.open) return false;
      if (!Array.isArray(data.bindings)) throw new Error('来源 IP 绑定响应格式异常');
      bindings = new Map();
      for (const item of data.bindings) {
        if (item && item.member && Array.isArray(item.ips)) {
          bindings.set(item.member, item.ips.slice());
        }
      }
      bindingsError = '';
      renderNodes();
      return true;
    } catch (error) {
      if (generation !== epoch || !dialog.open) return false;
      bindings = new Map();
      bindingsError = message(error);
      renderNodes();
      return false;
    }
  }
  async function loadListen(generation) {
    listenLoading = true;
    interfacesReady = false;
    statusReady = false;
    listenError = '';
    renderListen();
    refreshControls();
    try {
      const [ifaceData, statusData] = await Promise.all([
        api('/admin/listen/interfaces', {cache: 'no-store', signal: AbortSignal.timeout(12000)}),
        api('/admin/listen/status', {cache: 'no-store', signal: AbortSignal.timeout(12000)}),
      ]);
      if (generation !== epoch || !dialog.open) return false;
      if (!Array.isArray(ifaceData?.interfaces)) throw new Error('网卡枚举响应格式异常');
      if (!statusData || typeof statusData !== 'object' || !('remote_enabled' in statusData)) {
        throw new Error('监听状态响应格式异常');
      }
      interfaces = ifaceData.interfaces.filter(item =>
        item && typeof item.address === 'string' && item.address);
      listenState = statusData;
      interfacesReady = true;
      statusReady = true;
      listenError = '';
      if (listenStatus.dataset.error === 'true') report(listenStatus, '');
      renderListen();
      return true;
    } catch (error) {
      if (generation !== epoch || !dialog.open) return false;
      interfaces = [];
      listenState = null;
      listenError = message(error);
      report(listenStatus, `读取监听配置失败：${listenError}。`, true);
      renderListen();
      return false;
    } finally {
      if (generation === epoch) {
        listenLoading = false;
        if (interfacesReady && statusReady) listenUncertain = false;
        refreshControls();
      }
    }
  }
  async function reload(quiet = false) {
    const generation = ++epoch;
    loading = true;
    nodesReady = false;
    membersReady = false;
    bindingsReady = false;
    bindingsError = '';
    listenLoading = true;
    interfacesReady = false;
    statusReady = false;
    listenError = '';
    refreshControls();
    const results = await Promise.all([
      loadNodes(generation, quiet),
      loadMembers(generation),
      loadBindings(generation),
      loadListen(generation),
    ]);
    if (generation !== epoch || !dialog.open) return;
    [nodesReady, membersReady, bindingsReady] = results;
    loading = false;
    listenLoading = false;
    if (nodesReady && membersReady && bindingsReady) writeUncertain = false;
    renderNodes();
    renderMembers();
    renderListen();
    refreshControls();
  }
  async function open() {
    if (dialog.open) return;
    dialog.showModal();
    clearIssued();
    nodes = [];
    members = [];
    bindings = new Map();
    bindingsError = '';
    nodesReady = false;
    membersReady = false;
    bindingsReady = false;
    loading = true;
    writeUncertain = false;
    interfaces = [];
    listenState = null;
    interfacesReady = false;
    statusReady = false;
    listenLoading = true;
    listenUncertain = false;
    listenError = '';
    report(nodesStatus, '');
    report(registerStatus, '');
    report(listenStatus, '');
    renderListen();
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
    if (busy || !allowed() || !registrationReady()) return;
    const member = selectedMember();
    const ip = registerIp.value.trim();
    if (!member) {
      report(registerStatus, '请选择 Hermes 成员。', true);
      return;
    }
    if (!ip) {
      report(registerStatus, '请填写该节点允许的来源 IP。', true);
      registerIp.focus();
      return;
    }
    if (!isIp(ip)) {
      report(registerStatus, '来源 IP 格式不正确，请填写 IPv4 或 IPv6 地址。', true);
      registerIp.focus();
      return;
    }
    if (isLoopback(ip)) {
      report(registerStatus, '本机回环地址始终放行，无需登记。', true);
      registerIp.focus();
      return;
    }
    if (nodes.some(node => node.member === member.profile)) {
      report(registerStatus, '该成员已有节点，请使用节点卡片中的“重新签发”或“添加 IP”。', true);
      return;
    }
    busy = true;
    refreshControls();
    report(registerStatus, '正在登记成员并绑定来源 IP…');
    try {
      const data = await api('/relay/nodes', {
        method: 'POST',
        body: JSON.stringify({member: member.profile, display_name: member.display_name || member.profile}),
      });
      if (!dialog.open || !allowed()) return;
      if (data.member !== member.profile || !data.credential) throw new Error('登记响应缺少成员标识或凭据');
      writeUncertain = false;
      showIssued(member.profile, data.credential);
      try {
        const bound = await api('/admin/source-ips/add', {
          method: 'POST',
          body: JSON.stringify({member: member.profile, ip}),
        });
        if (bound.member !== member.profile || !bound.ip) throw new Error('来源 IP 绑定响应格式异常');
        addLocalBinding(member.profile, bound.ip);
        writeUncertain = false;
        report(registerStatus, `已登记 ${member.profile} 并绑定来源 IP ${bound.ip}；凭据仅显示一次，请立即复制。`);
        registerIp.value = '';
      } catch (bindError) {
        if (!bindError.status) writeUncertain = true;
        report(registerStatus,
          `成员 ${member.profile} 已登记，但来源 IP 绑定失败：${message(bindError)}。请在节点卡片中重试添加 IP；凭据仅显示一次。`, true);
      }
      await reload(true);
    } catch (error) {
      if (dialog.open && allowed()) {
        const lost = !error.status;
        if (lost) writeUncertain = true;
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
    if (busy || !allowed() || !registrationReady() || !nodes.some(node => node.member === member)) return;
    if (!await showConfirm(`吊销 ${member} 的连接凭据？当前在线连接会立即断开，节点需重新签发凭据才能上线。`)) return;
    if (busy || !allowed() || !dialog.open || !registrationReady()) return;
    busy = true;
    refreshControls();
    report(nodesStatus, `正在吊销 ${member} 的凭据…`);
    try {
      await api(`/relay/nodes/${encodeURIComponent(member)}/revoke`, {method: 'POST'});
      writeUncertain = false;
      if (issued?.member === member) clearIssued();
      if (dialog.open) report(nodesStatus, `已吊销 ${member} 的凭据；节点将离线，需重新签发才能上线。`);
      await reload(true);
    } catch (error) {
      if (dialog.open) {
        if (!error.status) writeUncertain = true;
        report(nodesStatus, `${error.status ? '吊销失败' : '吊销结果未确认'}：${message(error)}${error.status ? '' : '。请先刷新核对，勿直接重复操作'}。`, true);
      }
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function reissueNode(member) {
    if (busy || !allowed() || !registrationReady() || !nodes.some(node => node.member === member)) return;
    if (!await showConfirm(`重新签发 ${member} 的连接凭据？旧凭据立即失效，凭据只显示一次。`)) return;
    if (busy || !allowed() || !dialog.open || !registrationReady()) return;
    const node = nodes.find(item => item.member === member);
    busy = true;
    refreshControls();
    report(nodesStatus, `正在重新签发 ${member} 的凭据…`);
    try {
      const data = await api('/relay/nodes', {
        method: 'POST',
        body: JSON.stringify({member, display_name: node.display_name || member, rotate: true}),
      });
      if (!dialog.open || !allowed()) return;
      if (data.member !== member || !data.credential) throw new Error('重新签发响应缺少凭据');
      writeUncertain = false;
      showIssued(member, data.credential);
      report(nodesStatus, `已重新签发 ${member} 的凭据；旧凭据立即失效，新凭据仅显示一次。`);
      await reload(true);
    } catch (error) {
      if (dialog.open && allowed()) {
        const lost = !error.status;
        if (lost) writeUncertain = true;
        report(nodesStatus, lost
          ? `重新签发结果未确认：${message(error)}。请先刷新节点列表核对，勿直接重复签发。`
          : `重新签发失败：${message(error)}。`, true);
      }
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function addIp(card, form) {
    if (busy || !allowed() || !registrationReady()) return;
    const member = card.dataset.relayMember;
    const input = form.querySelector('input');
    const status = form.querySelector('[data-role="ip-status"]');
    const ip = input.value.trim();
    if (!ip) {
      report(status, '请填写来源 IP。', true);
      input.focus();
      return;
    }
    if (!isIp(ip)) {
      report(status, '来源 IP 格式不正确，请填写 IPv4 或 IPv6 地址。', true);
      input.focus();
      return;
    }
    if (isLoopback(ip)) {
      report(status, '本机回环地址始终放行，无需登记。', true);
      input.focus();
      return;
    }
    const existing = bindingFor(member);
    if (existing.includes(ip)) {
      report(status, '该来源 IP 已绑定到此节点。', true);
      return;
    }
    busy = true;
    refreshControls();
    report(status, '正在保存来源 IP…');
    try {
      const data = await api('/admin/source-ips/add', {
        method: 'POST',
        body: JSON.stringify({member, ip}),
      });
      if (data.member !== member || !data.ip) throw new Error('来源 IP 绑定响应格式异常');
      addLocalBinding(member, data.ip);
      writeUncertain = false;
      report(registerStatus, `已为 ${member} 添加来源 IP ${data.ip}；立即生效。`);
      await reload(true);
    } catch (error) {
      if (!error.status) writeUncertain = true;
      report(status, `添加来源 IP 失败：${message(error)}。`, true);
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function removeIp(member, ip) {
    if (busy || !allowed() || !registrationReady()) return;
    busy = true;
    refreshControls();
    report(registerStatus, `正在移除 ${member} 的来源 IP ${ip}…`);
    try {
      await api(`/admin/source-ips/${encodeURIComponent(member)}/${encodeURIComponent(ip)}`, {method: 'DELETE'});
      bindings.set(member, bindingFor(member).filter(item => item !== ip));
      writeUncertain = false;
      report(registerStatus, `已移除 ${member} 的来源 IP ${ip}；立即生效。`);
      await reload(true);
    } catch (error) {
      if (!error.status) writeUncertain = true;
      report(registerStatus, `移除来源 IP 失败：${message(error)}。`, true);
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function applyListen(address) {
    if (busy || !allowed() || !listenReady()) return;
    busy = true;
    refreshControls();
    report(listenStatus, address ? `正在切换监听至 ${address}…` : '正在停用远程监听…');
    try {
      const data = await api('/admin/listen', {
        method: 'POST',
        body: JSON.stringify({address}),
        signal: AbortSignal.timeout(30000),
      });
      if (!dialog.open || !allowed()) return;
      if (!data || typeof data !== 'object' || !('remote_enabled' in data)) {
        throw new Error('监听响应格式异常');
      }
      listenState = data;
      listenUncertain = false;
      listenError = '';
      if (address) {
        if (data.remote_enabled) {
          report(listenStatus, `远程监听已开启：${hostPort(data.remote_address, data.remote_port)}。请复制上方引擎地址到 ECS 插件。`);
        } else {
          report(listenStatus, `监听未开启：${data.error || '所选地址未能绑定，请检查网卡与端口。'}`, true);
        }
      } else if (data.remote_enabled) {
        report(listenStatus, '停用未完成：引擎仍显示远程监听。请刷新核对。', true);
      } else {
        report(listenStatus, '远程监听已停用，回退回环；本机仍可用。');
      }
      renderListen();
      await reload(true);
    } catch (error) {
      if (!error.status) listenUncertain = true;
      report(listenStatus, !error.status
        ? `监听切换结果未确认：${message(error)}。请先点击刷新核对。`
        : `监听切换失败：${message(error)}。`, true);
      renderListen();
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  function saveListen() {
    if (busy || !allowed() || !listenReady()) return;
    if (!listenAddress.value) {
      report(listenStatus, '请选择本机网卡地址。', true);
      listenAddress.focus();
      return;
    }
    applyListen(listenAddress.value);
  }
  function disableListen() {
    if (busy || !allowed() || !listenReady() || !listenState?.remote_enabled) return;
    applyListen('');
  }
  async function copy(value, label, target = registerStatus) {
    try {
      await navigator.clipboard.writeText(value);
      report(target, `已复制${label}。`);
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
        report(target, `已复制${label}。`);
      } catch {
        report(target, `复制${label}失败，请手动选择文本。`, true);
      }
    }
  }

  entry.addEventListener('click', open);
  close.addEventListener('click', () => dialog.close());
  listenSave.addEventListener('click', saveListen);
  listenDisable.addEventListener('click', disableListen);
  listenCopy.addEventListener('click', () => {
    if (listenUrl.value) copy(listenUrl.value, 'ECS 插件引擎地址', listenStatus);
  });
  listenAddress.addEventListener('change', refreshControls);
  refresh.addEventListener('click', () => {
    if (busy || loading || !allowed()) return;
    clearIssued();
    report(registerStatus, '');
    reload();
  });
  register.addEventListener('click', registerNode);
  registerIp.addEventListener('input', refreshControls);
  memberSelect.addEventListener('change', refreshControls);
  list.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const card = button.closest('[data-relay-member]');
    if (!card) return;
    if (button.dataset.action === 'add-ip') {
      if (busy || !registrationReady()) return;
      const form = card.querySelector('[data-ip-form]');
      if (!form) return;
      form.hidden = false;
      button.hidden = true;
      form.querySelector('input')?.focus();
      return;
    }
    if (button.dataset.action === 'cancel-ip') {
      const form = button.closest('[data-ip-form]');
      const add = card.querySelector('button[data-action="add-ip"]');
      if (form) form.hidden = true;
      if (add) add.hidden = false;
      return;
    }
    if (button.dataset.action === 'remove-ip') {
      removeIp(card.dataset.relayMember, button.dataset.ip);
      return;
    }
    if (button.dataset.action === 'revoke') revokeNode(card.dataset.relayMember);
    else if (button.dataset.action === 'reissue') reissueNode(card.dataset.relayMember);
  });
  list.addEventListener('submit', event => {
    const form = event.target.closest('[data-ip-form]');
    if (!form) return;
    event.preventDefault();
    const card = form.closest('[data-relay-member]');
    if (card) addIp(card, form);
  });
  $('#relay-copy-member').addEventListener('click', () => {
    if (issued) copy(issued.member, '成员标识');
  });
  $('#relay-copy-credential').addEventListener('click', () => {
    if (issued) copy(issued.credential, '连接凭据');
  });
  dialog.addEventListener('cancel', event => {
    if (event.target === dialog && busy) event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    epoch++;
    clearIssued();
    list.replaceChildren();
    nodes = [];
    members = [];
    bindings = new Map();
    bindingsError = '';
    nodesReady = false;
    membersReady = false;
    bindingsReady = false;
    loading = false;
    writeUncertain = false;
    interfaces = [];
    listenState = null;
    interfacesReady = false;
    statusReady = false;
    listenLoading = false;
    listenUncertain = false;
    listenError = '';
    report(nodesStatus, '');
    report(registerStatus, '');
    report(registerGate, '');
    report(listenStatus, '');
    renderListen();
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
