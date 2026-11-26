/* Remote Hermes relay node administration with member-level source bindings. */
(() => {
  const entry = document.querySelector('#btn-relay-nodes');
  const anchor = document.querySelector('#btn-settings');
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;

  const dialog = document.createElement('dialog');
  dialog.id = 'relay-nodes-dialog';
  dialog.setAttribute('aria-labelledby', 'relay-nodes-title');
  dialog.innerHTML = `
    <h3 id="relay-nodes-title" data-i18n="relay_title">远程节点管理</h3>
    <p id="relay-readonly" class="profile-help" role="note" data-i18n="relay_readonly" hidden>当前为 Bot 身份，仅可查看说明；远程节点管理需要管理员登录。</p>
    <div id="relay-admin-body">
      <div class="relay-toolbar">
        <button type="button" id="relay-add" class="primary" data-i18n="relay_add">添加远程 Hermes</button>
        <button type="button" id="relay-open-listen" data-i18n="relay_listen_config">本机监听配置</button>
        <button type="button" id="relay-refresh" data-i18n="refresh">刷新</button>
        <span id="relay-nodes-status" class="profile-help" role="status" aria-live="polite"></span>
      </div>
      <dialog id="relay-listen-dialog" aria-labelledby="relay-listen-title">
      <section id="relay-listen" class="relay-listen" aria-labelledby="relay-listen-title">
        <div class="relay-listen-head">
          <h4 id="relay-listen-title" data-i18n="relay_listen_config">本机监听配置</h4>
          <span id="relay-listen-badge" class="relay-badge offline" data-i18n="relay_reading">读取中</span>
        </div>
        <p id="relay-listen-summary" class="profile-help" role="status" aria-live="polite"></p>
        <div class="relay-listen-controls">
          <label class="relay-field" for="relay-listen-address"><span data-i18n="relay_interface">本机网卡地址</span>
            <select id="relay-listen-address"></select>
          </label>
          <button type="button" id="relay-listen-save" class="primary" data-i18n="relay_save_listen" disabled>保存并开启监听</button>
          <button type="button" id="relay-listen-disable" data-i18n="relay_disable_listen" disabled>停用远程监听</button>
        </div>
        <div id="relay-listen-url-row" class="relay-copy-row" hidden>
          <input id="relay-listen-url" readonly autocomplete="off" spellcheck="false"
                 aria-label="本机连接地址" data-i18n-aria-label="relay_local_address">
          <button type="button" id="relay-listen-copy" data-i18n="relay_copy_address">复制地址</button>
        </div>
        <p id="relay-listen-sources" class="profile-help"></p>
        <p id="relay-listen-status" class="profile-help" role="status" aria-live="polite"></p>
      </section>
      <div class="dlg-btns"><button id="relay-listen-refresh" data-i18n="refresh">刷新</button><button id="relay-listen-close" data-i18n="close">关闭</button></div></dialog>
      <div id="relay-node-list" aria-live="polite"></div>
      <section id="relay-credential" class="relay-credential" hidden aria-labelledby="relay-credential-title">
        <h4 id="relay-credential-title" data-i18n="relay_save_credential">请立即保存凭据</h4>
        <p class="profile-help" role="alert" data-i18n="relay_credential_once">凭据只显示这一次；关闭窗口、刷新页面或重新打开后无法再次查看。</p>
        <label for="relay-issued-member" data-i18n="relay_member_id">成员标识</label>
        <div class="relay-copy-row">
          <input id="relay-issued-member" readonly autocomplete="off" spellcheck="false" aria-label="成员标识" data-i18n-aria-label="relay_member_id">
          <button type="button" id="relay-copy-member" data-i18n="copy">复制</button>
        </div>
        <label for="relay-issued-credential" data-i18n="relay_credential">连接凭据</label>
        <div class="relay-copy-row">
          <input id="relay-issued-credential" readonly autocomplete="off" spellcheck="false" aria-label="连接凭据" data-i18n-aria-label="relay_credential">
          <button type="button" id="relay-copy-credential" data-i18n="copy">复制</button>
        </div>
      </section>
    </div>
    <div class="dlg-btns"><button type="button" id="relay-close" data-i18n="close">关闭</button></div>`;
  document.body.append(dialog);

  const directionPanel = createRelayDirectionDialog(dialog, 'relay-direction', {
    title: 'relay_switch_direction',
    badge: '<span id="relay-direction-badge" class="relay-badge offline"></span>',
    content: `<p id="relay-direction-status" class="profile-help" role="status" aria-live="polite"></p>
        <p id="relay-direction-failure" class="profile-help" role="alert"></p>
        <details id="relay-direction-detail">
          <summary data-i18n="relay_connection_details">连接详情</summary>
          <div class="relay-direction-addresses">
            <span><span class="relay-key" data-i18n="relay_local_listen">本机监听地址</span><span id="relay-direction-local">—</span></span>
            <span><span class="relay-key" data-i18n="relay_peer_dial">对端拨号地址</span><span id="relay-direction-remote">—</span></span>
            <span><span class="relay-key" data-i18n="relay_fingerprint">证书指纹</span><span id="relay-direction-fingerprint">—</span></span>
          </div>
          <ol class="relay-stages" aria-label="连接阶段" data-i18n-aria-label="relay_stages_aria">
            <li id="relay-stage-listen"><span data-i18n="relay_listening">已监听</span><em data-i18n="relay_not_started">未开始</em></li>
            <li id="relay-stage-auth"><span data-i18n="relay_authenticated">双方已认证</span><em data-i18n="relay_not_started">未开始</em></li>
            <li id="relay-stage-mcp"><span data-i18n="relay_reading_members">读取成员名单</span><em data-i18n="relay_not_started">未开始</em></li>
          </ol>
          <div class="relay-direction-actions">
            <button type="button" id="relay-direction-verify" data-i18n="relay_reverify_mcp" hidden>重新验证 MCP</button>
            <button type="button" id="relay-pairing-replace" data-i18n="relay_replace_pairing">更换配对链接</button>
          </div>
        </details>
        <section id="relay-pairing" class="relay-pairing" aria-label="云端配对" data-i18n-aria-label="relay_cloud_pairing" hidden>
          <div class="relay-pairing-line">
            <label class="relay-field" for="relay-pairing-url"><span data-i18n="relay_cloud_pairing_url">云端 Hermes 配对链接</span>
              <input id="relay-pairing-url" type="password" autocomplete="off" spellcheck="false"
                     placeholder="粘贴配对链接" data-i18n-placeholder="relay_paste_pairing">
            </label>
            <button type="button" id="relay-pairing-parse" data-i18n="relay_parse">解析</button>
          </div>
          <div id="relay-pairing-preview" class="relay-pairing-preview" hidden>
            <p><span class="relay-key" data-i18n="relay_device">设备</span><span id="relay-pairing-identity"></span></p>
            <p><span class="relay-key" data-i18n="relay_entry">入口</span><span id="relay-pairing-address"></span></p>
            <p><span class="relay-key" data-i18n="relay_short_fingerprint">短指纹</span><span id="relay-pairing-fingerprint"></span></p>
            <div class="relay-direction-actions">
              <button type="button" id="relay-pairing-confirm" class="primary" data-i18n="relay_confirm_pairing">确认配对并切换</button>
              <button type="button" id="relay-pairing-cancel" data-i18n="cancel">取消</button>
            </div>
          </div>
          <p id="relay-pairing-status" class="profile-help" role="status" aria-live="polite"></p>
        </section>`,
    actions: `<button type="button" id="relay-direction-close" data-i18n="close">关闭</button>
          <button type="button" id="relay-direction-apply" class="primary" data-i18n="relay_apply_switch">确认切换</button>`,
  });

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
  const listenDialog = $('#relay-listen-dialog');
  let listenContinuation = null;
  let selectedProfile = '';
  const directionBadge = $('#relay-direction-badge');
  const directionListen = $('#relay-direction-listen');
  const directionDial = $('#relay-direction-dial');
  const directionDetail = $('#relay-direction-detail');
  const directionLocal = $('#relay-direction-local');
  const directionRemote = $('#relay-direction-remote');
  const directionFingerprint = $('#relay-direction-fingerprint');
  const directionStatus = $('#relay-direction-status');
  const directionFailure = $('#relay-direction-failure');
  const directionVerify = $('#relay-direction-verify');
  const directionApply = $('#relay-direction-apply');
  let replacePairing = false;
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
  const credentialPanel = $('#relay-credential');
  const issuedMember = $('#relay-issued-member');
  const issuedCredential = $('#relay-issued-credential');
  const close = $('#relay-close');

  let epoch = 0, busy = false, nodes = [], issued = null;
  let bindings = new Map();
  let bindingsError = '';
  let nodesReady = false, bindingsReady = false;
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
    if (error?.status === 403) return window.t('relay_forbidden');
    const text = String(error?.message || '').trim();
    return text && !/^(Internal Server Error|Service Unavailable|Bad Gateway|Gateway Timeout)$/i.test(text)
      ? text : window.t('relay_server_failed');
  }
  function fmtHeartbeat(value) {
    const ts = Number(value);
    if (!Number.isFinite(ts) || ts <= 0) return '—';
    return new Date(ts * 1000).toLocaleString();
  }
  function bindingFor(member) {
    return bindings.get(member)?.ips || [];
  }
  function remoteName(node) {
    if (node.host_profile && node.host_profile !== node.member) return node.display_name;
    return `${node.display_name || node.member}${window.t('relay_remote_suffix')}`;
  }
  function addLocalBinding(member, ip) {
    if (!bindingFor(member).includes(ip)) {
      bindings.set(member, {...bindings.get(member), ips: [...bindingFor(member), ip]});
    }
  }
  function registrationReady() {
    return nodesReady && bindingsReady && !loading && !writeUncertain;
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
    for (const {ips} of bindings.values()) {
      for (const ip of ips) seen.add(ip);
    }
    return [...seen].sort();
  }
  function directionLabel(direction) {
    return direction === 'dial' ? window.t('relay_direction_dial') : window.t('relay_direction_listen');
  }
  function selectedDirectionNode() {
    return nodes.find(node => node.member === selectedProfile) || null;
  }
  function nodeDirection(node) {
    return node?.direction === 'dial' ? 'dial' : 'listen';
  }
  function stageState(item, state, text) {
    item.className = `relay-stage-${state}`;
    item.querySelector('em').textContent = text;
  }
  function stageLabel(stage) {
    return {dns: window.t('relay_stage_dns'), tcp: window.t('relay_stage_tcp'), tls: window.t('relay_stage_tls'), auth: window.t('relay_stage_auth')}[stage] || window.t('relay_stage_conn');
  }
  function endpointText(endpoint) {
    if (!endpoint?.host || !endpoint?.port) return '';
    const host = String(endpoint.host).includes(':') ? `[${endpoint.host}]` : endpoint.host;
    return `${endpoint.tls === false ? 'ws' : 'wss'}://${host}:${endpoint.port}`;
  }
  function renderDirection() {
    const selected = selectedDirectionNode();
    if (!selected) {
      if (directionPanel.open) directionPanel.close();
      return;
    }
    $('#relay-direction-title').textContent = window.t('relay_switch_direction') + ' · ' + remoteName(selected);
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
    listenFlag.textContent = serverDirection === 'listen' ? window.t('relay_current_direction') : '';
    dialFlag.textContent = serverDirection === 'dial' ? window.t('relay_current_direction') : '';
    listenFlag.hidden = !listenFlag.textContent;
    dialFlag.hidden = !dialFlag.textContent;

    if (revoked) {
      directionBadge.className = 'relay-badge revoked';
      directionBadge.textContent = window.t('relay_credential_revoked');
    } else if (online) {
      directionBadge.className = 'relay-badge online';
      directionBadge.textContent = window.t('relay_connected');
    } else if (selected.switch_error) {
      directionBadge.className = 'relay-badge revoked';
      directionBadge.textContent = window.t('relay_switch_failed_badge');
    } else if (switching) {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = window.t('relay_switching');
    } else if (selected.dial_state === 'connecting' || selected.dial_state === 'backoff') {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = selected.dial_state === 'backoff' ? window.t('relay_dial_backoff') : window.t('relay_dialing');
    } else if (selected.dial_state === 'abandoned') {
      directionBadge.className = 'relay-badge revoked';
      directionBadge.textContent = window.t('relay_dial_stopped');
    } else if (selected.offline_reason === 'unexpected') {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = window.t('relay_dropped');
    } else {
      directionBadge.className = 'relay-badge offline';
      directionBadge.textContent = window.t('relay_offline');
    }

    directionLocal.textContent = local || window.t('relay_loopback_only');
    directionRemote.textContent = remote || window.t('relay_no_pairing');
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
      stageState(stageListen, 'current', window.t('relay_configured_waiting'));
    } else {
      stageState(stageListen, listening ? 'done' : 'pending', listening ? window.t('relay_done') : window.t('relay_pending'));
    }
    stageState(stageAuth, authenticated ? 'done' : listening ? 'current' : 'pending',
      authenticated ? window.t('relay_done') : listening ? window.t('relay_in_progress') : window.t('relay_pending'));
    if (mcp) {
      stageState(stageMcp, 'done', window.t('relay_done'));
    } else if (authenticated && mcpVerifying) {
      stageState(stageMcp, 'current', window.t('relay_verifying'));
    } else if (authenticated && mcpFailed) {
      stageState(stageMcp, 'failed', window.t('relay_verify_failed'));
    } else if (authenticated) {
      stageState(stageMcp, 'current', window.t('relay_to_verify'));
    } else {
      stageState(stageMcp, 'pending', window.t('relay_pending'));
    }

    let status = '', statusError = false, failure = '', failureError = false;
    if (revoked) {
      status = window.t('relay_revoked_hint');
      statusError = true;
    } else if (direction !== serverDirection && !switching && !selected.switch_error) {
      if (direction === 'dial' && !endpoint) status = window.t('relay_paste_pairing_hint');
    } else if (onlineForView && !switching) {
      if (mcpVerifying) {
        status = window.t('relay_connected_verifying', [directionLabel(direction)]);
      } else if (mcpFailed) {
        status = window.t('relay_mcp_verify_failed');
      }
    } else if (selected.switch_error) {
      status = window.t('relay_switch_failed', [selected.switch_error]);
      statusError = true;
    } else if (switching) {
      status = window.t('relay_switching_target', [directionLabel(selected.switch_target || direction)]);
    } else if (selected.offline_reason === 'switch') {
      status = window.t('relay_switch_lost', [directionLabel(direction)]);
    } else if (selected.offline_reason === 'unexpected') {
      status = window.t('relay_lost_retry');
      statusError = true;
    } else if (selected.dial_state === 'backoff' || selected.dial_state === 'connecting') {
      status = window.t('relay_dialing_hint', [remote || window.t('relay_cloud_listen')]);
    } else if (selected.dial_state === 'abandoned') {
      status = window.t('relay_dial_exhausted');
      statusError = true;
    } else {
      status = window.t('relay_current_waiting', [directionLabel(direction)]);
    }
    report(directionStatus, status, statusError);
    if (onlineForView && mcpVerifying) {
      failure = '';
    } else if (onlineForView && mcpFailed) {
      failure = window.t('relay_verify_auth_failed', [selected.mcp_verify_error || window.t('unknown_reason')]);
      failureError = true;
    } else if (!online && selected.dial_failure?.stage) {
      failure = window.t('relay_dial_stuck', [selected.dial_failure.address || remote, stageLabel(selected.dial_failure.stage), selected.dial_failure.error || window.t('unknown_error')]);
      failureError = true;
    } else if (!online && selected.switch_error) {
      failure = window.t('relay_failure_reason', [selected.switch_error]);
      failureError = true;
    } else if (!online && direction === 'listen' && !listenState?.remote_enabled) {
      failure = window.t('relay_open_listen_hint');
      failureError = true;
    }
    report(directionFailure, failure, failureError);
    const failed = (!online && (selected.switch_error || selected.dial_failure?.stage
      || selected.dial_state === 'abandoned' || selected.offline_reason === 'unexpected'))
      || (onlineForView && mcpFailed);
    if (failed && !pairingBusy && pairingStatus.dataset.error !== 'true') report(pairingStatus, '');

    directionVerify.hidden = !(direction === 'dial' && onlineForView && mcpFailed);
    directionVerify.disabled = busy || loading || directionBusy || revoked || mcpVerifying;
    directionVerify.textContent = mcpVerifying ? window.t('relay_mcp_verifying') : window.t('relay_reverify_mcp');
    pairingSection.hidden = direction !== 'dial' || (!!endpoint && !replacePairing);
    $('#relay-pairing-replace').hidden = direction !== 'dial' || !endpoint || replacePairing;
    directionApply.hidden = !pairingSection.hidden || (onlineForView && !switching);
    directionApply.textContent = direction === serverDirection && !switching ? window.t('relay_retry_connect') : window.t('relay_apply_switch');
    pairingParse.disabled = busy || loading || directionBusy || pairingBusy || !nodesReady;
    pairingConfirm.disabled = busy || loading || directionBusy || pairingBusy || !pendingPairing;
    pairingPreview.hidden = !pendingPairing;
    if (pendingPairing) {
      pairingIdentity.textContent = remoteName(selected);
      pairingAddress.textContent = `wss://${pendingPairing.displayHost}:${pendingPairing.port}`;
      pairingFingerprint.textContent = `${pendingPairing.fingerprint.slice(0, 8)}…`;
    }
  }
  function listenNetwork(item) {
    return [...new Set([item?.network, item?.name].filter(Boolean))].join(' · ');
  }
  function renderListenOptions() {
    const previous = listenAddress.value || listenState?.remote_address || '';
    listenAddress.replaceChildren();
    if (!interfacesReady) {
      listenAddress.append(new Option(window.t('relay_reading_interfaces'), ''));
      return;
    }
    const list = interfaces.filter(item => item && item.address);
    if (!list.length) {
      listenAddress.append(new Option(window.t('relay_no_interfaces'), ''));
      return;
    }
    listenAddress.append(new Option(window.t('relay_select_interface'), ''));
    const values = new Set();
    for (const item of list) {
      values.add(item.address);
      const label = [listenNetwork(item), item.address, item.family].filter(Boolean).join(' · ');
      listenAddress.append(new Option(label, item.address));
    }
    listenAddress.value = values.has(previous) ? previous : '';
  }
  function renderListen() {
    const enabled = !!(listenState?.remote_enabled && listenState.remote_address);
    listenBadge.className = `relay-badge ${enabled && !listenUncertain ? 'online' : 'offline'}`;
    listenBadge.textContent = listenUncertain ? window.t('relay_unconfirmed')
      : listenLoading && !listenState ? window.t('relay_reading') : enabled ? window.t('relay_enabled') : window.t('relay_local_only');
    let summary = '', summaryError = false;
    if (listenUncertain) {
      summary = window.t('relay_listen_unconfirmed');
      summaryError = true;
    } else if (listenError) {
      summary = window.t('relay_listen_read_failed', [listenError]);
      summaryError = true;
    } else if (listenLoading && !listenState) {
      summary = window.t('relay_reading_listen');
    } else if (!listenState) {
      summary = window.t('relay_listen_unknown');
      summaryError = true;
    } else if (enabled) {
      const network = listenNetwork(interfaces.find(item => item.address === listenState.remote_address));
      summary = window.t('relay_listen_actual', [[network, hostPort(listenState.remote_address, listenState.remote_port)].filter(Boolean).join(' · ')]);
    } else {
      summary = window.t('relay_listen_off_loopback', [listenState.loopback || '127.0.0.1']);
      if (listenState.error) {
        summary = window.t('relay_listen_off_error', [listenState.error]);
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
        ? window.t('relay_bindings_read_failed', [bindingsError])
        : window.t('relay_bindings_reading'), !!bindingsError);
    } else {
      const ips = allowedSourceIps();
      report(listenSources, ips.length
        ? window.t('relay_bindings_list', [ips.join(window.t('workspace_sep'))])
        : window.t('relay_bindings_hint'), false);
    }
    renderListenOptions();
  }
  function refreshControls() {
    const ready = registrationReady();
    const changingDirection = directionBusy || pairingBusy;
    refresh.disabled = busy || loading || changingDirection;
    close.disabled = busy || changingDirection;
    $('#relay-add').disabled = busy || !nodesReady || loading || changingDirection;
    $('#relay-open-listen').disabled = busy || loading || changingDirection;
    for (const button of list.querySelectorAll('button')) {
      button.disabled = busy || !ready || directionBusy || pairingBusy;
    }
    for (const input of list.querySelectorAll('input')) {
      input.disabled = busy || !ready;
    }
    const lready = listenReady();
    $('#relay-listen-close').disabled = busy;
    $('#relay-listen-refresh').disabled = busy || loading;
    listenAddress.disabled = busy || loading || !lready;
    listenSave.disabled = busy || loading || !lready || !listenAddress.value;
    listenDisable.disabled = busy || loading || !lready || !listenState?.remote_enabled;
    listenCopy.disabled = busy || !listenUrl.value;
    const dirNode = selectedDirectionNode();
    const dirLocked = busy || loading || directionBusy || pairingBusy || !dirNode || !!dirNode.credential_revoked;
    directionListen.disabled = dirLocked;
    directionDial.disabled = dirLocked;
    directionVerify.disabled = dirLocked || !!dirNode?.credential_revoked
      || mcpVerifyInflight.has(dirNode?.member);
    directionApply.disabled = dirLocked || (dirNode?.switch_state === 'switching'
      && dirNode.switch_target === (directionView || nodeDirection(dirNode)) && !dirNode.switch_error && !dirNode.dial_failure);
    $('#relay-direction-close').disabled = directionBusy || pairingBusy;
    $('#relay-pairing-replace').disabled = dirLocked;
    pairingUrl.disabled = dirLocked;
    pairingParse.disabled = dirLocked;
    pairingConfirm.disabled = dirLocked || !pendingPairing;
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
  function renderNodesUnavailable() {
    list.replaceChildren();
    const empty = document.createElement('p');
    empty.className = 'profile-help relay-empty';
    empty.textContent = window.t('relay_nodes_unavailable');
    list.append(empty);
  }
  function sourceSection(node) {
    const section = document.createElement('div');
    section.className = 'relay-node-source';
    const head = document.createElement('div');
    head.className = 'relay-source-head';
    const title = document.createElement('span');
    title.className = 'relay-source-title';
    title.textContent = window.t('relay_bindings_title');
    const add = document.createElement('button');
    add.type = 'button';
    add.dataset.action = 'add-ip';
    add.textContent = window.t('relay_add_ip');
    head.append(title, add);

    const ips = bindingFor(node.member);
    const ipList = document.createElement('div');
    ipList.className = 'relay-ip-list';
    if (!bindingsReady) {
      const note = document.createElement('span');
      note.className = 'profile-help';
      note.textContent = bindingsError ? window.t('relay_ip_read_failed', [bindingsError]) : window.t('relay_reading_ips');
      ipList.append(note);
    } else if (!ips.length) {
      const note = document.createElement('span');
      note.className = 'profile-help relay-source-empty';
      note.textContent = window.t('relay_no_ips');
      ipList.append(note);
    } else {
      for (const ip of ips) {
        const chip = document.createElement('span');
        chip.className = 'relay-ip-chip';
        const text = document.createElement('span');
        const network = bindings.get(node.member)?.networks?.[ip];
        text.textContent = [network, ip].filter(Boolean).join(' · ');
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.dataset.action = 'remove-ip';
        remove.dataset.ip = ip;
        remove.setAttribute('aria-label', window.t('relay_remove_ip_aria', [ip]));
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
    input.placeholder = window.t('relay_source_ip');
    input.setAttribute('aria-label', window.t('relay_add_ip_aria', [node.member]));
    const save = document.createElement('button');
    save.type = 'submit';
    save.textContent = window.t('save');
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.dataset.action = 'cancel-ip';
    cancel.textContent = window.t('cancel');
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
      if (!nodesReady) return;
      const empty = document.createElement('p');
      empty.className = 'profile-help relay-empty';
      empty.textContent = window.t('relay_empty');
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
      name.textContent = remoteName(node);
      const badge = document.createElement('span');
      const revoked = !!node.credential_revoked;
      const online = node.state === 'online' && !revoked;
      badge.className = `relay-badge ${revoked ? 'revoked' : online ? 'online' : 'offline'}`;
      badge.textContent = revoked ? window.t('relay_offline_revoked') : online ? window.t('relay_online') : window.t('relay_offline');
      head.append(name, badge);

      const meta = document.createElement('div');
      meta.className = 'relay-node-meta';
      const fields = [
        [window.t('relay_member'), node.member],
        [window.t('relay_direction'), directionLabel(nodeDirection(node))],
        [window.t('relay_generation'), node.generation ?? '—'],
        [window.t('relay_last_heartbeat'), fmtHeartbeat(node.last_heartbeat)],
        [window.t('relay_capacity'), node.capacity_used ?? 0],
        [window.t('relay_mcp_fingerprint'), node.mcp_fingerprint || window.t('relay_not_reported')],
        [window.t('relay_peer_ip'), node.peer_ip || window.t('relay_not_connected')],
      ];
      if (node.code_version) fields.push([window.t('relay_version'), node.code_version]);
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
        revoke.textContent = window.t('relay_revoke');
        actions.append(revoke);
      }
      const reissue = document.createElement('button');
      reissue.type = 'button';
      reissue.dataset.action = 'reissue';
      reissue.textContent = window.t('relay_reissue');
      actions.append(reissue);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.dataset.action = 'delete-node';
      remove.className = 'relay-danger';
      remove.textContent = window.t('relay_delete');
      actions.append(remove);
      const directionButton = document.createElement('button');
      directionButton.type = 'button';
      directionButton.dataset.action = 'direction';
      directionButton.textContent = window.t('relay_switch_direction_btn');
      directionButton.setAttribute('aria-haspopup', 'dialog');
      actions.prepend(directionButton);
      const workspace = document.createElement('form');
      workspace.className = 'relay-copy-row';
      const rootLabel = document.createElement('label');
      rootLabel.textContent = window.t('relay_root_dir');
      const rootInput = document.createElement('input');
      rootInput.value = node.workspace_root || '';
      rootInput.placeholder = window.t('relay_root_placeholder');
      rootInput.required = true;
      rootLabel.append(rootInput);
      const saveRoot = document.createElement('button');
      saveRoot.textContent = window.t('relay_verify_save');
      const result = document.createElement('p');
      result.className = 'profile-help';
      result.setAttribute('role', 'status');
      workspace.append(rootLabel, saveRoot);
      workspace.onsubmit = async event => {
        event.preventDefault();
        if (!allowed() || saveRoot.disabled) return;
        saveRoot.disabled = true;
        rootInput.disabled = true;
        report(result, window.t('relay_verifying_root'));
        try {
          const saved = await api('/relay/nodes/' + encodeURIComponent(node.member) + '/workspace-root', {
            method: 'PUT', body: JSON.stringify({root: rootInput.value.trim()}),
          });
          rootInput.value = node.workspace_root = saved.root;
          report(result, window.t('relay_root_saved'));
        } catch (error) { report(result, message(error), true); }
        finally { saveRoot.disabled = false; rootInput.disabled = false; }
      };
      card.append(head, meta, workspace, result, sourceSection(node), actions);
      list.append(card);
    }
    renderDirection();
    refreshControls();
  }
  function renderReadonly() {
    closeChildren();
    epoch++;
    clearIssued();
    nodes = [];
    selectedProfile = '';
    bindings = new Map();
    bindingsError = '';
    nodesReady = false;
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
    readonly.hidden = false;
    body.hidden = true;
  }
  function renderAdmin() {
    readonly.hidden = true;
    body.hidden = false;
    renderNodes();
  }
  async function loadNodes(generation, quiet = false) {
    refresh.disabled = true;
    if (!quiet) report(nodesStatus, window.t('relay_loading_nodes'));
    try {
      const data = await api('/relay/nodes', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (generation !== epoch || !dialog.open) return false;
      if (!Array.isArray(data)) throw new Error(window.t('relay_bad_nodes_response'));
      nodes = data;
      nodesReady = true;
      renderNodes();
      if (!quiet) report(nodesStatus, nodes.length
        ? window.t('relay_nodes_loaded', [nodes.length, new Date().toLocaleTimeString()])
        : window.t('relay_no_nodes'));
      return true;
    } catch (error) {
      if (generation !== epoch || !dialog.open) return false;
      nodes = [];
      nodesReady = false;
      renderNodesUnavailable();
      report(nodesStatus, window.t('relay_nodes_load_failed', [message(error)]), true);
      return false;
    } finally {
      if (generation === epoch) refreshControls();
    }
  }
  async function loadBindings(generation) {
    try {
      const data = await api('/admin/source-ips', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (generation !== epoch || !dialog.open) return false;
      if (!Array.isArray(data.bindings)) throw new Error(window.t('relay_bad_bindings_response'));
      bindings = new Map();
      for (const item of data.bindings) {
        if (item && item.member && Array.isArray(item.ips)) {
          bindings.set(item.member, item);
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
      if (!Array.isArray(ifaceData?.interfaces)) throw new Error(window.t('relay_bad_interfaces_response'));
      if (!statusData || typeof statusData !== 'object' || !('remote_enabled' in statusData)) {
        throw new Error(window.t('relay_bad_listen_response'));
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
      report(listenStatus, window.t('relay_listen_config_failed', [listenError]), true);
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
    bindingsReady = false;
    bindingsError = '';
    listenLoading = true;
    interfacesReady = false;
    statusReady = false;
    listenError = '';
    refreshControls();
    const results = await Promise.all([
      loadNodes(generation, quiet),
      loadBindings(generation),
      loadListen(generation),
    ]);
    if (generation !== epoch || !dialog.open) return;
    [nodesReady, bindingsReady] = results;
    loading = false;
    listenLoading = false;
    if (nodesReady && bindingsReady) writeUncertain = false;
    if (nodesReady) renderNodes();
    else renderNodesUnavailable();
    renderListen();
    renderDirection();
    refreshControls();
  }
  async function open() {
    if (dialog.open) return;
    dialog.showModal();
    clearIssued();
    nodes = [];
    selectedProfile = '';
    bindings = new Map();
    bindingsError = '';
    nodesReady = false;
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
      $('#relay-add').focus();
    }
  }
  async function revokeOrDeleteNode(member, deleting) {
    if (busy || !allowed() || !registrationReady() || !nodes.some(node => node.member === member)) return;
    if (!await showConfirm(window.t(deleting ? 'relay_delete_confirm' : 'relay_revoke_confirm', [member]))) return;
    if (busy || !allowed() || !dialog.open || !registrationReady()) return;
    busy = true;
    refreshControls();
    report(nodesStatus, window.t(deleting ? 'relay_deleting' : 'relay_revoking', [member]));
    try {
      const path = `/relay/nodes/${encodeURIComponent(member)}`;
      if (deleting) await api(path, {method: 'DELETE'});
      else await api(path + '/revoke', {method: 'POST'});
      writeUncertain = false;
      if (issued?.member === member) clearIssued();
      if (dialog.open) report(nodesStatus, window.t(deleting ? 'relay_deleted' : 'relay_revoked', [member]));
      await reload(true);
    } catch (error) {
      if (dialog.open) {
        if (!error.status) writeUncertain = true;
        const prefix = deleting ? 'relay_delete' : 'relay_revoke';
        report(nodesStatus, window.t('relay_revoke_result', [window.t(prefix + (error.status ? '_failed' : '_unconfirmed')), message(error), error.status ? '' : window.t('relay_verify_before_retry')]), true);
      }
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function reissueNode(member) {
    if (busy || !allowed() || !registrationReady() || !nodes.some(node => node.member === member)) return;
    const node = nodes.find(item => item.member === member);
    if (nodeDirection(node) === 'listen') {
      firstJoin.renew(node, remoteName(node));
      return;
    }
    if (!await showConfirm(window.t('relay_reissue_confirm', [member]))) return;
    if (busy || !allowed() || !dialog.open || !registrationReady()) return;
    busy = true;
    refreshControls();
    report(nodesStatus, window.t('relay_reissuing', [member]));
    try {
      const data = await api('/relay/nodes', {
        method: 'POST',
        body: JSON.stringify({member, display_name: node.display_name || member, rotate: true}),
      });
      if (!dialog.open || !allowed()) return;
      if (data.member !== member || !data.credential) throw new Error(window.t('relay_reissue_missing'));
      writeUncertain = false;
      showIssued(member, data.credential);
      report(nodesStatus, window.t('relay_reissued', [member]));
      await reload(true);
    } catch (error) {
      if (dialog.open && allowed()) {
        const lost = !error.status;
        if (lost) writeUncertain = true;
        report(nodesStatus, lost
          ? window.t('relay_reissue_unconfirmed', [message(error)])
          : window.t('relay_reissue_failed', [message(error)]), true);
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
      report(status, window.t('relay_ip_required'), true);
      input.focus();
      return;
    }
    if (!isIp(ip)) {
      report(status, window.t('relay_ip_invalid'), true);
      input.focus();
      return;
    }
    if (isLoopback(ip)) {
      report(status, window.t('relay_ip_loopback'), true);
      input.focus();
      return;
    }
    const existing = bindingFor(member);
    if (existing.includes(ip)) {
      report(status, window.t('relay_ip_duplicate'), true);
      return;
    }
    busy = true;
    refreshControls();
    report(status, window.t('relay_ip_saving'));
    try {
      const data = await api('/admin/source-ips/add', {
        method: 'POST',
        body: JSON.stringify({member, ip}),
      });
      if (data.member !== member || !data.ip) throw new Error(window.t('relay_bad_bindings_response'));
      addLocalBinding(member, data.ip);
      writeUncertain = false;
      report(nodesStatus, window.t('relay_ip_added', [member, data.ip]));
      await reload(true);
    } catch (error) {
      if (!error.status) writeUncertain = true;
      report(status, window.t('relay_ip_add_failed', [message(error)]), true);
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function removeIp(member, ip) {
    if (busy || !allowed() || !registrationReady()) return;
    busy = true;
    refreshControls();
    report(nodesStatus, window.t('relay_ip_removing', [member, ip]));
    try {
      await api(`/admin/source-ips/${encodeURIComponent(member)}/${encodeURIComponent(ip)}`, {method: 'DELETE'});
      bindings.set(member, {...bindings.get(member), ips: bindingFor(member).filter(item => item !== ip)});
      writeUncertain = false;
      report(nodesStatus, window.t('relay_ip_removed', [member, ip]));
      await reload(true);
    } catch (error) {
      if (!error.status) writeUncertain = true;
      report(nodesStatus, window.t('relay_ip_remove_failed', [message(error)]), true);
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function applyListen(address) {
    if (busy || !allowed() || !listenReady()) return;
    busy = true;
    refreshControls();
    report(listenStatus, address ? window.t('relay_switching_listen', [address]) : window.t('relay_disabling_listen'));
    try {
      const data = await api('/admin/listen', {
        method: 'POST',
        body: JSON.stringify({address}),
        signal: AbortSignal.timeout(30000),
      });
      if (!dialog.open || !allowed()) return;
      if (!data || typeof data !== 'object' || !('remote_enabled' in data)) {
        throw new Error(window.t('relay_bad_listen_response'));
      }
      listenState = data;
      listenUncertain = false;
      listenError = '';
      if (address) {
        if (data.remote_enabled) {
          report(listenStatus, window.t('relay_listen_enabled', [hostPort(data.remote_address, data.remote_port)]));
        } else {
          report(listenStatus, window.t('relay_listen_bind_failed', [data.error || window.t('relay_bind_failed_hint')]), true);
        }
      } else if (data.remote_enabled) {
        report(listenStatus, window.t('relay_disable_incomplete'), true);
      } else {
        report(listenStatus, window.t('relay_listen_disabled'));
      }
      renderListen();
      await reload(true);
      if (address && listenState?.remote_enabled && listenState.remote_address === address && listenContinuation) {
        const resume = listenContinuation;
        listenContinuation = null;
        listenDialog.close();
        busy = false;
        await resume();
      }
    } catch (error) {
      if (!error.status) listenUncertain = true;
      report(listenStatus, !error.status
        ? window.t('relay_listen_switch_unconfirmed', [message(error)])
        : window.t('relay_listen_switch_failed', [message(error)]), true);
      renderListen();
    } finally {
      busy = false;
      if (dialog.open) refreshControls();
    }
  }
  function saveListen() {
    if (busy || !allowed() || !listenReady()) return;
    if (!listenAddress.value) {
      report(listenStatus, window.t('relay_select_interface_hint'), true);
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
    if (!raw) throw new Error(window.t('relay_pairing_required'));
    let url;
    try {
      url = new URL(raw);
    } catch {
      throw new Error(window.t('relay_pairing_bad_format'));
    }
    if (url.protocol !== 'wss:') throw new Error(window.t('relay_pairing_wss'));
    if (url.username || url.password) throw new Error(window.t('relay_pairing_no_auth'));
    let host = url.hostname || '';
    if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
    if (!host) throw new Error(window.t('relay_pairing_no_host'));
    if (host === '0.0.0.0' || host === '::') throw new Error(window.t('relay_pairing_wildcard'));
    const port = Number(url.port || 443);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(window.t('relay_pairing_bad_port'));
    const params = url.searchParams;
    const token = params.get('pairing_token') || '';
    if (!token) throw new Error(window.t('relay_pairing_no_token'));
    const fingerprint = (params.get('fingerprint') || '').toLowerCase().replace(/:/g, '');
    if (!/^[0-9a-f]{64}$/.test(fingerprint)) throw new Error(window.t('relay_pairing_bad_fingerprint'));
    const member = params.get('member') || '';
    const selected = selectedDirectionNode();
    if (member && selected && member !== (selected.host_profile || selected.member)) {
      throw new Error(window.t('relay_pairing_member_mismatch', [member, selected.display_name || selected.member]));
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
      if (!selected) throw new Error(window.t('relay_select_node_first'));
      pairingPreview.hidden = false;
      pairingIdentity.textContent = remoteName(selected);
      pairingAddress.textContent = `wss://${pendingPairing.displayHost}:${pendingPairing.port}`;
      pairingFingerprint.textContent = `${pendingPairing.fingerprint.slice(0, 8)}…`;
      report(pairingStatus, window.t('relay_pairing_valid'));
      refreshControls();
    } catch (error) {
      pendingPairing = null;
      pairingPreview.hidden = true;
      report(pairingStatus, window.t('relay_pairing_invalid', [error.message]), true);
      refreshControls();
    }
  }
  async function confirmPairing() {
    if (busy || loading || directionBusy || pairingBusy || !pendingPairing) return;
    const selected = selectedDirectionNode();
    if (!selected) {
      report(pairingStatus, window.t('relay_select_node_first'), true);
      return;
    }
    pairingBusy = true;
    refreshControls();
    report(pairingStatus, window.t('relay_pairing_probing'));
    const member = selected.member;
    try {
      const probe = await api(`/relay/nodes/${encodeURIComponent(member)}/dial-endpoint/probe`, {
        method: 'POST',
        body: JSON.stringify({host: pendingPairing.host, port: pendingPairing.port,
                              fingerprint: pendingPairing.fingerprint}),
        signal: AbortSignal.timeout(15000),
      });
      if (!probe || probe.ok !== true) {
        report(pairingStatus, window.t('relay_probe_failed', [stageLabel(probe?.stage), probe?.error || window.t('relay_probe_unreachable')]), true);
        return;
      }
      const saved = await api(`/relay/nodes/${encodeURIComponent(member)}/dial-endpoint`, {
        method: 'POST',
        body: JSON.stringify({host: pendingPairing.host, port: pendingPairing.port, tls: true,
                              fingerprint: pendingPairing.fingerprint,
                              pairing_token: pendingPairing.token}),
        signal: AbortSignal.timeout(15000),
      });
      if (!saved?.saved) throw new Error(window.t('relay_bad_dial_response'));
      report(pairingStatus, window.t('relay_probe_ok'));
      pendingPairing = null;
      pairingPreview.hidden = true;
      pairingUrl.value = '';
      replacePairing = false;
      await reload(true);
      if (directionPanel.open && allowed()) await switchDirection('dial');
    } catch (error) {
      report(pairingStatus, window.t('relay_import_failed', [message(error), pendingPairing ? window.t('relay_retry_hint') : '']), true);
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
            report(directionStatus, window.t('relay_switch_done', [directionLabel(target)]));
            if (target === 'dial') {
              report(pairingStatus, window.t('relay_import_done'));
            }
            renderDirection();
            maybeVerifyMcp();
          } else {
            report(directionStatus, window.t('relay_switch_incomplete', [node.switch_error || window.t('relay_new_direction_unauth')]), true);
            if (target === 'dial') {
              report(pairingStatus, window.t('relay_saved_not_connected', [node.switch_error || window.t('relay_new_direction_unauth')]), true);
            }
            renderDirection();
          }
          return;
        }
      }
      if (token === directionWatch && generation === epoch) {
        report(directionStatus, window.t('relay_switching_refresh'));
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
      report(directionStatus, window.t('relay_mcp_reading_roster'));
      const result = await api(`/relay/nodes/${encodeURIComponent(member)}/mcp-verify`, {
        method: 'POST',
        signal: AbortSignal.timeout(30000),
      });
      if (!result || !['ok', 'failed'].includes(result.mcp_verify_state)) {
        throw new Error(window.t('relay_bad_mcp_response'));
      }
      await reload(true);
      if (!dialog.open || !allowed()) return;
      const fresh = nodes.find(item => item.member === member);
      if (fresh?.mcp_verify_state === 'failed') {
        report(directionFailure, window.t('relay_mcp_verify_failed_detail', [fresh.mcp_verify_error || window.t('unknown_reason')]), true);
      }
    } catch (error) {
      report(directionStatus, window.t('relay_mcp_verify_failed_detail', [message(error)]), true);
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
      report(directionStatus, window.t('relay_select_node_first'), true);
      return;
    }
    if (node.credential_revoked) {
      report(directionStatus, window.t('relay_reissue_first'), true);
      return;
    }
    if (target === 'listen' && !listenState?.remote_enabled) {
      openListen(() => {
        if (selectedProfile === node.member) switchDirection(target);
      });
      return;
    }
    directionView = target;
    renderDirection();
    refreshControls();
    const current = nodeDirection(node);
    const online = node.state === 'online' && !node.credential_revoked;
    if (target === current && node.switch_state !== 'switching' && online) {
      report(directionStatus, window.t('relay_already_direction', [directionLabel(target)]));
      return;
    }
    if (target === 'dial' && !node.dial_endpoint) {
      report(directionStatus, window.t('relay_paste_pairing_hint'), true);
      pairingUrl.focus();
      return;
    }
    directionBusy = true;
    directionWatch += 1;
    const token = directionWatch;
    refreshControls();
    report(directionStatus, window.t('relay_switching_target2', [directionLabel(target)]));
    report(directionFailure, '');
    try {
      const result = await api(`/relay/nodes/${encodeURIComponent(node.member)}/switch`, {
        method: 'POST',
        body: JSON.stringify({direction: target}),
        signal: AbortSignal.timeout(45000),
      });
      if (!result || result.direction !== target) throw new Error(window.t('relay_bad_switch_response'));
      await reload(true);
      if (!dialog.open || !allowed()) return;
      watchSwitch(node.member, target, token, epoch);
    } catch (error) {
      renderDirection();
      report(directionStatus, window.t('relay_switch_failed', [message(error)]), true);
    } finally {
      directionBusy = false;
      if (dialog.open) refreshControls();
    }
  }
  async function copy(value, label, target = nodesStatus) {
    try {
      await navigator.clipboard.writeText(value);
      report(target, window.t('relay_copied', [label]));
    } catch {
      try {
        const area = document.createElement('textarea');
        area.value = value;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        (target.closest('dialog') || document.body).append(area);
        area.select();
        const ok = document.execCommand('copy');
        area.remove();
        if (!ok) throw new Error('copy failed');
        report(target, window.t('relay_copied', [label]));
      } catch {
        report(target, window.t('relay_copy_failed', [label]), true);
      }
    }
  }

  function openListen(resume = null) {
    if (!allowed()) return;
    listenContinuation = resume;
    listenDialog.showModal();
    if (!listenReady()) loadListen(epoch);
  }
  function closeChildren() {
    listenContinuation = null;
    if (listenDialog.open) listenDialog.close();
    if (directionPanel.open) directionPanel.close();
    firstJoin.dismiss();
  }
  const firstJoin = createRelayJoinDialog(dialog, {
    allowed,
    requireListen(start) {
      if (listenState?.remote_enabled && !listenUncertain) start();
      else openListen(start);
    },
    async registered(member, renewed) {
      await reload(true);
      const node = nodes.find(item => item.member === member);
      if (dialog.open && allowed() && renewed) {
        report(nodesStatus, window.t('relay_credential_effective', [node ? remoteName(node) : member]));
      } else if (dialog.open && allowed() && node) report(nodesStatus, window.t('relay_added', [remoteName(node)]));
    },
    copy,
  });
  $('#relay-add').addEventListener('click', firstJoin.open);
  $('#relay-open-listen').addEventListener('click', () => openListen());
  $('#relay-listen-refresh').addEventListener('click', () => { if (!busy && !loading) reload(true); });
  $('#relay-listen-close').addEventListener('click', () => { if (!busy) listenDialog.close(); });
  listenDialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  listenDialog.addEventListener('close', () => { listenContinuation = null; });
  entry.addEventListener('click', open);
  close.addEventListener('click', () => dialog.close());
  listenSave.addEventListener('click', saveListen);
  listenDisable.addEventListener('click', disableListen);
  listenCopy.addEventListener('click', () => {
    if (listenUrl.value) copy(listenUrl.value, window.t('relay_local_address'), listenStatus);
  });
  listenAddress.addEventListener('change', refreshControls);
  function chooseDirection(target) {
    directionView = target;
    replacePairing = false;
    clearPairing();
    pairingUrl.value = '';
    renderDirection();
  }
  directionListen.addEventListener('click', () => chooseDirection('listen'));
  directionDial.addEventListener('click', () => chooseDirection('dial'));
  directionApply.addEventListener('click', () => switchDirection(directionView || nodeDirection(selectedDirectionNode())));
  $('#relay-direction-close').addEventListener('click', () => directionPanel.close());
  directionPanel.addEventListener('cancel', event => { if (directionBusy || pairingBusy) event.preventDefault(); });
  directionPanel.addEventListener('close', () => {
    const member = selectedProfile;
    selectedProfile = '';
    directionWatch++;
    clearPairing();
    pairingUrl.value = '';
    if (dialog.open && member) {
      list.querySelector(`[data-relay-member="${CSS.escape(member)}"] [data-action="direction"]`)?.focus({preventScroll: true});
    }
  });
  $('#relay-pairing-replace').addEventListener('click', () => {
    replacePairing = true;
    renderDirection();
    pairingUrl.focus();
  });
  directionVerify.addEventListener('click', () => {
    const node = selectedDirectionNode();
    if (node) verifyMcp(node.member, {force: true});
  });
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
    await reload();
    if (dialog.open && allowed()) maybeVerifyMcp();
  });
  list.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const card = button.closest('[data-relay-member]');
    if (!card) return;
    if (button.dataset.action === 'direction') {
      clearPairing();
      pairingUrl.value = '';
      directionView = '';
      selectedProfile = card.dataset.relayMember;
      replacePairing = false;
      directionDetail.open = false;
      directionWatch++;
      renderDirection();
      refreshControls();
      directionPanel.showModal();
      maybeVerifyMcp();
      return;
    }
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
    if (button.dataset.action === 'revoke' || button.dataset.action === 'delete-node') {
      revokeOrDeleteNode(card.dataset.relayMember, button.dataset.action === 'delete-node');
    }
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
    if (issued) copy(issued.member, window.t('relay_member_id'));
  });
  $('#relay-copy-credential').addEventListener('click', () => {
    if (issued) copy(issued.credential, window.t('relay_credential'));
  });
  dialog.addEventListener('cancel', event => {
    if (event.target === dialog && (busy || directionBusy || pairingBusy)) event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    closeChildren();
    epoch++;
    clearIssued();
    list.replaceChildren();
    nodes = [];
    selectedProfile = '';
    bindings = new Map();
    bindingsError = '';
    nodesReady = false;
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
