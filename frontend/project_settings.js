// Project-scoped membership and workspaces; profile editing remains global.
let profileProjectReturn = null;
const projectMembersCache = new Map(), projectMembersPending = new Map(), projectMembersVersions = new Map();
let workspaceEditor = null;
let projectPermissionsEditor = null, projectPermissionsVersion = 0;
window.refreshProjectSettingsFromAssistant = (project) => {
  if(!$('#dlg-project-detail').open||$('#pj-detail-form').dataset.origName!==project)return;
  const tab=$('#pj-detail-tabs [aria-selected="true"]')?.dataset.tab;
  if(tab==='roles'&&!$('#pj-role-list').querySelector('.pj-role-rename'))loadProjectRoles();
  if(tab==='members')loadProjectMembers();
  if(tab==='permissions'&&projectPermissionsEditor){
    const dirty=[...$('#pj-permissions-list').querySelectorAll('[data-permission]')].some(row=>
      [...row.querySelectorAll('input')].some(input=>input.checked!==(projectPermissionsEditor.grants[row.dataset.permission]||[]).includes(input.value)));
    if(dirty)$('#pj-permissions-status').textContent=window.t('pj_permissions_hud_updated');
    else loadProjectPermissions(true);
  }
};
function resetProjectPermissions() {
  projectPermissionsVersion++;
  projectPermissionsEditor = null;
  $('#pj-permissions-list').replaceChildren();
  $('#pj-permissions-status').textContent = '';
  $('#pj-permissions-save').disabled = true;
  $('#pj-permissions-reload').disabled = false;
}
async function loadProjectPermissions(force = false) {
  const project = $('#pj-detail-form').dataset.origName;
  if (!project || (!force && projectPermissionsEditor?.project === project)) return;
  const version = ++projectPermissionsVersion;
  $('#pj-permissions-save').disabled = true;
  $('#pj-permissions-status').textContent = window.t('pj_permissions_loading');
  try {
    const data = await api(`/projects/${encodeURIComponent(project)}/permissions`);
    if (version !== projectPermissionsVersion || $('#pj-detail-form').dataset.origName !== project) return;
    projectPermissionsEditor = data;
    renderProjectPermissions(data);
    $('#pj-permissions-save').disabled = false;
    $('#pj-permissions-status').textContent = window.t('pj_permissions_hint');
  } catch (err) {
    if (version === projectPermissionsVersion) $('#pj-permissions-status').textContent = window.t('read_failed') + ': ' + err.message;
  }
}
function renderProjectPermissions(data) {
  const box = $('#pj-permissions-list');
  box.replaceChildren();
  // req#96 #166：授权对象=bot ∪ 角色（角色来自 view 的 roles 数组， grants 以 role:<id> 区分）
  const roles = data.roles || [];
  for (const permission of data.permissions) {
    const row = document.createElement('fieldset');
    row.className = 'project-permission-row';
    row.dataset.permission = permission.key;
    const legend = document.createElement('legend');
    legend.textContent = permission.label;
    row.appendChild(legend);
    const options = document.createElement('div');
    options.className = 'project-permission-options';
    const picker = document.createElement('details'), summary = document.createElement('summary');
    picker.className = 'project-permission-picker';
    picker.append(summary, options);
    const selected = data.grants[permission.key] || [];
    const members = data.members.filter(member => member.active === 1);
    for (const member of members) {
      const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      input.type = 'checkbox'; input.value = member.profile; input.checked = selected.includes(member.profile);
      text.textContent = `${memberLabel(member)}${member.missing ? ' · ' + window.t('deleted') : !member.active ? ' · ' + window.t('pj_disabled_mark') : ''}`;
      label.append(input, text); options.appendChild(label);
    }
    for (const role of roles) {
      const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      input.type = 'checkbox'; input.value = `role:${role.id}`; input.checked = selected.includes(`role:${role.id}`);
      input.addEventListener('change', () => {
        if (!input.checked && selected.includes(input.value)) showAlert(window.t('pj_revoke_perm_warning', [role.name, permission.label]));
      });
      text.textContent = window.t('pj_role_members', [role.name, role.member_count]);
      label.append(input, text); options.appendChild(label);
    }
    const updateSummary = () => {
      const chosen = [...options.querySelectorAll('input:checked')].map(input => {
        const role = roles.find(r => `role:${r.id}` === input.value);
        if (role) return window.t('pj_role_label', [role.name]);
        const member = members.find(m => m.profile === input.value);
        return member ? memberDisplayName(member) : input.value;
      });
      summary.textContent = chosen.length ? `${chosen.slice(0, 2).join(window.t('workspace_sep'))}${chosen.length > 2 ? ' ' + window.t('pj_etc', [chosen.length]) : ''}` : window.t('pj_permission_pick_hint');
    };
    options.addEventListener('change', updateSummary);
    updateSummary();
    if (!members.length) options.textContent = window.t('pj_no_members');
    row.appendChild(picker); box.appendChild(row);
  }
}
$('#pj-permissions-reload').addEventListener('click', async () => {
  if (projectPermissionsEditor && !await showConfirm(window.t('pj_discard_permissions'))) return;
  loadProjectPermissions(true);
});
$('#pj-permissions-form').addEventListener('submit', async e => {
  e.preventDefault();
  const editor = projectPermissionsEditor;
  if (!editor || $('#pj-permissions-save').disabled) return;
  const version = projectPermissionsVersion;
  const grants = {};
  $('#pj-permissions-list').querySelectorAll('[data-permission]').forEach(row => {
    grants[row.dataset.permission] = [...row.querySelectorAll('input:checked')].map(input => input.value);
  });
  $('#pj-permissions-save').disabled = true;
  $('#pj-permissions-reload').disabled = true;
  $('#pj-permissions-list').querySelectorAll('input').forEach(input => input.disabled = true);
  $('#pj-permissions-status').textContent = window.t('saving');
  try {
    const result = await api(`/projects/${encodeURIComponent(editor.project)}/permissions`, {
      method: 'PUT', body: JSON.stringify({grants, revision: editor.revision})});
    if (version !== projectPermissionsVersion) return;
    projectPermissionsEditor = result;
    renderProjectPermissions(result);
    $('#pj-permissions-status').textContent = window.t('pj_permissions_saved');
  } catch (err) {
    if (version === projectPermissionsVersion) $('#pj-permissions-status').textContent = window.t('save_failed') + ': ' + err.message + window.t('open_paren') + window.t('pj_selection_kept') + window.t('close_paren');
  } finally {
    if (version === projectPermissionsVersion) {
      $('#pj-permissions-save').disabled = false;
      $('#pj-permissions-reload').disabled = false;
      $('#pj-permissions-list').querySelectorAll('input').forEach(input => input.disabled = false);
    }
  }
});
document.querySelectorAll('[name="project-directory-mode"]').forEach(radio => radio.addEventListener('change', () => {
  if (radio.checked) { $('#pj-d-mode').value = radio.value; $('#pj-worktree-options').hidden = radio.value !== 'worktree'; }
}));
$('#pj-members-refresh').addEventListener('click', () => loadProjectMembers());
// Shared profile creation keeps enrollment in the caller's member selection flow.
let memberAdd = null;
function memberAddReset(options) {
  memberAdd = {host: 'hermes', sources: [], scanning: false, error: '', routes: {},
    busy: false, project: options.project || '', template: options.template,
    onCreated: options.onCreated,
    draft: {profile: '', display_name: '', model: '', provider: '', instructions: '', source: ''},
    requestId: [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2, '0')).join('')};
}
function memberProfileError(value) {
  if (!value) return window.t('pj_profile_required');
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(value)) return window.t('pj_profile_format');
  if (['default', 'user', 'profiles'].includes(value) || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/.test(value)) return window.t('pj_profile_reserved');
  return '';
}
function memberCreateValid() {
  return !memberProfileError(memberAdd.draft.profile);
}
function renderMemberCreate(steps) {
  const dsh = memberAdd.host === 'dsh';
  const form = document.createElement('div');
  form.className = 'member-create-fields';
  form.innerHTML = `<label for="member-create-name" data-i18n="pj_display_name">显示名称</label><input id="member-create-name" maxlength="100" placeholder="例如：小夏" data-i18n-placeholder="pj_display_name_placeholder">
    <label for="member-create-profile"><span data-i18n="pj_profile_id">Profile 标识</span> <span class="profile-help" data-i18n="required">必填</span></label>
    <input id="member-create-profile" required maxlength="64" placeholder="例如：dev-01" data-i18n-placeholder="pj_profile_placeholder" autocomplete="off" spellcheck="false" aria-describedby="member-create-profile-help">
    <p class="profile-help" id="member-create-profile-help" data-i18n="pj_profile_help">使用 1–64 位小写英文字母、数字、短横线或下划线，以小写英文字母或数字开头。系统会自动准备独立目录。</p>
    <div id="member-create-source-block">
      <label for="member-create-source" data-i18n="pj_config_source">配置来源</label><select id="member-create-source"><option value="" data-i18n="pj_use_defaults">使用默认设置</option></select>
    </div>
    <p class="profile-help" id="member-create-auth-help"></p>
    <details><summary data-i18n="pj_more_settings">更多设置</summary>
    <div class="member-create-route${memberAdd.host === 'codex' ? ' single' : ''}">
      <div><label for="member-create-model" data-i18n="model">模型</label>
        <input id="member-create-model" maxlength="200" placeholder="留空沿用来源或运行时默认模型" data-i18n-placeholder="pj_model_placeholder"></div>
      <div id="member-create-provider-block"><label for="member-create-provider" data-i18n="provider">供应商</label>
        <input id="member-create-provider" maxlength="200" placeholder="留空沿用来源或运行时默认供应商" data-i18n-placeholder="pj_provider_placeholder"></div>
    </div>
    <p class="profile-help" id="member-create-route-help" data-i18n="pj_route_help">模型和供应商均可选；留空沿用配置来源或宿主默认值，填写后覆盖对应项。</p>
      <div class="profile-soul-heading">
        <label for="member-create-instructions">${window.t('pj_personal_instructions')} · ${dsh ? window.t('persona') : memberAdd.host === 'codex' ? 'AGENTS.md' : 'SOUL.md'}</label>
        <div id="member-create-soul-templates"></div>
      </div>
      <textarea id="member-create-instructions" rows="4" maxlength="32000" placeholder="填写性格、专长或工作偏好；项目职责在加入后设置。" data-i18n-placeholder="pj_instructions_placeholder"></textarea>
    </details>`;
  const draft = memberAdd.draft;
  form.querySelector('#member-create-profile-help').textContent = dsh
    ? window.t('pj_profile_help_dsh')
    : window.t('pj_profile_help');
  const profileInput = form.querySelector('#member-create-profile');
  profileInput.dialogValidation = memberProfileError;
  const paired = memberAdd.host !== 'codex';
  form.querySelector('#member-create-provider-block').hidden = !paired;
  form.querySelector('#member-create-route-help').hidden = !paired;
  const details = form.querySelector('details');
  details.open = !!draft.expanded;
  details.addEventListener('toggle', () => { draft.expanded = details.open; });
  for (const [id, key] of [['name', 'display_name'], ['profile', 'profile'], ['model', 'model'], ['provider', 'provider'], ['instructions', 'instructions']]) {
    const input = form.querySelector('#member-create-' + id);
    input.value = draft[key];
    input.addEventListener('input', () => {
      draft[key] = input.value;
      if (key === 'profile') draft.profileTouched = true;
      $('#member-add-join').disabled = !memberCreateValid() || memberAdd.busy || memberAdd.scanning;
    });
  }
  const source = form.querySelector('#member-create-source');
  memberAdd.sources.forEach((p, i) => source.add(new Option(
    `${p.display_name || p.profile}${window.t('open_paren')}${p.profile}${window.t('close_paren')}`, String(i))));
  source.value = draft.source;
  const updateAuth = () => {
    if (memberAdd.host === 'dsh') {
      form.querySelector('#member-create-auth-help').textContent = window.t('pj_auth_help_dsh');
      return;
    }
    if (memberAdd.host === 'codex') {
      form.querySelector('#member-create-auth-help').textContent = window.t('pj_auth_help_default');
      return;
    }
    form.querySelector('#member-create-auth-help').textContent = window.t('pj_auth_help_inherit');
  };
  source.addEventListener('change', () => {
    draft.source = source.value;
    updateAuth();
  });
  updateAuth();
  steps.appendChild(form);
  if (draft.profileTouched) validateDialogField(profileInput);
  {
    const current = memberAdd;
    mountProjectSoulTemplates(form.querySelector('#member-create-soul-templates'), {
      project: current.project, template: current.template, maxLength: 32000, disabled: current.scanning,
      isActive: () => memberAdd === current && !current.busy,
      getValue: () => draft.instructions,
      setValue: value => {
        draft.instructions = value; draft.expanded = true; details.open = true;
        form.querySelector('#member-create-instructions').value = value;
      },
    });
  }
}
function renderMemberAdd() {
  const steps = $('#member-add-steps'), status = $('#member-add-status');
  status.dataset.error = String(!!memberAdd.error);
  const join = $('#member-add-join');
  join.disabled = memberAdd.busy || memberAdd.scanning || !memberCreateValid();
  join.textContent = window.t('pj_create_profile');
  steps.replaceChildren();
  $('#member-add-title').textContent = window.t('pj_create_profile');
  if (memberAdd.scanning) status.textContent = window.t('pj_scanning_sources');
  else if (memberAdd.error) status.textContent = memberAdd.error;
  else status.textContent = '';
  const host = document.createElement('div');
  host.className = 'project-mode-options';
  host.setAttribute('role', 'radiogroup'); host.setAttribute('aria-label', window.t('pj_host_type'));
  for (const [value, label] of [['hermes', 'Hermes'], ['codex', 'Codex'], ['dsh', 'DSH']]) {
    const choice = document.createElement('label'), input = document.createElement('input');
    input.type = 'radio'; input.name = 'member-add-host'; input.value = value;
    input.checked = memberAdd.host === value;
    input.addEventListener('change', () => {
      const draft = memberAdd.draft;
      memberAdd.routes[memberAdd.host] = {model: draft.model, provider: draft.provider};
      Object.assign(draft, memberAdd.routes[value] || {model: '', provider: ''});
      memberAdd.host = value;
      loadMemberCreateSources();
    });
    const title = document.createElement('span'); title.textContent = label;
    choice.append(input, title); host.append(choice);
  }
  steps.append(host);
  renderMemberCreate(steps);
}
function openMemberAdd() {
  MemberPicker.openProject($('#pj-detail-form').dataset.origName);
}
function openProfileCreate(options) {
  if ($('#dlg-member-add').open) return;
  memberAddReset(options);
  $('#dlg-member-add').showModal();
  loadMemberCreateSources();
}
$('#pj-members-add').addEventListener('click', openMemberAdd);
$('#member-add-close').addEventListener('click', () => { if (!memberAdd?.busy) $('#dlg-member-add').close(); });
$('#dlg-member-add').addEventListener('cancel', e => { if (memberAdd?.busy) e.preventDefault(); });
$('#dlg-member-add').addEventListener('close', () => { if (!$('#dlg-member-add').open) memberAdd = null; });
async function loadMemberCreateSources() {
  const current = memberAdd, scanVersion = current.scanVersion = (current.scanVersion || 0) + 1;
  current.scanning = true; current.sources = []; current.draft.source = ''; current.error = '';
  renderMemberAdd();
  try {
    const result = await api(`/discovery/scan?project=${encodeURIComponent(current.project)}`);
    if (memberAdd !== current || current.scanVersion !== scanVersion) return;
    current.sources = (result.candidates || []).filter(p => p.host_type === current.host);
    const defaultSource = current.sources.findIndex(p => p.profile === (current.host === 'dsh' ? 'desktop' : 'default'));
    current.draft.source = defaultSource >= 0 ? String(defaultSource) : current.sources.length ? '0' : '';
  } catch (err) {
    if (memberAdd === current && current.scanVersion === scanVersion) {
      current.error = window.t('pj_sources_failed', [err.message]);
    }
  } finally {
    if (memberAdd === current && current.scanVersion === scanVersion) {
      current.scanning = false; renderMemberAdd();
    }
  }
}

