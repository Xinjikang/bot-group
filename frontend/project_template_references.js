// Text-only role references. Projects resolve the latest reference by template type.
(() => {
  window.renderTemplateReferences = (container, template, {snapshot = true} = {}) => {
    container.replaceChildren();
    const title = document.createElement('p');
    title.textContent = template ? `${template.name} · ${template.id} · v${template.version}` : '此项目使用空白初始化或已有约定。';
    container.append(title);
    if (!template) return;
    const note = document.createElement('p'); note.className = 'profile-help';
    note.textContent = '仅供参考，由用户自行复制到对应 Bot 配置；Codex 使用宿主支持的人格配置方式。' + (snapshot ? '当前展示应用时快照。' : '当前展示所选模板的参考原文。');
    container.append(note);
    for (const reference of template.references || []) {
      const details = document.createElement('details'), summary = document.createElement('summary');
      details.className = 'template-reference';
      summary.textContent = `${reference.name} · 查看角色与 SOUL 参考`;
      const responsibilities = document.createElement('p'); responsibilities.textContent = reference.responsibilities;
      const pre = document.createElement('pre'); pre.textContent = reference.content;
      pre.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;';
      const state = document.createElement('span'); state.setAttribute('role', 'status');
      const copy = document.createElement('button'); copy.type = 'button'; copy.textContent = '复制参考';
      copy.className = 'template-copy';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(reference.content); state.textContent = '已复制'; }
        catch { state.textContent = '剪贴板不可用，请从全文选择复制。'; }
      });
      const download = document.createElement('button'); download.type = 'button'; download.textContent = '下载参考';
      download.className = 'template-download';
      download.addEventListener('click', () => {
        const url = URL.createObjectURL(new Blob([reference.content], {type: 'text/markdown;charset=utf-8'}));
        const link = document.createElement('a'); link.href = url;
        link.download = `${template.id}-${template.version}-${reference.role_id}.md`;
        details.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
      const actions = document.createElement('div'); actions.className = 'template-reference-actions';
      actions.append(copy, download, state);
      details.append(summary, responsibilities, pre, actions); container.append(details);
    }
  };
  const dialog = document.createElement('dialog'); dialog.id = 'dlg-template-reference';
  dialog.style.cssText = 'width:660px;max-width:calc(100vw - 24px);box-sizing:border-box;max-height:calc(100dvh - 24px);overflow:auto;';
  dialog.innerHTML = '<h3>模板参考 · 最新参考</h3><div id="template-reference-content"></div><p id="template-reference-status" role="status"></p><div class="dlg-btns"><button id="template-reference-retry">重新读取</button><button id="template-reference-close">关闭</button></div>';
  document.body.append(dialog);
  let project = '', generation = 0;
  async function load() {
    const current = ++generation;
    const status = dialog.querySelector('#template-reference-status'); status.textContent = '读取中…';
    dialog.querySelector('#template-reference-content').replaceChildren();
    try {
      const result = await api(`/projects/${encodeURIComponent(project)}/template-reference`, {cache: 'no-store'});
      if (current !== generation || !dialog.open) return;
      renderTemplateReferences(dialog.querySelector('#template-reference-content'), result.template, {snapshot: false});
      status.textContent = '';
      anchorDialog(dialog, document.querySelector('#chip-all'));
    } catch (error) { if (current === generation) status.textContent = error.message; }
  }
  window.openProjectTemplateReferences = id => {
    project = id; closeDialogAnchored(document.querySelector('#dlg-project-detail'));
    openDialogAnchored(dialog, document.querySelector('#chip-all')); load();
  };
  dialog.querySelector('#template-reference-retry').addEventListener('click', load);
  dialog.querySelector('#template-reference-close').addEventListener('click', () => { closeDialogAnchored(dialog); openProjectDetail(project); });
  window.addEventListener('resize', () => { if (dialog.open) anchorDialog(dialog, document.querySelector('#chip-all')); });
  dialog.addEventListener('close', () => { if (!dialog.open) generation++; });
  document.querySelector('#pj-template-reference').addEventListener('click', () => openProjectTemplateReferences(document.querySelector('#pj-d-id').value));
})();
