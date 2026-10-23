/* Shared candidate selection; profile registration and project enrollment are separate actions. */
(() => {
  const normalize = item => ({...item, host_type: item.host_type || item.runtime,
    runtime: item.runtime || item.host_type, profile: item.profile || item.host_profile,
    host_profile: item.host_profile || item.profile});
  const load = async project => ((await api(`/profiles/candidates?project=${encodeURIComponent(project || '')}`, {signal: AbortSignal.timeout(10000)})).candidates || []).map(normalize);
  function render(container, options) {
    container.replaceChildren();
    const toolbar = document.createElement('div'), create = document.createElement('button'), refresh = document.createElement('button');
    toolbar.className = 'member-picker-toolbar';
    create.type = refresh.type = 'button'; create.textContent = '创建 profile'; refresh.textContent = '刷新列表';
    create.className = 'member-picker-create'; refresh.className = 'member-picker-refresh';
    const status = document.createElement('p'); status.className = 'profile-help'; status.setAttribute('role', 'status');
    toolbar.append(create, refresh); container.append(toolbar, status);
    const active = () => container.isConnected && options.isActive();
    const lock = busy => {
      container.querySelectorAll('button,input,select').forEach(node => node.disabled = busy || node.dataset.enrolled === 'true');
      options.onBusy?.(busy);
    };
    create.addEventListener('click', () => openProfileCreate({project: options.project, template: options.template,
      onCreated: async item => { if (active()) options.onSelect(normalize(item), true); }}));
    refresh.addEventListener('click', async () => {
      lock(true); status.textContent = '正在读取成员…';
      try {
        const items = await load(options.project);
        if (active()) options.onRefresh(items);
      } catch (error) { if (active()) status.textContent = `读取失败：${error.message}。可刷新重试。`; }
      finally { if (active()) lock(false); }
    });
    if (!options.items.length) status.textContent = '暂无候选成员，可创建 profile 或刷新宿主扫描结果。';
    for (const raw of options.items) {
      const item = normalize(raw), row = document.createElement('section'), label = document.createElement('label');
      row.className = 'import-card import-member';
      const selected = options.selected.has(item.bot_id), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = selected || !!item.already_project_member;
      check.disabled = !!item.already_project_member; check.dataset.enrolled = String(!!item.already_project_member);
      if (item.bot_id != null) check.dataset.botId = item.bot_id;
      check.dataset.profile = item.profile; check.dataset.host = item.host_type;
      row.classList.toggle('selected', check.checked);
      const identity = document.createElement('span'), name = document.createElement('strong'), hint = document.createElement('small');
      name.textContent = item.display_name || item.profile;
      hint.textContent = `${item.host_type} / ${item.profile}${item.already_project_member ? ' · 已加入项目' : ''}`;
      identity.append(name, hint); label.append(check, identity); row.append(label);
      check.addEventListener('change', async () => {
        const checked = check.checked;
        lock(true); status.textContent = '';
        try {
          if (checked && item.bot_id == null) {
            const result = await api('/bots/register', {method: 'POST', body: JSON.stringify({
              host_type: item.host_type, profile: item.profile, config_root: item.config_root})});
            Object.assign(raw, {bot_id: result.bot_id, bound: true}); item.bot_id = result.bot_id;
          }
          if (active()) options.onSelect(item, checked);
        } catch (error) {
          if (active()) { check.checked = !checked; status.textContent = `选择失败：${error.message}。可重试。`; }
        } finally { if (active()) lock(false); }
      });
      if (selected) {
        options.extra?.(row, item);
        for (const kind of ['sandbox', 'approval']) {
          if (!item[`${kind}_notice`]?.message) continue;
          const notice = document.createElement('p'); notice.className = 'profile-help member-sandbox-notice';
          notice.textContent = item[`${kind}_notice`].message; row.append(notice);
        }
      }
      container.append(row);
    }
  }
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-member-picker'; dialog.setAttribute('aria-labelledby', 'member-picker-title');
  dialog.innerHTML = '<h3 id="member-picker-title">选择成员</h3><p class="profile-help">勾选待加入成员，点击“加入项目”后入组。角色与权限在项目设置中配置。</p><div id="member-picker-list"></div><p id="member-picker-status" class="profile-help" role="status"></p><div class="dlg-btns"><button type="button" id="member-picker-close">取消</button><button type="button" id="member-picker-join" class="primary">加入项目</button></div>';
  document.body.append(dialog);
  const find = id => dialog.querySelector('#member-picker-' + id);
  let state = null;
  function paint() {
    const current = state;
    render(find('list'), {items: current.items, selected: current.selected, project: current.project,
      isActive: () => state === current && dialog.open,
      onBusy: busy => { current.busy = busy; find('join').disabled = busy || !current.selected.size; },
      onRefresh: items => {
        current.items = items;
        for (const item of items) if (item.already_project_member) current.selected.delete(item.bot_id);
        paint();
      },
      onSelect: (item, selected) => {
        if (!current.items.some(p => p.bot_id === item.bot_id)) current.items.push(item);
        if (selected) current.selected.set(item.bot_id, item); else current.selected.delete(item.bot_id);
        paint();
      }});
    find('join').disabled = current.busy || !current.selected.size;
  }
  async function openProject(project) {
    if (!project || dialog.open) return;
    const current = state = {project, selected: new Map(), items: [], busy: false};
    find('status').textContent = ''; paint(); dialog.showModal();
    find('list').querySelector('.member-picker-refresh').click();
  }
  find('close').addEventListener('click', () => { if (!state?.busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (state?.busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { if (!dialog.open) state = null; });
  find('join').addEventListener('click', async () => {
    const current = state;
    if (!current || current.busy || !current.selected.size) return;
    current.busy = true;
    dialog.querySelectorAll('button,input').forEach(node => node.disabled = true);
    find('status').textContent = '正在加入项目…';
    try {
      for (const item of [...current.selected.values()]) {
        await api('/bots/register', {method: 'POST', body: JSON.stringify({project: current.project,
          host_type: item.host_type, profile: item.profile, config_root: item.config_root})});
        current.selected.delete(item.bot_id);
        const saved = current.items.find(p => p.bot_id === item.bot_id);
        if (saved) saved.already_project_member = true;
      }
      dialog.close();
      if (document.querySelector('#pj-detail-form').dataset.origName === current.project) await loadProjectMembers();
      await loadMembers();
    } catch (error) {
      if (state === current) find('status').textContent = `加入失败：${error.message}。已成功成员保留，剩余选择可重试。`;
    } finally {
      current.busy = false; find('close').disabled = false;
      if (state === current) paint();
    }
  });
  window.MemberPicker = {render, load, normalize, openProject};
})();