$('#member-add-join').addEventListener('click', submitMemberCreate);

async function submitMemberCreate() {
  const current = memberAdd;
  if (!current || current.busy || current.scanning || !memberCreateValid()) return;
  current.busy = true;
  const dialog = $('#dlg-member-add');
  dialog.querySelectorAll('input, select, textarea, button').forEach(el => el.disabled = true);
  $('#member-add-status').textContent = window.t('pj_creating_profile');
  $('#member-add-status').dataset.error = 'false';
  const draft = current.draft, source = current.sources[Number(draft.source)];
  const payload = {host_type: current.host, profile: draft.profile,
    display_name: draft.display_name.trim(), model: draft.model.trim(), instructions: draft.instructions,
    source: draft.source !== '' && source ? {profile: source.profile, config_root: source.config_root} : null,
    request_id: current.requestId};
  if (current.host !== 'codex') payload.provider = draft.provider.trim();
  try {
    const result = await api('/profiles/create', {method: 'POST', body: JSON.stringify(payload)});
    if (memberAdd !== current) return;
    current.busy = false;
    dialog.close();
    await current.onCreated({...result, display_name: draft.display_name.trim(), bound: true});
    if (result.setup_command) {
      await showAlert(window.t('pj_setup_notice', [result.notice, result.setup_command, result.home]));
    }
  } catch (err) {
    if (memberAdd !== current) return;
    current.error = window.t('pj_create_failed', [err.message]);
    current.busy = false;
    renderMemberAdd();
  } finally {
    $('#member-add-close').disabled = false;
  }
}

