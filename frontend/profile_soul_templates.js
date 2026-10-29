// Project-bound SOUL references; applying a reference only changes the caller's draft.
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-profile-soul-template';
  dialog.setAttribute('aria-labelledby', 'profile-soul-template-title');
  dialog.innerHTML = `<h3 id="profile-soul-template-title">当前项目的 SOUL 模板</h3>
    <p class="profile-help" id="profile-soul-template-project"></p>
    <label for="profile-soul-template-role">角色模板</label>
    <select id="profile-soul-template-role" disabled></select>
    <p class="profile-help" id="profile-soul-template-description"></p>
    <pre id="profile-soul-template-preview" tabindex="0" aria-label="SOUL 模板全文" hidden></pre>
    <p class="profile-help" id="profile-soul-template-status" role="status"></p>
    <div class="dlg-btns"><button type="button" id="profile-soul-template-retry">重新读取</button>
      <button type="button" id="profile-soul-template-close">关闭</button>
      <button type="button" class="primary" id="profile-soul-template-apply" disabled>引用模板</button></div>`;
  const find = id => dialog.querySelector('#profile-soul-template-' + id);
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
    find('status').textContent = reference ? '引用后可继续编辑，点击创建或保存时写入 profile。' : '当前项目没有可用的 SOUL 模板。';
  }
  async function load() {
    const state = current, version = ++generation;
    if (!state || !live(state)) return;
    state.references = [];
    find('role').replaceChildren(); find('role').disabled = true;
    find('preview').textContent = ''; find('preview').hidden = true;
    find('description').textContent = '';
    find('apply').disabled = true; find('retry').disabled = true;
    find('status').textContent = '正在读取当前项目的 SOUL 模板…';
    try {
      const result = state.template
        ? {template: state.template.id === 'blank' ? {...state.template, references: []} : await api(`/projects/templates/${encodeURIComponent(state.template.id)}`, {cache: 'no-store'})}
        : await api(`/projects/${encodeURIComponent(state.project)}/template-reference`, {cache: 'no-store'});
      if (!live(state) || version !== generation) return;
      if (state.template && result.template.version !== state.template.version) throw new Error('模板版本已变化，请返回项目向导重新读取。');
      if (!state.template && result.project !== state.project) throw new Error('模板与当前项目不一致，请重新读取。');
      state.references = (result.template?.references || []).filter(ref => typeof ref.content === 'string' && ref.content.trim());
      find('project').textContent = state.template ? `项目模板：${state.template.name || state.template.id}` : `项目：${state.project}` + (result.template ? ` · ${result.template.name} · 最新参考` : '');
      find('role').replaceChildren(...state.references.map((ref, i) => new Option(ref.name || ref.role_id, String(i))));
      find('role').disabled = !state.references.length;
      preview();
    } catch (error) {
      if (live(state) && version === generation) find('status').textContent = '读取失败：' + error.message;
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
      find('status').textContent = '模板超过当前编辑框的长度上限，请手动选取需要的内容。';
      return;
    }
    find('apply').disabled = true;
    try {
      if (before.trim() && before !== reference.content && !await showConfirm('用所选 SOUL 模板替换当前编辑框中的正文？确认后仍可编辑。')) return;
      if (!live(state)) return;
      if (state.getValue() !== before) {
        find('status').textContent = '正文已变化，请核对后再次引用。';
        return;
      }
      state.setValue(reference.content);
      state.status.textContent = `已引用「${reference.name || reference.role_id}」，可继续编辑。`;
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
    button.textContent = '查看 / 引用 SOUL 模板'; button.disabled = !!options.disabled;
    status.className = 'profile-help'; status.setAttribute('role', 'status');
    status.textContent = '仅显示当前项目的模板。';
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
      find('project').textContent = `项目：${state.project}`;
      find('close').disabled = false;
      dialog.showModal(); load();
    });
  };
})();
