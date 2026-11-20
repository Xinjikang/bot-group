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
      <section id="relay-direction" class="relay-direction" aria-labelledby="relay-direction-title">
        <div class="relay-direction-head">
          <h4 id="relay-direction-title">连接方向</h4>
          <span id="relay-direction-badge" class="relay-badge offline">未选择节点</span>
        </div>
        <p class="profile-help">谁可达谁监听：云端能连到本机时选方向 A，本机能连到云端监听端口时选方向 B。同一节点同一时刻只保留一条活动通道。</p>
        <label class="relay-field" for="relay-direction-node">节点
          <select id="relay-direction-node"></select>
        </label>
        <div class="relay-direction-choices" role="group" aria-label="连接方向">
          <button type="button" id="relay-direction-listen" data-direction="listen" aria-pressed="false">
            <span class="relay-direction-title">让云端连接这台电脑</span>
            <span class="relay-direction-sub">云端插件 register → 本机监听</span>
            <span class="relay-direction-flag" data-flag="listen"></span>
          </button>
          <button type="button" id="relay-direction-dial" data-direction="dial" aria-pressed="false">
            <span class="relay-direction-title">让这台电脑连接云端</span>
            <span class="relay-direction-sub">本机引擎拨入云端插件监听</span>
            <span class="relay-direction-flag" data-flag="dial"></span>
          </button>
        </div>
        <div id="relay-direction-detail">
          <div class="relay-direction-addresses">
            <span><span class="relay-key">本机监听地址</span><span id="relay-direction-local">—</span></span>
            <span><span class="relay-key">对端拨号地址</span><span id="relay-direction-remote">—</span></span>
            <span><span class="relay-key">证书指纹</span><span id="relay-direction-fingerprint">—</span></span>
          </div>
          <ol class="relay-stages" aria-label="连接阶段">
            <li id="relay-stage-listen"><span>已监听</span><em>未开始</em></li>
            <li id="relay-stage-auth"><span>双方已认证</span><em>未开始</em></li>
            <li id="relay-stage-mcp"><span>MCP 已连通</span><em>未开始</em></li>
          </ol>
          <p id="relay-direction-status" class="profile-help" role="status" aria-live="polite"></p>
          <p id="relay-direction-failure" class="profile-help" role="alert"></p>
          <div class="relay-direction-actions">
            <button type="button" id="relay-direction-test">测试连接</button>
            <button type="button" id="relay-direction-verify" hidden>重新验证 MCP</button>
            <button type="button" id="relay-direction-retry">重试当前方向</button>
            <button type="button" id="relay-direction-back">切回另一方向</button>
          </div>
        </div>
        <section id="relay-pairing" class="relay-pairing" aria-labelledby="relay-pairing-title" hidden>
          <h5 id="relay-pairing-title">导入配对链接（方向 B）</h5>
          <p class="profile-help">粘贴云端插件 listen 输出的配对链接。先核对设备与短指纹，再由本机引擎预检可达性和证书；预检失败不会保存。</p>
          <div class="relay-pairing-line">
            <label class="relay-field" for="relay-pairing-url">配对链接
              <input id="relay-pairing-url" autocomplete="off" spellcheck="false"
                     placeholder="wss://example.com:18443?pairing_token=…&amp;fingerprint=…">
            </label>
            <button type="button" id="relay-pairing-parse">解析</button>
          </div>
          <div id="relay-pairing-preview" class="relay-pairing-preview" hidden>
            <p><span class="relay-key">设备</span><span id="relay-pairing-identity"></span></p>
            <p><span class="relay-key">入口</span><span id="relay-pairing-address"></span></p>
            <p><span class="relay-key">短指纹</span><span id="relay-pairing-fingerprint"></span></p>
            <div class="relay-direction-actions">
              <button type="button" id="relay-pairing-confirm" class="primary">确认并导入</button>
              <button type="button" id="relay-pairing-cancel">取消</button>
            </div>
          </div>
          <p id="relay-pairing-status" class="profile-help" role="status" aria-live="polite"></p>
        </section>
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
  const listenSection = $('#relay-listen');
  const directionBadge = $('#relay-direction-badge');
  const directionNode = $('#relay-direction-node');
  const directionListen = $('#relay-direction-listen');
  const directionDial = $('#relay-direction-dial');
  const directionDetail = $('#relay-direction-detail');
  const directionLocal = $('#relay-direction-local');
  const directionRemote = $('#relay-direction-remote');
  const directionFingerprint = $('#relay-direction-fingerprint');
  const directionStatus = $('#relay-direction-status');
  const directionFailure = $('#relay-direction-failure');
  const directionTest = $('#relay-direction-test');
  const directionVerify = $('#relay-direction-verify');
  const directionRetry = $('#relay-direction-retry');
  const directionBack = $('#relay-direction-back');
  const stageListen = $('#relay-stage-listen');
  const stageAuth = $('#relay-stage-auth');
  const stageMcp = $('#relay-stage-mcp');
  const pairingSection = $('#relay-pairing');
  const pairingUrl = $('#relay-pairing-url');
  const pairingParse = $('#relay-pairing-parse');
  const pairingPreview = $('#relay-pairing-preview');
  const pairingIdentity = $('#relay-pairing-identity');
  const pairingAddress = $('#relay-pairing-address');
  const pairingFingerprint = $('#relay-pairing-fingerprint');
  const pairingConfirm = $('#relay-pairing-confirm');
  const pairingCancel = $('#relay-pairing-cancel');
  const pairingStatus = $('#relay-pairing-status');
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
  let directionBusy = false, directionWatch = 0, pendingPairing = null, pairingBusy = false;
  let directionView = '';
  const mcpVerifyInflight = new Set();

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
  function directionLabel(direction) {
    return direction === 'dial' ? '让这台电脑连接云端' : '让云端连接这台电脑';
  }
  function selectedDirectionNode() {
    return nodes.find(node => node.member === directionNode.value) || null;
  }
  function nodeDirection(node) {
    return node?.direction === 'dial' ? 'dial' : 'listen';
  }
  function testedMap() {
    try {
      const raw = JSON.parse(localStorage.getItem('relay-direction-tested') || '{}');
      return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    } catch {
      return {};
    }
  }
  function testedDirection(member, direction) {
    return !!testedMap()?.[member]?.[direction];
  }
  function markTested(member, direction) {
    try {
      const map = testedMap();
      map[member] = {...(map[member] || {}), [direction]: true};
      localStorage.setItem('relay-direction-tested', JSON.stringify(map));
    } catch { /* 本地推荐标记不可用不影响连接 */ }
  }
  function stageState(item, state, text) {
    item.className = `relay-stage-${state}`;
    item.querySelector('em').textContent = text;
  }
  function stageLabel(stage) {
    return {dns: 'DNS 解析', tcp: 'TCP 连接', tls: 'TLS/证书', auth: '认证'}[stage] || '连接';
  }
  function endpointText(endpoint) {
    if (!endpoint?.host || !endpoint?.port) return '';
    const host = String(endpoint.host).includes(':') ? `[${endpoint.host}]` : endpoint.host;
    return `${endpoint.tls === false ? 'ws' : 'wss'}://${host}:${endpoint.port}`;
  }
  function renderDirection() {
    const node = selectedDirectionNode();
    const previous = directionNode.value;
    directionNode.replaceChildren();
    if (!nodes.length) {
      directionNode.append(new Option('暂无可选节点', ''));
      directionNode.disabled = true;
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = '未登记节点';
      directionDetail.hidden = true;
      pairingSection.hidden = true;
      stageListen.hidden = true;
      listenSection.hidden = false;
      directionListen.classList.remove('active');
      directionDial.classList.remove('active');
      directionListen.setAttribute('aria-pressed', 'false');
      directionDial.setAttribute('aria-pressed', 'false');
      const listenFlag = directionListen.querySelector('[data-flag="listen"]');
      const dialFlag = directionDial.querySelector('[data-flag="dial"]');
      listenFlag.hidden = true;
      dialFlag.hidden = true;
      report(directionStatus, '先登记远程节点，再选择连接方向。');
      report(directionFailure, '');
      return;
    }
    for (const item of nodes) {
      const label = `${item.display_name || item.member}（${item.member}）`;
      directionNode.append(new Option(label, item.member));
    }
    const keep = nodes.some(item => item.member === previous) ? previous
      : nodes.some(item => item.member === node?.member) ? node.member : nodes[0].member;
    directionNode.value = keep;
    directionNode.disabled = busy || loading || directionBusy || !nodes.length;
    const selected = selectedDirectionNode();
    if (!selected) return;
    const direction = directionView || nodeDirection(selected);
    const serverDirection = nodeDirection(selected);
    const revoked = !!selected.credential_revoked;
    const online = selected.state === 'online' && !revoked;
    const onlineForView = online && serverDirection === direction;
    const switching = selected.switch_state === 'switching';
    const endpoint = selected.dial_endpoint || null;
    const local = listenState?.remote_enabled
      ? hostPort(listenState.remote_address, listenState.remote_port) : '';
    const remote = endpointText(endpoint);

    directionListen.classList.toggle('active', direction === 'listen');
    directionDial.classList.toggle('active', direction === 'dial');
    directionListen.setAttribute('aria-pressed', String(direction === 'listen'));
    directionDial.setAttribute('aria-pressed', String(direction === 'dial'));
    const listenFlag = directionListen.querySelector('[data-flag="listen"]');
    const dialFlag = directionDial.querySelector('[data-flag="dial"]');
    listenFlag.textContent = serverDirection === 'listen' && online ? '已测通 · 推荐'
      : testedDirection(selected.member, 'listen') ? '已测通'
        : serverDirection === 'listen' ? '当前方向' : '';
    dialFlag.textContent = serverDirection === 'dial' && online ? '已测通 · 推荐'
      : testedDirection(selected.member, 'dial') ? '已测通'
        : serverDirection === 'dial' ? '当前方向' : '';
    listenFlag.hidden = !listenFlag.textContent;
    dialFlag.hidden = !dialFlag.textContent;

    if (revoked) {
      directionBadge.className = 'relay-badge revoked';
      directionBadge.textContent = '凭据已吊销';
    } else if (online) {
      directionBadge.className = 'relay-badge online';
      directionBadge.textContent = `已连接 · ${serverDirection === 'dial' ? 'B' : 'A'}`;
    } else if (selected.switch_error) {
      directionBadge.className = 'relay-badge revoked';
      directionBadge.textContent = '切换失败';
    } else if (switching) {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = '切换中';
    } else if (selected.dial_state === 'connecting' || selected.dial_state === 'backoff') {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = selected.dial_state === 'backoff' ? '拨号重试中' : '拨号中';
    } else if (selected.dial_state === 'abandoned') {
      directionBadge.className = 'relay-badge revoked';
      directionBadge.textContent = '拨号已停止';
    } else if (selected.offline_reason === 'unexpected') {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = '异常掉线';
    } else {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = '离线';
    }

    directionLocal.textContent = local || '仅本机回环（未开启远程监听）';
    directionRemote.textContent = remote || '未导入配对链接';
    const fingerprint = endpoint?.fingerprint_short || endpoint?.fingerprint?.slice(0, 8) || '';
    directionFingerprint.textContent = fingerprint ? `${fingerprint}…` : '—';

    const listening = direction === 'listen'
      ? !!listenState?.remote_enabled
      : !!endpoint && (onlineForView || selected.dial_state === 'connected');
    const authenticated = onlineForView;
    const mcpVerifying = mcpVerifyInflight.has(selected.member);
    const mcpVerified = selected.mcp_verify_state === 'ok';
    const mcpFailed = selected.mcp_verify_state === 'failed';
    const mcp = authenticated && mcpVerified;
    if (direction === 'dial' && !listening && endpoint) {
      stageState(stageListen, 'current', '已配置，待拨通');
    } else {
      stageState(stageListen, listening ? 'done' : 'pending', listening ? '已完成' : '待开始');
    }
    stageState(stageAuth, authenticated ? 'done' : listening ? 'current' : 'pending',
      authenticated ? '已完成' : listening ? '进行中' : '待开始');
    if (mcp) {
      stageState(stageMcp, 'done', '已完成');
    } else if (authenticated && mcpVerifying) {
      stageState(stageMcp, 'current', '验证中');
    } else if (authenticated && mcpFailed) {
      stageState(stageMcp, 'failed', '验证失败');
    } else if (authenticated) {
      stageState(stageMcp, 'current', '待验证');
    } else {
      stageState(stageMcp, 'pending', '待开始');
    }

    directionDetail.hidden = false;
    stageListen.hidden = false;
    listenSection.hidden = direction === 'dial';
    pairingSection.hidden = direction !== 'dial';
    directionTest.hidden = false;
    directionTest.textContent = direction === 'dial' ? '测试连接（引擎拨入）' : '测试连接（由云端 register 执行）';

    let status = '', statusError = false, failure = '', failureError = false;
    if (revoked) {
      status = '凭据已吊销；重新签发后才能建立任一方向的连接。';
      statusError = true;
    } else if (onlineForView) {
      if (mcpVerifying) {
        status = `已连通：${directionLabel(direction)}；MCP 只读验证中：经已认证通道调用随包成员工具…`;
      } else if (mcpFailed) {
        status = `已连通：${directionLabel(direction)}；MCP 只读验证失败：${selected.mcp_verify_error || '未知原因'}`;
      } else if (mcpVerified) {
        status = `已连通：${directionLabel(direction)}；MCP 只读验证已通过（已读取本项目名单）。`;
      } else {
        status = `已连通：${directionLabel(direction)}；MCP 待只读验证。`;
      }
    } else if (online) {
      status = `当前已连接：${directionLabel(serverDirection)}；目标${directionLabel(direction)}尚未切换。`;
    } else if (selected.switch_error) {
      status = `切换失败：${selected.switch_error}`;
      statusError = true;
    } else if (switching) {
      status = `切换中：目标为${directionLabel(selected.switch_target || direction)}。正在先断旧通道，再建立新通道；期间暂停派发。`;
    } else if (selected.offline_reason === 'switch') {
      status = `切换中断线：${directionLabel(direction)}尚未认证，等待新方向连接；不会按故障掉线告警。`;
    } else if (selected.offline_reason === 'unexpected') {
      status = '连接异常掉线：不是方向切换，按普通重连恢复。';
      statusError = true;
    } else if (selected.dial_state === 'backoff' || selected.dial_state === 'connecting') {
      status = `引擎正在向${remote || '云端监听地址'}拨号，失败会按退避重试。`;
    } else if (selected.dial_state === 'abandoned') {
      status = '拨号已停止：重试次数耗尽，请重试或切回另一方向。';
      statusError = true;
    } else {
      status = `当前方向：${directionLabel(direction)}；等待连接。`;
    }
    report(directionStatus, status, statusError);
    if (onlineForView && mcpVerifying) {
      failure = '';
    } else if (onlineForView && mcpFailed) {
      failure = `MCP 只读验证失败：${selected.mcp_verify_error || '未知原因'}`;
      failureError = true;
    } else if (!online && selected.dial_failure?.stage) {
      failure = `引擎拨号 ${selected.dial_failure.address || remote} 卡在${stageLabel(selected.dial_failure.stage)}：${selected.dial_failure.error || '未知错误'}`;
      failureError = true;
    } else if (!online && selected.switch_error) {
      failure = `失败原因：${selected.switch_error}`;
      failureError = true;
    } else if (!online && direction === 'listen' && !listenState?.remote_enabled) {
      failure = '本机监听未开启：先在上方选择网卡并开启监听，云端插件才能 register。';
      failureError = true;
    }
    report(directionFailure, failure, failureError);
    const failed = (!online && (selected.switch_error || selected.dial_failure?.stage
      || selected.dial_state === 'abandoned' || selected.offline_reason === 'unexpected'))
      || (onlineForView && mcpFailed);
    if (failed && pairingStatus.dataset.error !== 'true') report(pairingStatus, '');

    directionRetry.hidden = !(selected.switch_error || selected.dial_failure || selected.dial_state === 'abandoned'
      || selected.offline_reason === 'unexpected');
    directionBack.hidden = false;
    directionBack.textContent = direction === 'dial' ? '切回让云端连接这台电脑' : '切回让这台电脑连接云端';
    directionTest.disabled = busy || loading || directionBusy || revoked || direction !== 'dial'
      || !endpoint || switching;
    directionVerify.hidden = !(direction === 'dial' && onlineForView && mcpFailed);
    directionVerify.disabled = busy || loading || directionBusy || revoked || mcpVerifying;
    directionVerify.textContent = mcpVerifying ? 'MCP 验证中…' : '重新验证 MCP';
    directionRetry.disabled = busy || loading || directionBusy || revoked || !nodesReady;
    directionBack.disabled = busy || loading || directionBusy || revoked || !nodesReady;
    directionListen.disabled = busy || loading || directionBusy || !nodesReady;
    directionDial.disabled = busy || loading || directionBusy || !nodesReady;
    pairingSection.hidden = direction !== 'dial';
    pairingParse.disabled = busy || loading || directionBusy || pairingBusy || !nodesReady;
    pairingConfirm.disabled = busy || loading || directionBusy || pairingBusy || !pendingPairing;
    pairingPreview.hidden = !pendingPairing;
    if (pendingPairing) {
      pairingIdentity.textContent = `${selected.display_name || selected.member}（${selected.member}）`;
      pairingAddress.textContent = `wss://${pendingPairing.displayHost}:${pendingPairing.port}`;
      pairingFingerprint.textContent = `${pendingPairing.fingerprint.slice(0, 8)}…`;
    }
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
    const dirNode = selectedDirectionNode();
    const dirLocked = busy || loading || directionBusy || !dirNode;
    directionNode.disabled = busy || loading || directionBusy || !nodes.length;
    directionListen.disabled = dirLocked;
    directionDial.disabled = dirLocked;
    directionTest.disabled = dirLocked || !!dirNode?.credential_revoked
      || !dirNode?.dial_endpoint || dirNode?.switch_state === 'switching'
      || nodeDirection(dirNode) !== 'dial';
    directionVerify.disabled = dirLocked || !!dirNode?.credential_revoked
      || mcpVerifyInflight.has(dirNode?.member);
    directionRetry.disabled = dirLocked;
    directionBack.disabled = dirLocked;
    pairingParse.disabled = dirLocked || pairingBusy;
    pairingConfirm.disabled = dirLocked || pairingBusy || !pendingPairing;
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
    renderDirection();
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
    directionBusy = false;
    directionView = '';
    directionWatch++;
    pendingPairing = null;
    pairingBusy = false;
    pairingUrl.value = '';
    pairingPreview.hidden = true;
    list.replaceChildren();
    renderListen();
    renderDirection();
    report(listenStatus, '');
    report(directionStatus, '');
    report(directionFailure, '');
    report(pairingStatus, '');
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
        renderDirection();
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
    renderDirection();
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
    directionBusy = false;
    directionView = '';
    directionWatch++;
    pendingPairing = null;
    pairingBusy = false;
    pairingUrl.value = '';
    pairingPreview.hidden = true;
    report(directionStatus, '');
    report(directionFailure, '');
    report(pairingStatus, '');
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
    if (dialog.open && allowed()) {
      maybeVerifyMcp();
      memberSelect.focus();
    }
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
  function parsePairingLink(text) {
    const raw = String(text || '').trim();
    if (!raw) throw new Error('请粘贴配对链接。');
    let url;
    try {
      url = new URL(raw);
    } catch {
      throw new Error('配对链接格式不正确。');
    }
    if (url.protocol !== 'wss:') throw new Error('配对链接必须使用 wss://。');
    if (url.username || url.password) throw new Error('配对链接不能包含用户名或密码。');
    let host = url.hostname || '';
    if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
    if (!host) throw new Error('配对链接缺少云端地址。');
    if (host === '0.0.0.0' || host === '::') throw new Error('配对链接不能使用通配地址。');
    const port = Number(url.port || 443);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('配对链接端口无效。');
    const params = url.searchParams;
    const token = params.get('pairing_token') || '';
    if (!token) throw new Error('配对链接缺少一次性配对凭据。');
    const fingerprint = (params.get('fingerprint') || '').toLowerCase().replace(/:/g, '');
    if (!/^[0-9a-f]{64}$/.test(fingerprint)) throw new Error('配对链接的证书指纹格式不正确。');
    const member = params.get('member') || '';
    const selected = selectedDirectionNode();
    if (member && selected && member !== selected.member) {
      throw new Error(`链接属于成员 ${member}，与当前节点 ${selected.member} 不一致。`);
    }
    const displayHost = host.includes(':') ? `[${host}]` : host;
    return {host, displayHost, port, fingerprint, token, member};
  }
  function clearPairing() {
    pendingPairing = null;
    pairingPreview.hidden = true;
    pairingStatus.textContent = '';
    pairingStatus.dataset.error = 'false';
  }
  function parsePairing() {
    if (busy || loading || directionBusy || pairingBusy || !nodesReady) return;
    try {
      pendingPairing = parsePairingLink(pairingUrl.value);
      const selected = selectedDirectionNode();
      if (!selected) throw new Error('请先选择节点。');
      pairingPreview.hidden = false;
      pairingIdentity.textContent = `${selected.display_name || selected.member}（${selected.member}）`;
      pairingAddress.textContent = `wss://${pendingPairing.displayHost}:${pendingPairing.port}`;
      pairingFingerprint.textContent = `${pendingPairing.fingerprint.slice(0, 8)}…`;
      report(pairingStatus, '链接有效。请核对设备身份与短指纹，确认后先做可达性和证书预检。');
      refreshControls();
    } catch (error) {
      pendingPairing = null;
      pairingPreview.hidden = true;
      report(pairingStatus, `配对链接无效：${error.message}`, true);
      refreshControls();
    }
  }
  async function confirmPairing() {
    if (busy || loading || directionBusy || pairingBusy || !pendingPairing) return;
    const selected = selectedDirectionNode();
    if (!selected) {
      report(pairingStatus, '请先选择节点。', true);
      return;
    }
    pairingBusy = true;
    refreshControls();
    report(pairingStatus, '正在预检云端监听地址、TCP 和证书指纹…');
    const member = selected.member;
    try {
      const probe = await api(`/relay/nodes/${encodeURIComponent(member)}/dial-endpoint/probe`, {
        method: 'POST',
        body: JSON.stringify({host: pendingPairing.host, port: pendingPairing.port,
                              fingerprint: pendingPairing.fingerprint}),
        signal: AbortSignal.timeout(15000),
      });
      if (!probe || probe.ok !== true) {
        report(pairingStatus, `预检失败（${stageLabel(probe?.stage)}）：${probe?.error || '无法连接云端监听地址'}；未保存。`, true);
        return;
      }
      const saved = await api(`/relay/nodes/${encodeURIComponent(member)}/dial-endpoint`, {
        method: 'POST',
        body: JSON.stringify({host: pendingPairing.host, port: pendingPairing.port, tls: true,
                              fingerprint: pendingPairing.fingerprint,
                              pairing_token: pendingPairing.token}),
        signal: AbortSignal.timeout(15000),
      });
      if (!saved?.saved) throw new Error('拨号端点保存响应异常');
      report(pairingStatus, '预检通过，配对已保存；正在拨号并完成认证。');
      pendingPairing = null;
      pairingPreview.hidden = true;
      pairingUrl.value = '';
      await reload(true);
      if (dialog.open && allowed()) switchDirection('dial');
    } catch (error) {
      report(pairingStatus, `导入失败：${message(error)}。${pendingPairing ? '可核对后重试。' : ''}`, true);
    } finally {
      pairingBusy = false;
      if (dialog.open) refreshControls();
    }
  }
  function watchSwitch(member, target, token, generation) {
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    (async () => {
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await sleep(1000);
        if (token !== directionWatch || generation !== epoch || !dialog.open || !allowed()) return;
        const ok = await loadNodes(generation, true);
        if (!ok || token !== directionWatch) return;
        const node = nodes.find(item => item.member === member);
        if (!node) return;
        renderDirection();
        if (node.switch_state !== 'switching') {
          if (node.state === 'online' && nodeDirection(node) === target) {
            markTested(member, target);
            report(directionStatus, `切换完成：${directionLabel(target)}；双方已认证。`);
            if (target === 'dial') {
              report(pairingStatus, '配对导入完成：引擎已认证并连通云端监听。');
            }
            renderDirection();
            maybeVerifyMcp();
          } else {
            report(directionStatus, `切换未完成：${node.switch_error || '新方向尚未认证'}`, true);
            if (target === 'dial') {
              report(pairingStatus, `配对已保存，但拨号未连通：${node.switch_error || '新方向尚未认证'}`, true);
            }
            renderDirection();
          }
          return;
        }
      }
      if (token === directionWatch && generation === epoch) {
        report(directionStatus, '后台仍在切换中；请点击刷新核对，页面不会用计时器判定失败。');
        renderDirection();
      }
    })();
  }
  async function verifyMcp(member, {force = false} = {}) {
    if (!allowed() || mcpVerifyInflight.has(member)) return;
    const node = nodes.find(item => item.member === member);
    if (!node || node.state !== 'online' || nodeDirection(node) !== 'dial'
        || (!force && node.mcp_verify_state)) return;
    mcpVerifyInflight.add(member);
    if (dialog.open) renderDirection();
    try {
      report(directionStatus, 'MCP 只读验证中：经已认证通道调用随包成员工具读取本项目名单…');
      const result = await api(`/relay/nodes/${encodeURIComponent(member)}/mcp-verify`, {
        method: 'POST',
        signal: AbortSignal.timeout(30000),
      });
      if (!result || !['ok', 'failed'].includes(result.mcp_verify_state)) {
        throw new Error('MCP 验证响应格式异常');
      }
      await reload(true);
      if (!dialog.open || !allowed()) return;
      const fresh = nodes.find(item => item.member === member);
      if (fresh?.mcp_verify_state === 'failed') {
        report(directionFailure, `MCP 只读验证失败：${fresh.mcp_verify_error || '未知原因'}`, true);
      }
    } catch (error) {
      report(directionStatus, `MCP 只读验证失败：${message(error)}。`, true);
    } finally {
      mcpVerifyInflight.delete(member);
      if (dialog.open) {
        renderDirection();
        refreshControls();
      }
    }
  }
  function maybeVerifyMcp() {
    const node = selectedDirectionNode();
    if (!node || node.state !== 'online' || nodeDirection(node) !== 'dial'
        || node.mcp_verify_state) return;
    verifyMcp(node.member);
  }
  async function switchDirection(target) {
    if (busy || loading || directionBusy || !allowed()) return;
    const node = selectedDirectionNode();
    if (!node) {
      report(directionStatus, '请先选择节点。', true);
      return;
    }
    if (node.credential_revoked) {
      report(directionStatus, '凭据已吊销，请先重新签发。', true);
      return;
    }
    directionView = target;
    renderDirection();
    refreshControls();
    const current = nodeDirection(node);
    const online = node.state === 'online' && !node.credential_revoked;
    if (target === current && node.switch_state !== 'switching' && online) {
      report(directionStatus, `当前已经是${directionLabel(target)}。`);
      return;
    }
    if (target === 'listen' && !listenState?.remote_enabled) {
      report(directionStatus, '方向 A 需要本机远程监听已开启；请先在上方选择网卡并开启监听。', true);
      return;
    }
    if (target === 'dial' && !node.dial_endpoint) {
      report(directionStatus, '方向 B 需要先导入云端配对链接。', true);
      pairingUrl.focus();
      return;
    }
    directionBusy = true;
    directionWatch += 1;
    const token = directionWatch;
    refreshControls();
    report(directionStatus, `正在切换到${directionLabel(target)}：先断开旧连接，再建立新方向；期间暂停派发。`);
    report(directionFailure, '');
    try {
      const result = await api(`/relay/nodes/${encodeURIComponent(node.member)}/switch`, {
        method: 'POST',
        body: JSON.stringify({direction: target}),
        signal: AbortSignal.timeout(20000),
      });
      if (!result || result.direction !== target) throw new Error('切换响应格式异常');
      await reload(true);
      if (!dialog.open || !allowed()) return;
      watchSwitch(node.member, target, token, epoch);
    } catch (error) {
      report(directionStatus, `切换失败：${message(error)}。`, true);
      renderDirection();
    } finally {
      directionBusy = false;
      if (dialog.open) refreshControls();
    }
  }
  function retryDirection() {
    const node = selectedDirectionNode();
    if (!node) return;
    switchDirection(node.switch_target || nodeDirection(node));
  }
  function backDirection() {
    const node = selectedDirectionNode();
    if (!node) return;
    const view = directionView || nodeDirection(node);
    switchDirection(view === 'dial' ? 'listen' : 'dial');
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
  directionNode.addEventListener('change', () => {
    clearPairing();
    directionView = '';
    renderDirection();
    refreshControls();
  });
  directionListen.addEventListener('click', () => switchDirection('listen'));
  directionDial.addEventListener('click', () => switchDirection('dial'));
  directionTest.addEventListener('click', () => {
    const node = selectedDirectionNode();
    if (!node) return;
    if (node.state === 'online' && nodeDirection(node) === 'dial') {
      report(directionStatus, '当前节点已在线；测试连接由已建立的活动通道完成。');
      return;
    }
    switchDirection('dial');
  });
  directionVerify.addEventListener('click', () => {
    const node = selectedDirectionNode();
    if (node) verifyMcp(node.member, {force: true});
  });
  directionRetry.addEventListener('click', retryDirection);
  directionBack.addEventListener('click', backDirection);
  pairingParse.addEventListener('click', parsePairing);
  pairingConfirm.addEventListener('click', confirmPairing);
  pairingCancel.addEventListener('click', () => {
    if (pairingBusy) return;
    clearPairing();
    pairingUrl.value = '';
    refreshControls();
  });
  pairingUrl.addEventListener('input', () => {
    if (pendingPairing) {
      pendingPairing = null;
      pairingPreview.hidden = true;
      report(pairingStatus, '');
    }
  });
  refresh.addEventListener('click', async () => {
    if (busy || loading || !allowed()) return;
    clearIssued();
    report(registerStatus, '');
    await reload();
    if (dialog.open && allowed()) maybeVerifyMcp();
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
    directionBusy = false;
    directionView = '';
    directionWatch++;
    pendingPairing = null;
    pairingBusy = false;
    pairingUrl.value = '';
    pairingPreview.hidden = true;
    report(nodesStatus, '');
    report(registerStatus, '');
    report(registerGate, '');
    report(listenStatus, '');
    report(directionStatus, '');
    report(directionFailure, '');
    report(pairingStatus, '');
    renderListen();
    renderDirection();
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