async function loadDshBindingAvailability(project, data) {
  if (!data.members.some(m => m.runtime === 'dsh' && m.bot_id != null)) return null;
  try {
    // 候选扫描按 candidate_available 过滤：仍可用的已注册 dsh Bot 会出现在结果中。
    const result = await api(`/profiles/candidates?project=${encodeURIComponent(project)}`);
    return new Set((result.candidates || []).map(c => c.bot_id).filter(id => id != null));
  } catch {
    // 可用性查询失败时保留未知状态，不误报绑定失效。
    return null;
  }
}
function markDshBindings(data, available) {
  if (!available) return;
  for (const member of data.members) {
    if (member.runtime !== 'dsh' || member.bot_id == null) continue;
    member.binding_invalid = !available.has(member.bot_id);
  }
}

const remoteSessionRequests = new Map();
function remoteSessionLabel(member) {
  if (member.remote_session_pending) return window.t('pj_session_querying');
  if (member.work_session_exists == null) return window.t('pj_session_unknown');
  return member.work_session_exists ? window.t('pj_session_exists') : window.t('pj_session_none');
}

function workspaceStatus(state, remote) {
  if (!state.valid) return state.error || window.t('pj_dir_unavailable');
  if (!state.head) return window.t('pj_dir_available');
  const head = (state.detached ? 'detached' : state.branch) + ' · ' + state.head.slice(0, 12);
  return (remote ? window.t('pj_last_remote_check') : '') + head + (state.dirty ? ' · ' + window.t('pj_uncommitted') : ' · ' + window.t('pj_clean'));
}

async function refreshRemoteSessions(project, members) {
  const pending = members.filter(member => member.remote_session_pending);
  const current = member => projectMembersCache.get(project)?.members.includes(member);
  async function worker() {
    while (pending.length) {
      const member = pending.shift();
      if (!current(member) || $('#pj-detail-form').dataset.origName !== project) continue;
      const key = JSON.stringify([project, member.profile, member.bot_id]);
      let request = remoteSessionRequests.get(key);
      if (!request) {
        request = api(`/projects/${encodeURIComponent(project)}/members/${encodeURIComponent(member.profile)}/remote-work-session`)
          .catch(() => null)
          .finally(() => remoteSessionRequests.delete(key));
        remoteSessionRequests.set(key, request);
      }
      const result = await request;
      if (!current(member)) continue;
      member.remote_session_pending = false;
      member.work_session_exists = result?.work_session_exists ?? null;
      if ($('#pj-detail-form').dataset.origName !== project) continue;
      const row = $('#pj-member-list').querySelector(`[data-profile="${CSS.escape(member.profile)}"]`);
      const label = row?.querySelector('.remote-session-status');
      if (!label) continue;
      label.textContent = remoteSessionLabel(member);
      if (member.remote_workspace && result?.workspace && !row.querySelector('.workspace').disabled) {
        member.workspace = result.workspace;
        const path = row.querySelector('.project-member-path');
        path.textContent = path.title = member.workspace.path || window.t('pj_no_remote_path');
        const status = row.querySelector('.project-member-workspace .project-member-state');
        status.textContent = workspaceStatus(member.workspace, true);
        status.classList.toggle('valid', member.workspace.valid);
        status.classList.toggle('invalid', !member.workspace.valid);
      }
      const button = row.querySelector('.reset-session');
      if (button && !resettingMemberSessions.has(JSON.stringify([project, member.profile]))) {
        const reset = sessionResetButton(member);
        Object.assign(button, {textContent: reset.label, title: reset.title, disabled: reset.disabled});
      }
    }
  }
  await Promise.all([worker(), worker()]);
}

