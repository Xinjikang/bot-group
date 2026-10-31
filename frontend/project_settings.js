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
    if(dirty)$('#pj-permissions-status').textContent='助手已更新服务端配置，当前未保存的权限选择已保留。';
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
  $('#pj-permissions-status').textContent = '正在读取权限…';
  try {
    const data = await api(`/projects/${encodeURIComponent(project)}/permissions`);
    if (version !== projectPermissionsVersion || $('#pj-detail-form').dataset.origName !== project) return;
    projectPermissionsEditor = data;
    renderProjectPermissions(data);
    $('#pj-permissions-save').disabled = false;
    $('#pj-permissions-status').textContent = '未勾选的成员无对应权限。';
  } catch (err) {
    if (version === projectPermissionsVersion) $('#pj-permissions-status').textContent = '读取失败：' + err.message;
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
      text.textContent = `${memberLabel(member)}${member.missing ? ' · 已删除' : !member.active ? ' · 已停用' : ''}`;
      label.append(input, text); options.appendChild(label);
    }
    for (const role of roles) {
      const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      input.type = 'checkbox'; input.value = `role:${role.id}`; input.checked = selected.includes(`role:${role.id}`);
      input.addEventListener('change', () => {
        if (!input.checked && selected.includes(input.value)) showAlert(`准备撤销角色「${role.name}」的「${permission.label}」授权。保存后，依赖此权限的职责可能缺少有权限的执行人，请核对项目协作正文与角色授权。`);
      });
      text.textContent = `角色：${role.name}（${role.member_count} 名成员）`;
      label.append(input, text); options.appendChild(label);
    }
    const updateSummary = () => {
      const chosen = [...options.querySelectorAll('input:checked')].map(input => {
        const role = roles.find(r => `role:${r.id}` === input.value);
        if (role) return `角色：${role.name}`;
        const member = members.find(m => m.profile === input.value);
        return member ? memberDisplayName(member) : input.value;
      });
      summary.textContent = chosen.length ? `${chosen.slice(0, 2).join('、')}${chosen.length > 2 ? ' 等' + chosen.length + '项' : ''}` : '选择成员或角色…';
    };
    options.addEventListener('change', updateSummary);
    updateSummary();
    if (!members.length) options.textContent = '暂无成员';
    row.appendChild(picker); box.appendChild(row);
  }
}
$('#pj-permissions-reload').addEventListener('click', async () => {
  if (projectPermissionsEditor && !await showConfirm('放弃尚未保存的权限修改，恢复服务端最新配置？')) return;
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
  $('#pj-permissions-status').textContent = '正在保存…';
  try {
    const result = await api(`/projects/${encodeURIComponent(editor.project)}/permissions`, {
      method: 'PUT', body: JSON.stringify({grants, revision: editor.revision})});
    if (version !== projectPermissionsVersion) return;
    projectPermissionsEditor = result;
    renderProjectPermissions(result);
    $('#pj-permissions-status').textContent = '权限已保存，下次工具调用时生效。';
  } catch (err) {
    if (version === projectPermissionsVersion) $('#pj-permissions-status').textContent = '保存失败：' + err.message + '（选择已保留）';
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
  memberAdd = {host: 'hermes', sources: [], scanning: false, error: '',
    busy: false, project: options.project || '', template: options.template,
    onCreated: options.onCreated,
    draft: {profile: '', display_name: '', model: '', instructions: '', source: ''},
    requestId: [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2, '0')).join('')};
}
function memberProfileError(value) {
  if (!value) return '请填写 Profile 标识。';
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(value)) return 'Profile 标识须为 1–64 位小写英文字母、数字、短横线或下划线，并以小写英文字母或数字开头，例如 dev-01。';
  if (['default', 'user', 'profiles'].includes(value) || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/.test(value)) return '该 Profile 标识为系统保留名称，请换一个标识。';
  return '';
}
function memberCreateValid() {
  return !memberProfileError(memberAdd.draft.profile);
}
function renderMemberCreate(steps) {
  const form = document.createElement('div');
  form.className = 'member-create-fields';
  form.innerHTML = `<label for="member-create-name">显示名称</label><input id="member-create-name" maxlength="100" placeholder="例如：小夏">
    <label for="member-create-profile">Profile 标识 <span class="profile-help">必填</span></label>
    <input id="member-create-profile" required maxlength="64" placeholder="例如：dev-01" autocomplete="off" spellcheck="false" aria-describedby="member-create-profile-help">
    <p class="profile-help" id="member-create-profile-help">使用 1–64 位小写英文字母、数字、短横线或下划线，以小写英文字母或数字开头。系统会自动准备独立目录。</p>
    <label for="member-create-source">配置来源</label><select id="member-create-source"><option value="">使用默认设置</option></select>
    <p class="profile-help" id="member-create-auth-help"></p>
    <div id="member-create-soul-templates"></div>
    <details><summary>更多设置</summary>
      <label for="member-create-model">模型</label><input id="member-create-model" maxlength="200" placeholder="留空沿用来源或运行时默认模型">
      <label for="member-create-instructions">个人指令</label><textarea id="member-create-instructions" rows="4" maxlength="32000" placeholder="填写性格、专长或工作偏好；项目职责在加入后设置。"></textarea>
    </details>`;
  const draft = memberAdd.draft;
  form.querySelector('#member-create-profile').dialogValidation = memberProfileError;
  const details = form.querySelector('details');
  details.open = !!draft.expanded;
  details.addEventListener('toggle', () => { draft.expanded = details.open; });
  for (const [id, key] of [['name', 'display_name'], ['profile', 'profile'], ['model', 'model'], ['instructions', 'instructions']]) {
    const input = form.querySelector('#member-create-' + id);
    input.value = draft[key];
    input.addEventListener('input', () => {
      draft[key] = input.value;
      if (key === 'profile') draft.profileTouched = true;
      $('#member-add-join').disabled = !memberCreateValid() || memberAdd.busy || memberAdd.scanning;
    });
  }
  const source = form.querySelector('#member-create-source');
  memberAdd.sources.forEach((p, i) => source.add(new Option(`${p.display_name || p.profile}（${p.profile}）`, String(i))));
  source.value = draft.source;
  const updateAuth = () => {
    if (memberAdd.host === 'codex') {
      form.querySelector('#member-create-auth-help').textContent = '自动使用主账户认证；新 Bot 统一开启智能审批（帮我审批）和沙箱，加入项目时获得项目目录授权。';
      return;
    }
    form.querySelector('#member-create-auth-help').textContent = '自动继承来源的模型配置与凭证；使用默认设置时读取主 profile。消息渠道独立配置，OAuth 登录需单独完成。';
  };
  source.addEventListener('change', () => { draft.source = source.value; updateAuth(); });
  updateAuth();
  steps.appendChild(form);
  if (draft.profileTouched) validateDialogField(form.querySelector('#member-create-profile'));
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
function renderMemberAdd() {
  const steps = $('#member-add-steps'), status = $('#member-add-status');
  status.dataset.error = String(!!memberAdd.error);
  const join = $('#member-add-join');
  join.disabled = memberAdd.busy || memberAdd.scanning || !memberCreateValid();
  join.textContent = '创建 profile';
  steps.replaceChildren();
  $('#member-add-title').textContent = '创建 profile';
  status.textContent = memberAdd.scanning ? '正在读取配置来源…' : memberAdd.error;
  const host = document.createElement('div');
  host.className = 'project-mode-options';
  host.setAttribute('role', 'radiogroup'); host.setAttribute('aria-label', '宿主类型');
  for (const [value, label] of [['hermes', 'Hermes'], ['codex', 'Codex']]) {
    const choice = document.createElement('label'), input = document.createElement('input');
    input.type = 'radio'; input.name = 'member-add-host'; input.value = value;
    input.checked = memberAdd.host === value;
    input.addEventListener('change', () => { memberAdd.host = value; loadMemberCreateSources(); });
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
    const defaultSource = current.sources.findIndex(p => p.profile === 'default');
    current.draft.source = defaultSource >= 0 ? String(defaultSource) : current.sources.length ? '0' : '';
  } catch (err) {
    if (memberAdd === current && current.scanVersion === scanVersion) current.error = `配置来源读取失败：${err.message}。可切换宿主重试，或使用默认设置。`;
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
  $('#member-add-status').textContent = '正在创建 profile…';
  $('#member-add-status').dataset.error = 'false';
  const draft = current.draft, source = current.sources[Number(draft.source)];
  const payload = {host_type: current.host, profile: draft.profile,
    display_name: draft.display_name.trim(), model: draft.model.trim(), instructions: draft.instructions,
    source: draft.source !== '' && source ? {profile: source.profile, config_root: source.config_root} : null,
    request_id: current.requestId};
  try {
    const result = await api('/profiles/create', {method: 'POST', body: JSON.stringify(payload)});
    if (memberAdd !== current) return;
    current.busy = false;
    dialog.close();
    await current.onCreated({...result, display_name: draft.display_name.trim(), bound: true});
    if (result.setup_command) {
      await showAlert(`${result.notice}\n\n在引擎所在电脑的终端执行：\n${result.setup_command}\n\nProfile 目录：${result.home}`);
    }
  } catch (err) {
    if (memberAdd !== current) return;
    current.error = `创建失败：${err.message}。填写内容已保留，可修改后重试。`;
    current.busy = false;
    renderMemberAdd();
  } finally {
    $('#member-add-close').disabled = false;
  }
}

async function loadProjectMembers(options = {}) {
  const project = $('#pj-detail-form').dataset.origName;
  if (!project) return;
  const cached = projectMembersCache.get(project);
  if (cached) renderProjectMembers(project, cached);
  else $('#pj-member-list').textContent = '正在读取成员…';
  $('#pj-members-status').textContent = cached ? '正在更新状态…' : '正在校验目录…';
  if (!options.profile && projectMembersPending.has(project)) return projectMembersPending.get(project);
  const version = (projectMembersVersions.get(project) || 0) + 1;
  projectMembersVersions.set(project, version);
  const request = (async () => {
    try {
      const suffix = options.profile && cached ? `?profile=${encodeURIComponent(options.profile)}` : '';
      const data = await api(`/projects/${encodeURIComponent(project)}/workspaces${suffix}`);
      if (projectMembersVersions.get(project) !== version) return;
      const previous = projectMembersCache.get(project);
      const merged = suffix && previous ? {...data, members: previous.members.map(m => data.members.find(n => n.profile === m.profile) || m)} : data;
      projectMembersCache.set(project, merged);
      if ($('#pj-detail-form').dataset.origName !== project) return;
      renderProjectMembers(project, merged);
      $('#pj-members-status').textContent = '状态已更新';
    } catch (err) {
      if ($('#pj-detail-form').dataset.origName === project && projectMembersVersions.get(project) === version) {
        $('#pj-members-status').textContent = '刷新失败：' + err.message;
        if (!projectMembersCache.has(project)) $('#pj-member-list').textContent = '暂时无法读取成员，请重试。';
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
    return {disabled:false, label:'重置会话', title:'重置本项目已有工作会话，保持成员启停状态'};
  if (member.work_session_exists === false)
    return {disabled:true, label:'暂无会话', title:'本项目暂无工作会话'};
  return {disabled:true, label:'状态待确认', title:'请刷新成员列表后重试'};
}

function bindMemberNameEditor(row, member) {
  const button = row.querySelector('.member-name-edit');
  button.addEventListener('click', () => {
    if (row.querySelector('.member-name-input')) return;
    const input = document.createElement('input');
    input.className = 'member-name-input';
    input.type = 'text'; input.maxLength = 200;
    input.value = member.display_name || member.host_profile || member.profile;
    input.setAttribute('aria-label', '显示名（所有项目共用）');
    const status = document.createElement('p');
    status.className = 'project-member-state'; status.setAttribute('role', 'status');
    status.textContent = '所有项目共用 · Enter 保存 · Esc 取消';
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
        status.textContent = '请输入非空的单行显示名';
        status.dataset.error = 'true';
        input.setAttribute('aria-invalid', 'true'); return;
      }
      if (name === member.display_name) { finish(); return; }
      saving = true; input.disabled = true;
      status.textContent = '正在保存显示名…';
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
        status.textContent = `显示名保存失败：${err.message}`;
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
    const path = state.path || `${data.project.worktree_root || ''}/${member.profile}`;
    const row = document.createElement('div');
    row.className = 'project-member-card';
    row.dataset.profile = member.profile;
    row.innerHTML = `<div class="project-member-header"><div class="project-member-identity">
      <div class="project-member-name"><button type="button" class="member-name-edit" title="点击编辑显示名（所有项目共用）">${esc(memberDisplayName(member))}</button><small>${esc(memberHostProfile(member))}</small>
      <button type="button" role="switch" aria-checked="${!!member.active}" aria-label="成员启用状态" title="${member.active ? '点击停用' : '点击启用'}" class="project-member-badge ${member.active ? 'enabled' : ''}">${member.active ? '启用' : '停用'}</button></div></div>
      <div class="project-member-actions"><button type="button" class="workspace">工作区</button>
      <button type="button" class="member-more" aria-label="成员更多操作" aria-haspopup="menu" aria-expanded="false">⋯</button>
      <div class="project-member-menu" popover="auto" role="menu">
      <button type="button" role="menuitem" class="edit" title="修改将影响共用此 profile 的项目">编辑 profile</button>
      ${member.profile !== 'user' ? `<button type="button" role="menuitem" class="reset-session" title="${reset.title}" ${reset.disabled ? 'disabled' : ''}>${reset.label}</button><button type="button" role="menuitem" class="member-remove">移出项目</button>` : ''}
      </div></div></div>
      <div class="project-member-workspace ${state.valid && !state.head ? 'compact-status' : ''}"><span class="project-member-caption">${independent ? '独立 worktree' : '项目主目录'}</span>
      <div class="project-member-path" title="${esc(path)}">${esc(path)}</div>
      <p class="project-member-state ${state.valid ? 'valid' : 'invalid'}">${state.valid ? esc(state.head ? `${state.detached ? 'detached' : state.branch} · ${state.head.slice(0, 12)}${state.dirty ? ' · 有未提交改动' : ' · 工作区干净'}` : '目录可用') : esc(state.error || '目录不可用')}</p></div>`;
    bindMemberNameEditor(row, member);
    row.querySelector('.edit').addEventListener('click', () => {
      profileProjectReturn = project;
      closeDialogAnchored($('#dlg-project-detail'));
      openProfileEditor(member, project);
    });
    row.querySelector('.workspace')?.addEventListener('click', () => openWorkspaceEditor(project, member));
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
        button.textContent = active ? '启用' : '停用';
        button.classList.toggle('enabled', active);
        button.setAttribute('aria-checked', String(active));
        button.title = active ? '点击停用' : '点击启用';
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
    roleBtn.textContent = member.role ? member.role : '设置角色';
    roleBtn.setAttribute('aria-haspopup', 'menu');
    roleBtn.setAttribute('aria-label', `${member.display_name || member.profile}的项目角色${member.role ? `：${member.role}` : '（未设置）'}`);
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
  dialog.innerHTML = '<h3 id="member-remove-title">移出项目</h3><p class="member-remove-summary"></p><p class="profile-help">移出后将解除本项目成员关系、角色与权限，并退出话题参与名单。历史消息、任务记录、profile 和工作目录保留，其他项目中的成员关系保持原样。后续可通过“添加成员”重新加入，并重新配置角色与权限。</p><p class="profile-help">请根据剩余成员的角色分工调整协作流程，明确相关职责的承担者。</p><p class="member-remove-status" role="status"></p><div class="member-remove-blockers"></div><div class="dlg-btns"><button type="button" class="cancel">取消</button><button type="button" class="recheck">重新检查</button><button type="button" class="confirm" disabled>移出项目</button></div>';
  const projectName = S.projects.find(item => item.name === project)?.display_name || project;
  dialog.querySelector('.member-remove-summary').textContent = `将「${memberLabel(member)}」移出「${projectName}」？`;
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
        action.textContent = item.kind === 'runtime' ? '停止运行' : item.kind === 'owner' ? '更换负责人' : '查看并处理';
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
    confirm.disabled = true; recheck.disabled = true; status.textContent = '正在检查职责与运行状态…';
    try {
      const result = await api(endpoint + '/removal');
      if (!dialog.isConnected) return;
      showBlockers(result.blockers);
      confirm.disabled = !result.can_remove;
      status.textContent = result.can_remove ? '检查通过，可以移出项目。' : '请先处理以下事项，再重新检查：';
    } catch (error) { if (dialog.isConnected) status.textContent = `检查失败：${error.message}`; }
    finally { recheck.disabled = false; }
  }
  cancel.addEventListener('click', () => dialog.close()); recheck.addEventListener('click', check);
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove());
  confirm.addEventListener('click', async () => {
    busy = true; confirm.disabled = cancel.disabled = recheck.disabled = true;
    status.textContent = '正在移出项目…';
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
          ? '已移出项目，工作目录已保留。部分列表刷新失败，请刷新状态。' : '已移出项目，工作目录已保留。';
      }
    } catch (error) {
      status.textContent = `移出失败：${error.message}`;
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
  note.textContent = '正在读取角色…';
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
      empty.textContent = '本项目尚未定义角色（到「角色」tab 创建）';
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
          roleBtn.textContent = result.role || '设置角色';
          closeMenu();
          resetStatus.hidden = false; resetStatus.textContent = '本项目角色已保存';
          await loadMembers();
        } catch (err) { resetStatus.hidden = false; resetStatus.textContent = `角色保存失败：${err.message}`; item.disabled = false; }
      });
      menu.appendChild(item);
    });
    positionMenu();
  }).catch(err => {
    if (!menu.isConnected) return;
    menu.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'project-member-role-empty';
    empty.textContent = `角色列表读取失败：${err.message}`;
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
  box.textContent = '正在读取角色…';
  try {
    const data = await api(`/projects/${encodeURIComponent(project)}/roles`);
    if ($('#pj-detail-form').dataset.origName !== project) return;
    projectRolesCache.set(project, data.roles || []);
    renderProjectRoles(project, data.roles || []);
    roleStatus('');
  } catch (err) {
    if ($('#pj-detail-form').dataset.origName !== project) return;
    box.textContent = '角色列表读取失败，请重试。';
    roleStatus(err.message, true);
  }
}
function renderProjectRoles(project, roles) {
  const box = $('#pj-role-list');
  box.replaceChildren();
  if (!roles.length) {
    box.textContent = '本项目还没有角色。在上方输入名称创建第一个角色。';
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
    nameBtn.title = `改名「${role.name}」（稳定 ID ${role.id}，改名家属成员关联不断）`;
    const count = document.createElement('span');
    count.className = 'profile-help';
    count.style.margin = '0';
    count.textContent = `${role.member_count} 名成员`;
    const edit = document.createElement('button');
    edit.type = 'button'; edit.textContent = '改名';
    const del = document.createElement('button');
    del.type = 'button'; del.textContent = '删除';
    row.append(nameBtn, count, edit);
    if (!(role.is_owner ?? role.name === '项目负责人')) row.append(del);
    del.disabled = role.member_count > 0;
    if (role.member_count > 0) del.title = '仍有成员使用此角色，先改派成员后再删';
    box.appendChild(row);
    nameBtn.addEventListener('click', () => startRoleRename(project, role, row, nameBtn));
    edit.addEventListener('click', () => startRoleRename(project, role, row, nameBtn));  // req#96 #165：改名按钮此前漏绑监听
    del.addEventListener('click', async () => {
      if (!await showConfirm(`确认删除角色「${role.name}」？删除后，派工、审查、验收等职责可能缺少执行人。请在项目详情的 AGENTS.md 中根据调整后的角色分工修改协作流程，重新明确各项职责的承担者，并同步更新角色引用与职责。`)) return;
      del.disabled = true;
      try {
        await api(`/projects/${encodeURIComponent(project)}/roles/${role.id}`, {method: 'DELETE'});
        await loadProjectRoles();
        roleStatus('角色已删除');
      } catch (err) { roleStatus(`删除失败：${err.message}`, true); del.disabled = false; }
    });
  }
}
async function startRoleRename(project, role, row, nameBtn) {
  if (row.querySelector('.pj-role-rename')) return;
  await showAlert('角色改名后，请自行核对项目协作正文中的角色引用，确保派工、审查和验收职责对应的执行人清晰。');
  if (!row.isConnected || row.querySelector('.pj-role-rename')) return;
  const input = document.createElement('input');
  input.className = 'pj-role-rename';
  input.type = 'text'; input.maxLength = 100;
  input.value = role.name;
  input.setAttribute('aria-label', `改名角色 ${role.name}`);
  const finish = () => { input.remove(); nameBtn.hidden = false; };
  let saving = false;
  const save = async () => {
    if (saving) return;
    const name = input.value.trim();
    if (!name) { roleStatus('角色名不能为空', true); return; }
    if (name === role.name) { finish(); return; }
    saving = true; input.disabled = true;
    roleStatus('正在保存角色名…');
    try {
      await api(`/projects/${encodeURIComponent(project)}/roles/${role.id}`,
        {method: 'PATCH', body: JSON.stringify({name})});
      await loadProjectRoles();
      // 状态提示放在重载之后（loadProjectRoles 会先清状态）：改名结果稳定可见
      roleStatus('角色已改名（成员关联保持）');
    } catch (err) {
      roleStatus(`角色改名失败：${err.message}`, true);
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
  if (!name) { roleStatus('请输入角色名', true); return; }
  const btn = $('#pj-role-create');
  btn.disabled = true;
  try {
      await api(`/projects/${encodeURIComponent(project)}/roles`, {method: 'POST', body: JSON.stringify({name})});
      input.value = '';
      await loadProjectRoles();
      roleStatus('角色已创建');
    } catch (err) { roleStatus(`创建失败：${err.message}`, true); }
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
  button.textContent = '重置中…';
  if (status) { status.hidden = false; status.textContent = '正在重置工作会话…'; }
  const notify = async message => {
    if (status?.isConnected) status.textContent = message;
    else await showAlert(message);
  };
  try {
    const result = await api(`/projects/${encodeURIComponent(project)}/members/${encodeURIComponent(profile)}/reset-session`, {method: 'POST'});
    if (!result.handled || !result.ok) throw new Error(result.detail || '会话重置失败');
    await notify(result.detail || `@${profile} 的工作会话已切换。`);
  } catch (err) {
    await notify(err.message);
  } finally {
    resettingMemberSessions.delete(key);
    button.disabled = false;
    button.textContent = '重置会话';
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
  $('#workspace-title').textContent = `工作区 · ${member.display_name || member.profile}`;
  document.querySelectorAll('[name="member-directory-mode"]').forEach(radio => radio.checked = radio.value === workspaceEditor.mode);
  $('#workspace-path').value = workspaceEditor.path;
  let resourcesButton = $('#workspace-resources');
  if (!resourcesButton) {
    resourcesButton = document.createElement('button'); resourcesButton.id = 'workspace-resources'; resourcesButton.type = 'button';
    resourcesButton.textContent = '资源配置'; $('#workspace-current').after(resourcesButton);
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
    ? '创建工作区或使用已有工作区后，即可配置资源。'
    : '保存工作区模式后，即可配置资源。';
  $('#workspace-current').textContent = shared
    ? `项目主目录：${editor.root}${assigned ? '\n已有 worktree 保留：' + editor.path : ''}`
    : assigned ? `已分配 worktree：${editor.path}` : '尚未分配工作区。选择创建新目录或使用已有 worktree。';
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
  $('#workspace-help').textContent = action === 'create' ? '从项目主目录当前 HEAD 创建 detached worktree，并分配给该成员。'
    : action === 'adopt' ? '验证并使用已有 worktree，保留其分支、提交和文件。'
    : `将删除工作目录：${workspaceEditor.path}。Git 分支保留；有未提交改动或未受分支保护的提交时拒绝移除。`;
  $('#workspace-submit').textContent = {create: '确认创建', adopt: '确认接管', remove: '确认移除工作区'}[action];
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
  $('#workspace-result').textContent = '正在执行…';
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
      $('#workspace-result').textContent = `${action === 'create' ? '工作区已创建' : '已使用已有工作区'}，可继续查看和调整资源配置。`;
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
