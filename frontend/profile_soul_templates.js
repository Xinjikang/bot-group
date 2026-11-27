// Project-bound SOUL references; applying a reference only changes the caller's draft.
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-profile-soul-template';
  dialog.setAttribute('aria-labelledby', 'profile-soul-template-title');
  dialog.innerHTML = `<h3 id="profile-soul-template-title">${window.t('st_title')}</h3>
    <p class="profile-help" id="profile-soul-template-project"></p>
    <label for="profile-soul-template-role">${window.t('st_role_label')}</label>
    <select id="profile-soul-template-role" disabled></select>
    <p class="profile-help" id="profile-soul-template-description"></p>
    <pre id="profile-soul-template-preview" tabindex="0" aria-label="${window.t('st_preview_aria')}" hidden></pre>
    <p class="profile-help" id="profile-soul-template-status" role="status"></p>
    <div class="dlg-btns"><button type="button" id="profile-soul-template-retry">${window.t('st_retry')}</button>
      <button type="button" id="profile-soul-template-close">${window.t('close')}</button>
      <button type="button" class="primary" id="profile-soul-template-apply" disabled>${window.t('st_apply')}</button></div>`;
  const find = id => dialog.querySelector('#profile-soul-template-' + id);
  // 弹窗内容在模块加载时构建一次；打开前按当前语言刷新静态文案。
  function refreshStaticCopy() {
    find('title').textContent = window.t('st_title');
    dialog.querySelector('label[for="profile-soul-template-role"]').textContent = window.t('st_role_label');
    find('preview').setAttribute('aria-label', window.t('st_preview_aria'));
    find('retry').textContent = window.t('st_retry');
    find('close').textContent = window.t('close');
    find('apply').textContent = window.t('st_apply');
  }
  let current = null, generation = 0;
  const live = state => current === state && dialog.open && state.owner.open && state.container.isConnected && state.isActive();
  function release() {
    if (current) current.owner.removeEventListener('close', current.ownerClosed);
    current = null;
    generation++;
  }
  function selected(state) { return state.references[Number(find('role').value)]; }
  function preview() {
    const state = current;
    if (!state || !live(state)) return;
    const reference = selected(state);
    find('preview').textContent = reference?.content || '';
    find('preview').hidden = !reference;
    find('description').textContent = reference?.responsibilities || '';
    find('apply').disabled = !reference;
    find('status').textContent = reference ? window.t('st_applied_editable') : window.t('st_none_available');
  }
  async function load() {
    const state = current, version = ++generation;
    if (!state || !live(state)) return;
    state.references = [];
    find('role').replaceChildren(); find('role').disabled = true;
    find('preview').textContent = ''; find('preview').hidden = true;
    find('description').textContent = '';
    find('apply').disabled = true; find('retry').disabled = true;
    find('status').textContent = window.t('st_loading');
    try {
      const result = state.template
        ? {template: state.template.id === 'blank' ? {...state.template, references: []} : await api(`/projects/templates/${encodeURIComponent(state.template.id)}`, {cache: 'no-store'})}
        : await api(`/projects/${encodeURIComponent(state.project)}/template-reference`, {cache: 'no-store'});
      if (!live(state) || version !== generation) return;
      if (state.template && result.template.version !== state.template.version) throw new Error(window.t('st_version_changed'));
      if (!state.template && result.project !== state.project) throw new Error(window.t('st_project_mismatch'));
      state.references = (result.template?.references || []).filter(ref => typeof ref.content === 'string' && ref.content.trim());
      find('project').textContent = state.template ? window.t('st_project_template', [state.template.name || state.template.id]) : window.t('st_project', [state.project]) + (result.template ? ` · ${result.template.name} · ${window.t('st_latest_ref')}` : '');
      find('role').replaceChildren(...state.references.map((ref, i) => new Option(ref.name || ref.role_id, String(i))));
      find('role').disabled = !state.references.length;
      preview();
    } catch (error) {
      if (live(state) && version === generation) find('status').textContent = window.t('st_read_failed', [error.message]);
    } finally {
      if (live(state) && version === generation) find('retry').disabled = false;
    }
  }
  find('role').addEventListener('change', preview);
  find('retry').addEventListener('click', load);
  find('close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (!dialog.open) { release(); dialog.remove(); } });
  find('apply').addEventListener('click', async () => {
    const state = current;
    if (!state || !live(state) || find('apply').disabled) return;
    const reference = selected(state), before = state.getValue();
    if (!reference) return;
    if (reference.content.length > state.maxLength) {
      find('status').textContent = window.t('st_too_long');
      return;
    }
    find('apply').disabled = true;
    try {
      if (before.trim() && before !== reference.content && !await showConfirm(window.t('st_replace_confirm'))) return;
      if (!live(state)) return;
      if (state.getValue() !== before) {
        find('status').textContent = window.t('st_changed_reapply');
        return;
      }
      state.setValue(reference.content);
      state.status.textContent = window.t('st_applied_ref', [reference.name || reference.role_id]);
      dialog.close();
    } finally {
      if (live(state)) find('apply').disabled = false;
    }
  });

  window.mountProjectSoulTemplates = (container, options) => {
    container.replaceChildren();
    container.hidden = !(options.project || options.template);
    container.classList.toggle('profile-soul-template-toolbar', !container.hidden);
    if (container.hidden) return;
    const button = document.createElement('button'), status = document.createElement('span');
    button.type = 'button'; button.className = 'profile-soul-template-open';
    button.textContent = window.t('st_open_button'); button.disabled = !!options.disabled;
    status.className = 'profile-help'; status.setAttribute('role', 'status');
    status.textContent = window.t('st_only_current');
    container.append(button, status);
    button.addEventListener('click', () => {
      if (!(options.project || options.template) || !options.isActive()) return;
      const owner = container.closest('dialog');
      if (!owner?.open) return;
      release();
      const state = {...options, container, owner, status, references: []};
      state.ownerClosed = () => { if (current === state && (!owner.open || !state.isActive())) dialog.close(); };
      current = state;
      owner.addEventListener('close', state.ownerClosed);
      // Keep nested clicks inside the caller's anchored-dialog interaction boundary.
      owner.appendChild(dialog);
      find('project').textContent = window.t('st_project', [state.project]);
      find('close').disabled = false;
      refreshStaticCopy();
      dialog.showModal(); load();
    });
  };
})();