async function loadProjectMembers(options = {}) {
  const project = $('#pj-detail-form').dataset.origName;
  if (!project) return;
  const cached = projectMembersCache.get(project);
  if (cached) renderProjectMembers(project, cached);
  else $('#pj-member-list').textContent = window.t('pj_loading_members');
  $('#pj-members-status').textContent = cached ? window.t('pj_updating_status') : window.t('pj_checking_dirs');
  if (!options.profile && projectMembersPending.has(project)) return projectMembersPending.get(project);
  const version = (projectMembersVersions.get(project) || 0) + 1;
  projectMembersVersions.set(project, version);
  const request = (async () => {
    try {
      const suffix = options.profile && cached ? `?profile=${encodeURIComponent(options.profile)}` : '';
      const data = await api(`/projects/${encodeURIComponent(project)}/workspaces${suffix}${suffix ? '&' : '?'}defer_remote_sessions=true`);
      if (projectMembersVersions.get(project) !== version) return;
      const previous = projectMembersCache.get(project);
      const merged = suffix && previous ? {...data, members: previous.members.map(m => data.members.find(n => n.profile === m.profile) || m)} : data;
      const dshAvailable = await loadDshBindingAvailability(project, merged);
      if (projectMembersVersions.get(project) !== version) return;
      markDshBindings(merged, dshAvailable);
      projectMembersCache.set(project, merged);
      if ($('#pj-detail-form').dataset.origName !== project) return;
      renderProjectMembers(project, merged);
      $('#pj-members-status').textContent = merged.members.some(m => m.remote_session_pending) ? window.t('pj_members_loaded') : window.t('pj_status_updated');
      void refreshRemoteSessions(project, merged.members);
    } catch (err) {
      if ($('#pj-detail-form').dataset.origName === project && projectMembersVersions.get(project) === version) {
        $('#pj-members-status').textContent = window.t('refresh_failed') + ': ' + err.message;
        if (!projectMembersCache.has(project)) $('#pj-member-list').textContent = window.t('pj_members_unavailable');
      }
    } finally {
      if (projectMembersPending.get(project) === request) projectMembersPending.delete(project);
    }
  })();
  projectMembersPending.set(project, request);
  return request;
}

function sessionResetButton(member) {
  if (member.work_session_exists === true)
    return {disabled:false, label:window.t('pj_reset_session'), title:window.t('pj_reset_session_title')};
  if (member.work_session_exists === false)
    return {disabled:true, label:window.t('pj_no_session'), title:window.t('pj_no_session_title')};
  return {disabled:true, label:window.t('pj_session_pending_label'), title:window.t('pj_session_pending_title')};
}

function bindMemberNameEditor(row, member) {
  const button = row.querySelector('.member-name-edit');
  button.addEventListener('click', () => {
    if (row.querySelector('.member-name-input')) return;
    const input = document.createElement('input');
    input.className = 'member-name-input';
    input.type = 'text'; input.maxLength = 200;
    input.value = member.display_name || member.host_profile || member.profile;
    input.setAttribute('aria-label', window.t('pj_display_name_shared'));
    const status = document.createElement('p');
    status.className = 'project-member-state'; status.setAttribute('role', 'status');
    status.textContent = window.t('pj_name_edit_hint');
    row.appendChild(status);
    button.hidden = true; button.after(input);
    let saving = false, finished = false;
    const finish = () => {
      finished = true; input.remove(); status.remove(); button.hidden = false;
    };
    const save = async () => {
      if (saving || finished) return;
      const name = input.value.trim();
      if (!name || /[\r\n\0]/.test(name)) {
        status.textContent = window.t('pj_name_required');
        status.dataset.error = 'true';
        input.setAttribute('aria-invalid', 'true'); return;
      }
      if (name === member.display_name) { finish(); return; }
      saving = true; input.disabled = true;
      status.textContent = window.t('pj_saving_name');
      status.dataset.error = 'false';
      try {
        const result = await api(`/members/${encodeURIComponent(member.profile)}/display-name`,
          {method: 'PATCH', body: JSON.stringify({display_name: name})});
        const update = m => {
          if ((result.bot_id != null && m.bot_id === result.bot_id) || m.profile === member.profile)
            m.display_name = result.display_name;
        };
        update(member);
        for (const data of projectMembersCache.values()) data.members.forEach(update);
        S.members.forEach(update);
        button.textContent = result.display_name;
        finish();
        renderMemberOptions();
        _lastRenderSignature = null;
        renderMessages();
      } catch (err) {
        status.textContent = window.t('pj_name_save_failed', [err.message]);
        status.dataset.error = 'true';
        input.disabled = false;
      } finally { saving = false; }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); save(); }
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation();
        if (!saving) { finish(); button.focus(); }
      }
    });
    input.focus(); input.select();
  });
}

function renderProjectMembers(project, data) {
  const box = $('#pj-member-list'), scroll = box.scrollTop;
  box.querySelectorAll('.project-member-menu:popover-open').forEach(menu => menu.hidePopover());
  box.replaceChildren();
  for (const member of data.members) {
    const reset = sessionResetButton(member);
    const state = member.workspace;
    const independent = member.directory_mode === 'worktree';
    const path = state.path || (member.remote_workspace ? window.t('pj_no_remote_path') : `${data.project.worktree_root || ''}/${member.profile}`);
    const row = document.createElement('div');
    row.className = 'project-member-card';
    row.dataset.profile = member.profile;
    row.innerHTML = `<div class="project-member-header"><div class="project-member-identity">
      <div class="project-member-name"><button type="button" class="member-name-edit" title="${window.t('pj_name_edit_title')}" data-i18n-title="pj_name_edit_title">${esc(memberDisplayName(member))}</button><small>${esc(memberHostProfile(member))}</small>
      <button type="button" role="switch" aria-checked="${!!member.active}" aria-label="${window.t('pj_member_state')}" data-i18n-aria-label="pj_member_state" title="${member.active ? window.t('pj_disable_click') : window.t('pj_enable_click')}" class="project-member-badge ${member.active ? 'enabled' : ''}">${member.active ? window.t('pj_enabled') : window.t('pj_disabled')}</button></div></div>
      <div class="project-member-actions"><button type="button" class="workspace">${window.t('pj_workspace')}</button>
      <button type="button" class="member-more" aria-label="${window.t('pj_member_more')}" data-i18n-aria-label="pj_member_more" aria-haspopup="menu" aria-expanded="false">⋯</button>
      <div class="project-member-menu" popover="auto" role="menu">
      <button type="button" role="menuitem" class="edit" title="${window.t('pj_edit_profile_title')}" data-i18n-title="pj_edit_profile_title" data-i18n="pj_edit_profile">${window.t('pj_edit_profile')}</button>
      ${member.profile !== 'user' ? `<button type="button" role="menuitem" class="reset-session" title="${reset.title}" ${reset.disabled ? 'disabled' : ''}>${reset.label}</button><button type="button" role="menuitem" class="member-remove">${window.t('pj_remove_member')}</button>` : ''}
      </div></div></div>
      <div class="project-member-workspace ${state.valid && !state.head ? 'compact-status' : ''}"><span class="project-member-caption">${member.remote_workspace ? window.t('pj_remote_mark') : ''}${independent ? window.t('pj_worktree_independent') : window.t('pj_worktree_root')}</span>
      <div class="project-member-path" title="${esc(path)}">${esc(path)}</div>
      <p class="project-member-state ${state.valid ? 'valid' : 'invalid'}">${esc(workspaceStatus(state, member.remote_workspace))}</p></div>
      ${member.remote_session_pending !== undefined ? `<p class="project-member-state remote-session-status" role="status">${remoteSessionLabel(member)}</p>` : ''}
      ${member.binding_invalid ? '<p class="project-member-state invalid" data-binding-invalid="true">' + window.t('pj_binding_invalid', [member.binding_invalid]) + '</p>' : ''}`;
    bindMemberNameEditor(row, member);
    row.querySelector('.edit').addEventListener('click', () => {
      profileProjectReturn = project;
      closeDialogAnchored($('#dlg-project-detail'));
      openProfileEditor(member, project);
    });
    const workspaceButton = row.querySelector('.workspace');
    if (member.remote_workspace) {
      workspaceButton.textContent = window.t('pj_prepare_workspace');
      workspaceButton.onclick = async () => {
        workspaceButton.disabled = true;
        const status = row.querySelector('.project-member-workspace .project-member-state');
        status.textContent = window.t('pj_remote_checking');
        try {
          await api(`/projects/${encodeURIComponent(project)}/members/${encodeURIComponent(member.profile)}/prepare-workspace`, {method: 'POST'});
          await loadProjectMembers({profile: member.profile});
        } catch (error) { status.textContent = error.message; }
        finally { workspaceButton.disabled = false; }
      };
    } else workspaceButton?.addEventListener('click', () => openWorkspaceEditor(project, member));
    const resetStatus = document.createElement('p');
    resetStatus.className = 'project-member-state';
    resetStatus.setAttribute('role', 'status');
    resetStatus.hidden = true;
    row.appendChild(resetStatus);
    bindMemberMoreMenu(row);
    row.querySelector('.member-remove')?.addEventListener('click', () => openMemberRemoval(project, member));
    row.querySelector('.reset-session')?.addEventListener('click', e => resetMemberSession(project, member.profile, e.currentTarget, resetStatus));
    row.querySelector('.project-member-badge').addEventListener('click', async e => {
      const button = e.currentTarget, active = !member.active;
      button.disabled = true;
      try {
        await api(`/members/${encodeURIComponent(member.profile)}/active`, {method: 'POST', body: JSON.stringify({project, active})});
        member.active = active;
        const latest = projectMembersCache.get(project)?.members.find(m => m.profile === member.profile);
        if (latest) latest.active = active;
        button.textContent = active ? window.t('enable') : window.t('disable');
        button.classList.toggle('enabled', active);
        button.setAttribute('aria-checked', String(active));
        button.title = active ? window.t('pj_disable_click') : window.t('pj_enable_click');
        await loadMembers();
        if ($('#pj-detail-form').dataset.origName === project) await loadProjectMembers({profile: member.profile});
        await loadThreads();
      } catch (err) { resetStatus.hidden = false; resetStatus.textContent = err.message; }
      finally { button.disabled = false; }
    });
    // req#94 #160：卡片底部「本项目角色」输入框+保存按钮整段删除，改为头部角色按钮+下拉就地保存。
    // 头部 profile 名和项目角色分别展示，角色使用独立按钮。
    const roleCandidates = [...new Set(data.members.map(m => (m.role || '').trim()).filter(Boolean))];
    const headerRow = row.querySelector('.project-member-header');
    const roleBtn = document.createElement('button');
    roleBtn.type = 'button';
    roleBtn.className = 'project-member-role-btn';
    roleBtn.textContent = member.role ? member.role : window.t('pj_set_role');
    roleBtn.setAttribute('aria-haspopup', 'menu');
    roleBtn.setAttribute('aria-label', window.t('pj_role_aria', [member.display_name || member.profile, member.role ? window.t('colon') + member.role : window.t('pj_role_unset')]));
    // 角色按钮位于 .project-member-name 内，紧接启停胶囊。
    row.querySelector('.project-member-badge').after(roleBtn);
    roleBtn.addEventListener('click', e => {
      e.stopPropagation();
      document.querySelectorAll('.project-member-role-menu').forEach(menu => menu.remove());
      renderMemberRoleMenu(row, project, member, roleBtn, resetStatus);
    });
    row.insertBefore(resetStatus, headerRow.nextSibling);
    box.appendChild(row);
  }
  box.scrollTop = scroll;
}

const projectRolesCache = new Map();   // project -> roles 数组（req#96 #165 角色实体化）

// 成员卡片角色下拉：候选改从「角色 tab 定义的实体角色」取（req#96 五），不再从已分配 role 文本 DISTINCT。
function bindMemberMoreMenu(row) {
  const button = row.querySelector('.member-more'), menu = row.querySelector('.project-member-menu');
  let listeners;
  const close = () => { if (menu.matches(':popover-open')) menu.hidePopover(); };
  button.addEventListener('click', () => {
    if (menu.matches(':popover-open')) { close(); return; }
    menu.showPopover();
    const rect = button.getBoundingClientRect();
    const above = rect.top - 8, below = innerHeight - rect.bottom - 8;
    const upwards = menu.scrollHeight > below && above > below;
    menu.style.maxHeight = `${Math.max(0, upwards ? above : below)}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${upwards ? Math.max(8, rect.top - menu.offsetHeight - 4) : rect.bottom + 4}px`;
    button.setAttribute('aria-expanded', 'true');
    menu.querySelector('button:not(:disabled)')?.focus();
    listeners?.abort(); listeners = new AbortController();
    window.addEventListener('resize', close, {signal: listeners.signal});
    window.addEventListener('scroll', event => { if (!menu.contains(event.target)) close(); }, {capture: true, signal: listeners.signal});
  });
  menu.addEventListener('toggle', event => {
    if (event.newState === 'closed') { listeners?.abort(); button.setAttribute('aria-expanded', 'false'); }
  });
  menu.addEventListener('click', event => { if (event.target.closest('button')) close(); });
  menu.addEventListener('keydown', event => {
    const items = [...menu.querySelectorAll('button:not(:disabled)')];
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const index = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    } else if (event.key === 'Escape') { event.preventDefault(); close(); button.focus(); }
  });
}

function openMemberRemoval(project, member) {
  if (document.querySelector('#dlg-member-remove')) return;
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-member-remove'; dialog.setAttribute('aria-labelledby', 'member-remove-title');
  dialog.innerHTML = `<h3 id="member-remove-title">${window.t('pj_remove_member')}</h3><p class="member-remove-summary"></p><p class="profile-help">${window.t('pj_remove_member_hint')}</p><p class="profile-help">${window.t('pj_remove_member_role_hint')}</p><p class="member-remove-status" role="status"></p><div class="member-remove-blockers"></div><div class="dlg-btns"><button type="button" class="cancel">${window.t('cancel')}</button><button type="button" class="recheck">${window.t('pj_recheck')}</button><button type="button" class="confirm" disabled>${window.t('pj_remove_member')}</button></div>`;
  const projectName = S.projects.find(item => item.name === project)?.display_name || project;
  dialog.querySelector('.member-remove-summary').textContent = window.t('pj_remove_member_summary', [memberLabel(member), projectName]);
  const status = dialog.querySelector('.member-remove-status'), blockers = dialog.querySelector('.member-remove-blockers');
  const confirm = dialog.querySelector('.confirm'), cancel = dialog.querySelector('.cancel'), recheck = dialog.querySelector('.recheck');
  const endpoint = `/projects/${encodeURIComponent(project)}/members/${encodeURIComponent(member.profile)}`;
  let busy = false;
  function showBlockers(items) {
    blockers.replaceChildren();
    for (const item of items) {
      const line = document.createElement('div'), text = document.createElement('span');
      line.className = 'member-remove-blocker'; text.textContent = item.title; line.append(text);
      if (item.kind !== 'session') {
        const action = document.createElement('button'); action.type = 'button';
        action.textContent = item.kind === 'runtime' ? window.t('pj_stop_runtime') : item.kind === 'owner' ? window.t('pj_change_owner') : window.t('pj_view_handle');
        action.addEventListener('click', async () => {
          dialog.close();
          if (item.kind === 'owner') { selectProjectTab('basic'); $('#pj-d-owner')?.focus(); }
          else if (item.kind === 'runtime') await stopBot(member.profile, project);
          else {
            closeDialogAnchored($('#dlg-project-detail'));
            if (item.kind === 'task') await openTaskHistory(item.id, project);
            else if (item.kind === 'requirement') await openRequirementDetails(item.id, project);
            else if (item.kind === 'thread') { if (S.currentProject !== project) selectProject(project); await selectThread(item.id); }
          }
        });
        line.append(action);
      }
      blockers.append(line);
    }
  }
  async function check() {
    confirm.disabled = true; recheck.disabled = true; status.textContent = window.t('pj_checking_roles');
    try {
      const result = await api(endpoint + '/removal');
      if (!dialog.isConnected) return;
      showBlockers(result.blockers);
      confirm.disabled = !result.can_remove;
      status.textContent = result.can_remove ? window.t('pj_check_ok') : window.t('pj_check_items_first');
    } catch (error) { if (dialog.isConnected) status.textContent = window.t('check_failed') + ': ' + error.message; }
    finally { recheck.disabled = false; }
  }
  cancel.addEventListener('click', () => dialog.close()); recheck.addEventListener('click', check);
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove());
  confirm.addEventListener('click', async () => {
    busy = true; confirm.disabled = cancel.disabled = recheck.disabled = true;
    status.textContent = window.t('pj_removing_member');
    try {
      await api(endpoint, {method: 'DELETE'});
      projectMembersVersions.set(project, (projectMembersVersions.get(project) || 0) + 1);
      projectMembersCache.delete(project); projectMembersPending.delete(project); projectRolesCache.delete(project);
      dialog.close();
      if (S.currentProject === project) {
        S.members = S.members.map(item => item.profile === member.profile ? {...item, active: 0, role: '', role_id: null} : item);
        selectedMembers.delete(member.profile); renderMemberOptions(); renderChips();
      }
      const refreshed = await Promise.allSettled([loadMembers(), loadThreads()]);
      if ($('#pj-detail-form').dataset.origName === project) {
        await loadProjectMembers();
        const failed = !projectMembersCache.has(project) || refreshed.some(result => result.status === 'rejected');
        $('#pj-members-status').textContent = failed
          ? window.t('pj_removed_partial') : window.t('pj_removed');
      }
    } catch (error) {
      status.textContent = window.t('pj_remove_failed', [error.message]);
      if (Array.isArray(error.detail?.blockers)) showBlockers(error.detail.blockers);
    } finally { busy = false; cancel.disabled = recheck.disabled = false; }
  });
  document.body.append(dialog); dialog.showModal(); cancel.focus(); check();
}

function renderMemberRoleMenu(row, project, member, roleBtn, resetStatus) {
  const menu = document.createElement('div');
  menu.className = 'project-member-role-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('popover', 'auto');
  const positionMenu = () => {
    const anchor = roleBtn.getBoundingClientRect();
    const below = innerHeight - anchor.bottom - 8, above = anchor.top - 8;
    const upwards = menu.scrollHeight > below && above > below;
    menu.style.maxHeight = `${Math.max(0, upwards ? above : below)}px`;
    menu.style.left = `${Math.max(8, Math.min(anchor.left, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${upwards ? Math.max(8, anchor.top - menu.offsetHeight - 4) : anchor.bottom + 4}px`;
  };
  const closeMenu = event => {
    if (event?.type === 'scroll' && menu.contains(event.target)) return;
    window.removeEventListener('scroll', closeMenu, true);
    window.removeEventListener('resize', closeMenu);
    roleBtn.setAttribute('aria-expanded', 'false');
    menu.remove();
  };
  menu.addEventListener('toggle', event => { if (event.newState === 'closed') closeMenu(); });
  // 菜单骨架先挂上（异步填充候选），保证点击即有反馈
  const note = document.createElement('div');
  note.className = 'project-member-role-empty';
  note.textContent = window.t('pj_loading_roles');
  menu.appendChild(note);
  row.appendChild(menu);
  menu.showPopover();
  roleBtn.setAttribute('aria-expanded', 'true');
  positionMenu();
  window.addEventListener('scroll', closeMenu, true);
  window.addEventListener('resize', closeMenu);
  api(`/projects/${encodeURIComponent(project)}/roles`).then(data => {
    if (!menu.isConnected) return;
    const roles = data.roles || [];
    projectRolesCache.set(project, roles);
    menu.replaceChildren();
    if (!roles.length) {
      const empty = document.createElement('div');
      empty.className = 'project-member-role-empty';
      empty.textContent = window.t('pj_no_roles');
      menu.appendChild(empty);
      positionMenu();
      return;
    }
    roles.forEach(role => {
      const item = document.createElement('button');
      item.type = 'button';
      item.setAttribute('role', 'menuitem');
      item.textContent = role.name;
      if (member.role_id != null && role.id === member.role_id) item.classList.add('current');
      item.addEventListener('click', async () => {
        item.disabled = true;
        try {
          // PATCH role 接口仍是文本口径（req#96 五：role 字段原样保留）；用实体角色名写入
          const result = await api(`/projects/${encodeURIComponent(project)}/members/${encodeURIComponent(member.profile)}/role`,
            {method: 'PATCH', body: JSON.stringify({role: role.name})});
          member.role = result.role;
          member.role_id = result.role_id ?? null;
          roleBtn.textContent = result.role || window.t('pj_set_role');
          closeMenu();
          resetStatus.hidden = false; resetStatus.textContent = window.t('pj_role_saved');
          await loadMembers();
        } catch (err) { resetStatus.hidden = false; resetStatus.textContent = window.t('pj_role_save_failed', [err.message]); item.disabled = false; }
      });
      menu.appendChild(item);
    });
    positionMenu();
  }).catch(err => {
    if (!menu.isConnected) return;
    menu.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'project-member-role-empty';
    empty.textContent = window.t('pj_roles_load_failed', [err.message]);
    menu.appendChild(empty);
    positionMenu();
  });
}

/* ----- req#96 #165：角色 tab（项目详情弹窗）——角色实体 CRUD 前端 ----- */
function roleStatus(msg, isError = false) {
  const el = $('#pj-role-status');
  el.textContent = msg || '';
  el.style.color = isError ? 'var(--danger)' : '';
}
async function loadProjectRoles() {
  const project = $('#pj-detail-form').dataset.origName;
  if (!project) return;
  const box = $('#pj-role-list');
  box.textContent = window.t('pj_loading_roles');
  try {
    const data = await api(`/projects/${encodeURIComponent(project)}/roles`);
    if ($('#pj-detail-form').dataset.origName !== project) return;
    projectRolesCache.set(project, data.roles || []);
    renderProjectRoles(project, data.roles || []);
    roleStatus('');
  } catch (err) {
    if ($('#pj-detail-form').dataset.origName !== project) return;
    box.textContent = window.t('pj_roles_retry');
    roleStatus(err.message, true);
  }
}
function renderProjectRoles(project, roles) {
  const box = $('#pj-role-list');
  box.replaceChildren();
  if (!roles.length) {
    box.textContent = window.t('pj_no_roles_create');
    return;
  }
  for (const role of roles) {
    const row = document.createElement('div');
    row.className = 'pj-role-row';
    row.dataset.roleId = role.id;
    const nameBtn = document.createElement('button');
    nameBtn.type = 'button';
    nameBtn.className = 'project-member-role-btn';
    nameBtn.textContent = role.name;
    nameBtn.title = window.t('pj_rename_title', [role.name, role.id]);
    const count = document.createElement('span');
    count.className = 'profile-help';
    count.style.margin = '0';
    count.textContent = window.t('pj_member_count', [role.member_count]);
    const edit = document.createElement('button');
    edit.type = 'button'; edit.textContent = window.t('pj_rename');
    const del = document.createElement('button');
    del.type = 'button'; del.textContent = window.t('pj_delete');
    row.append(nameBtn, count, edit);
    if (!(role.is_owner ?? role.name === window.t('pj_owner_role'))) row.append(del);
    del.disabled = role.member_count > 0;
    if (role.member_count > 0) del.title = window.t('pj_role_in_use');
    box.appendChild(row);
    nameBtn.addEventListener('click', () => startRoleRename(project, role, row, nameBtn));
    edit.addEventListener('click', () => startRoleRename(project, role, row, nameBtn));  // req#96 #165：改名按钮此前漏绑监听
    del.addEventListener('click', async () => {
      if (!await showConfirm(window.t('pj_role_delete_confirm', [role.name]))) return;
      del.disabled = true;
      try {
        await api(`/projects/${encodeURIComponent(project)}/roles/${role.id}`, {method: 'DELETE'});
        await loadProjectRoles();
        roleStatus(window.t('pj_role_deleted'));
      } catch (err) { roleStatus(window.t('pj_role_delete_failed', [err.message]), true); del.disabled = false; }
    });
  }
}
async function startRoleRename(project, role, row, nameBtn) {
  if (row.querySelector('.pj-role-rename')) return;
  await showAlert(window.t('pj_role_renamed_hint'));
  if (!row.isConnected || row.querySelector('.pj-role-rename')) return;
  const input = document.createElement('input');
  input.className = 'pj-role-rename';
  input.type = 'text'; input.maxLength = 100;
  input.value = role.name;
  input.setAttribute('aria-label', window.t('pj_rename_aria', [role.name]));
  const finish = () => { input.remove(); nameBtn.hidden = false; };
  let saving = false;
  const save = async () => {
    if (saving) return;
    const name = input.value.trim();
    if (!name) { roleStatus(window.t('pj_role_name_required'), true); return; }
    if (name === role.name) { finish(); return; }
    saving = true; input.disabled = true;
    roleStatus(window.t('pj_saving_role_name'));
    try {
      await api(`/projects/${encodeURIComponent(project)}/roles/${role.id}`,
        {method: 'PATCH', body: JSON.stringify({name})});
      await loadProjectRoles();
      // 状态提示放在重载之后（loadProjectRoles 会先清状态）：改名结果稳定可见
      roleStatus(window.t('pj_role_renamed'));
    } catch (err) {
      roleStatus(window.t('pj_role_rename_failed', [err.message]), true);
      input.disabled = false; saving = false; return;
    } finally { saving = false; }
  };
  input.addEventListener('blur', save);
  input.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); save(); }
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(); nameBtn.focus(); }
  });
  nameBtn.hidden = true; nameBtn.after(input);
  input.focus(); input.select();
}
$('#pj-role-create').addEventListener('click', async () => {
  const project = $('#pj-detail-form').dataset.origName;
  if (!project) return;
  const input = $('#pj-role-new-name');
  const name = input.value.trim();
  if (!name) { roleStatus(window.t('pj_role_name_required'), true); return; }
  const btn = $('#pj-role-create');
  btn.disabled = true;
  try {
      await api(`/projects/${encodeURIComponent(project)}/roles`, {method: 'POST', body: JSON.stringify({name})});
      input.value = '';
      await loadProjectRoles();
      roleStatus(window.t('pj_role_created'));
    } catch (err) { roleStatus(window.t('pj_role_create_failed', [err.message]), true); }
  finally { btn.disabled = false; }
});
$('#pj-role-new-name').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.isComposing) {
    event.preventDefault(); event.stopPropagation(); $('#pj-role-create').click();
  }
});
const resettingMemberSessions = new Set();
async function resetMemberSession(project, profile, button, status) {
  const key = JSON.stringify([project, profile]);
  if (resettingMemberSessions.has(key)) return;
  resettingMemberSessions.add(key);
  button.disabled = true;
  button.textContent = window.t('pj_resetting');
  if (status) { status.hidden = false; status.textContent = window.t('pj_resetting_session'); }
  const notify = async message => {
    if (status?.isConnected) status.textContent = message;
    else await showAlert(message);
  };
  try {
    const result = await api(`/projects/${encodeURIComponent(project)}/members/${encodeURIComponent(profile)}/reset-session`, {method: 'POST'});
    if (!result.handled || !result.ok) throw new Error(result.detail || window.t('pj_session_reset_failed'));
    await notify(result.detail || window.t('pj_session_switched', [profile]));
  } catch (err) {
    await notify(err.message);
  } finally {
    resettingMemberSessions.delete(key);
    button.disabled = false;
    button.textContent = window.t('pj_reset_session');
  }
}

function openWorkspaceEditor(project, member) {
  if (projectMembersCache.get(project).project.directory_mode === 'shared') {
    const parent = $('#dlg-project-detail');
    if (parent.__outsideCloser) document.removeEventListener('click', parent.__outsideCloser, true);
    openWorkspaceResources(project, member.profile, () => {
      if (parent.open && parent.__outsideCloser) document.addEventListener('click', parent.__outsideCloser, true);
    }); return;
  }
  const parent = $('#dlg-project-detail');
  if (parent.__outsideCloser) document.removeEventListener('click', parent.__outsideCloser, true);
  const config = projectMembersCache.get(project).project;
  workspaceEditor = {project, profile: member.profile, member, root: config.root,
    mode: member.directory_mode, savedMode: member.directory_mode,
    path: member.assigned_workspace_path || member.independent_path || `${config.worktree_root}/${member.profile}`, saving: false};
  $('#workspace-title').textContent = window.t('pj_workspace') + ' · ' + (member.display_name || member.profile);
  document.querySelectorAll('[name="member-directory-mode"]').forEach(radio => radio.checked = radio.value === workspaceEditor.mode);
  $('#workspace-path').value = workspaceEditor.path;
  let resourcesButton = $('#workspace-resources');
  if (!resourcesButton) {
    resourcesButton = document.createElement('button'); resourcesButton.id = 'workspace-resources'; resourcesButton.type = 'button';
    resourcesButton.textContent = window.t('pj_resources'); $('#workspace-current').after(resourcesButton);
  }
  resourcesButton.onclick = () => openWorkspaceResources(project, member.profile);
  $('#workspace-remove').hidden = !member.workspace_assigned;
  $('#workspace-remove-confirm').hidden = true;
  $('#workspace-form').hidden = true;
  $('#workspace-result').textContent = '';
  $('#dlg-workspace').querySelectorAll('button, input').forEach(el => el.disabled = false);
  renderWorkspaceMode();
  $('#dlg-workspace').showModal();
}
function renderWorkspaceMode() {
  const editor = workspaceEditor, shared = editor.mode === 'shared', assigned = editor.member.workspace_assigned;
  const resourcesUnavailable = editor.mode !== editor.savedMode || (!shared && !assigned);
  $('#workspace-resources').hidden = resourcesUnavailable;
  $('#workspace-resources').disabled = resourcesUnavailable;
  let resourceHint = $('#workspace-resources-hint');
  if (!resourceHint) {
    resourceHint = document.createElement('p'); resourceHint.id = 'workspace-resources-hint';
    resourceHint.style.cssText = 'font-size:12px;color:var(--fg-3);margin:8px 0';
    $('#workspace-resources').after(resourceHint);
  }
  resourceHint.hidden = !resourcesUnavailable;
  resourceHint.textContent = !shared && !assigned
    ? window.t('pj_resources_after_create')
    : window.t('pj_resources_after_mode');
  $('#workspace-current').textContent = shared
    ? window.t('pj_workspace_root_info', [editor.root, assigned ? '\n' + window.t('pj_worktree_kept') + ': ' + editor.path : ''])
    : assigned ? window.t('pj_worktree_assigned', [editor.path]) : window.t('pj_no_workspace');
  $('#workspace-options').hidden = shared || !!assigned;
  $('#workspace-save-mode').hidden = editor.mode === editor.savedMode || (!shared && !assigned);
  $('#workspace-form').hidden = true;
  editor.action = null;
  $('#workspace-result').textContent = '';
}
document.querySelectorAll('[name="member-directory-mode"]').forEach(radio => radio.addEventListener('change', () => {
  if (radio.checked) { workspaceEditor.mode = radio.value; renderWorkspaceMode(); }
}));
$('#workspace-save-mode').addEventListener('click', () => {
  workspaceEditor.action = 'set_mode';
  saveWorkspace();
});
function chooseWorkspaceAction(action) {
  workspaceEditor.action = action;
  $('#workspace-form').hidden = false;
  $('#workspace-path-field').hidden = action === 'remove';
  $('#workspace-path').required = action !== 'remove';
  $('#workspace-remove-confirm').hidden = action !== 'remove';
  $('#workspace-help').textContent = action === 'create' ? window.t('pj_workspace_create_help')
    : action === 'adopt' ? window.t('pj_workspace_adopt_help')
    : window.t('pj_workspace_remove_help', [workspaceEditor.path]);
  $('#workspace-submit').textContent = {create: window.t('pj_confirm_create'), adopt: window.t('pj_confirm_adopt'), remove: window.t('pj_confirm_remove')}[action];
  $('#workspace-submit').classList.toggle('workspace-danger', action === 'remove');
  $('#workspace-result').textContent = '';
}
$('#workspace-create').addEventListener('click', () => chooseWorkspaceAction('create'));
$('#workspace-adopt').addEventListener('click', () => chooseWorkspaceAction('adopt'));
$('#workspace-remove').addEventListener('click', () => chooseWorkspaceAction('remove'));
$('#workspace-close').addEventListener('click', () => $('#dlg-workspace').close());
$('#dlg-workspace').addEventListener('cancel', e => { if (workspaceEditor?.saving) e.preventDefault(); });
$('#dlg-workspace').addEventListener('close', () => {
  workspaceEditor = null;
  const parent = $('#dlg-project-detail');
  if (parent.open && parent.__outsideCloser) document.addEventListener('click', parent.__outsideCloser, true);
});
$('#workspace-form').addEventListener('submit', async e => {
  e.preventDefault();
  await saveWorkspace();
});
async function saveWorkspace() {
  const editor = workspaceEditor;
  if (!editor?.action || editor.saving) return;
  editor.saving = true;
  $('#dlg-workspace').querySelectorAll('button, input').forEach(el => el.disabled = true);
  $('#workspace-result').textContent = window.t('pj_executing');
  try {
    const result = await api(`/projects/${encodeURIComponent(editor.project)}/workspaces/${encodeURIComponent(editor.profile)}`, {
      method: 'POST', body: JSON.stringify(editor.action === 'set_mode'
        ? {action: 'set_mode', mode: editor.mode}
        : {action: editor.action, path: editor.action === 'remove' ? editor.path : $('#workspace-path').value.trim()})});
    if (editor.action === 'create' || editor.action === 'adopt') {
      const action = editor.action;
      editor.path = result.path || $('#workspace-path').value.trim();
      editor.member = {...editor.member, workspace_assigned: true, directory_mode: 'worktree',
        assigned_workspace_path: editor.path, independent_path: editor.path};
      editor.mode = editor.savedMode = 'worktree';
      editor.saving = false;
      $('#dlg-workspace').querySelectorAll('button, input').forEach(el => el.disabled = false);
      $('#workspace-remove').hidden = false;
      renderWorkspaceMode();
      $('#workspace-result').textContent = window.t('pj_workspace_done', [action === 'create' ? window.t('pj_workspace_created') : window.t('pj_workspace_adopted')]);
      $('#workspace-resources').focus();
    } else $('#dlg-workspace').close();
    if ($('#pj-detail-form').dataset.origName === editor.project) await loadProjectMembers({profile: editor.profile});
    else projectMembersCache.delete(editor.project);
  } catch (err) {
    editor.saving = false;
    $('#workspace-result').textContent = err.message;
    $('#dlg-workspace').querySelectorAll('button, input').forEach(el => el.disabled = false);
  }
}
