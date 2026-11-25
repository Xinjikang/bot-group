// Requirement details use an exact project/id pair and render untrusted text as text.
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-requirement-details';
  dialog.setAttribute('aria-labelledby', 'requirement-details-title');
  dialog.style.cssText = 'width:760px;max-width:calc(100vw - 24px);max-height:85vh;overflow:auto;box-sizing:border-box';
  dialog.innerHTML = `<div class="task-detail-head">
    <button type="button" data-refresh data-i18n="refresh">刷新</button><button type="button" data-close data-i18n="close">关闭</button></div>
    <div class="task-detail-scroll"><h3 id="requirement-details-title" data-i18n="requirement_details_title">需求详情</h3>
    <div data-content aria-live="polite"></div></div>`;
  document.body.appendChild(dialog);
  const content = dialog.querySelector('[data-content]');
  content.style.fontSize = '12px';
  const title = dialog.querySelector('h3');
  let revision = 0, current;
  const labelText = value => window.t('req_status_' + value);
  const label = value => { const text = labelText(value); return text === 'req_status_' + value ? (value || '—') : text; };
  function text(tag, value, parent = content) {
    const node = document.createElement(tag);
    node.textContent = value;
    node.style.overflowWrap = 'anywhere';
    parent.appendChild(node);
    return node;
  }
  function render(data) {
    const req = data.requirement;
    title.textContent = window.t('requirement_pound_detail', [req.id, req.title]);
    content.replaceChildren();
    text('p', window.t('requirement_meta', [req.project, req.version || '—', label(req.status), req.archived ? window.t('open_paren') + window.t('archived') + window.t('close_paren') : '']));
    // req#111 task#191：未过拆解门禁的需求标「待拆解授权」+原因（gate_reason 由 scanner 落库，#192）
    if (req.gate_reason) {
      const gate = text('p', window.t('requirement_gate', [req.gate_reason]));
      gate.style.color = 'var(--warn)';
      gate.setAttribute('data-requirement-gate', 'blocked');
    }
    text('p', window.t('requirement_dates', [req.created_at || '—', req.updated_at || '—']));
    if (req.project_root) text('p', window.t('requirement_root', [req.project_root]));
    const fields = [['title', 'requirement_field_title'], ['background_goal', 'requirement_field_goal'], ['scope', 'requirement_field_scope'],
      ['approach_basis', 'requirement_field_approach'], ['acceptance', 'requirement_field_acceptance'],
      ['undecided', 'requirement_field_undecided'], ['source_ref', 'requirement_field_source']];
    for (const [key, heading] of fields) {
      const section = text('section', '');
      section.setAttribute('data-requirement-field', key);
      text('h4', window.t(heading), section);
      const value = typeof req[key] === 'string' ? req[key] : '';
      text('div', value.trim() ? value : window.t('requirement_not_filled', [window.t(heading)]), section).style.whiteSpace = 'pre-wrap';
    }
    if (req.description) {
      text('h4', window.t('requirement_legacy_desc'));
      text('div', req.description).style.whiteSpace = 'pre-wrap';
    }
    text('h4', window.t('requirement_linked_tasks', [data.tasks.length])).style.marginTop = '20px';
    if (!data.tasks.length) text('p', window.t('requirement_waiting_tasks'));
    for (const task of data.tasks) {
      const row = text('section', '');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:16px;border-top:1px solid var(--border);padding:12px 0';
      const section = text('div', '', row);
      section.style.cssText = 'flex:1;min-width:0';
      text('strong', `#${task.id} ${task.title}`, section);
      const assignee = task.claimed_by || task.next_bot;
      text('p', `${label(task.status)} · ${label(task.briefing_status)} · ${assignee ? memberLabelForProfile(assignee, S.members) : window.t('unassigned')}`, section);
      let deps;
      try { deps = JSON.parse(task.prerequisites || '[]'); } catch { deps = null; }
      text('p', window.t('requirement_prereqs', [Array.isArray(deps) ? (deps.map(id => '#' + id).join(window.t('workspace_sep')) || window.t('none')) : window.t('invalid_data')]), section);
      const blockers = task.dependency_blockers || [];
      if (blockers.length) text('p', window.t('waiting') + ': ' + blockers.map(dep => `${dep.id ? '#' + dep.id : ''} ${dep.title}${window.t('open_paren')}${label(dep.status)}${window.t('close_paren')}`).join(window.t('workspace_sep')), section);
      const button = text('button', window.t('requirement_view_task_log'), row);
      button.style.cssText = 'flex-shrink:0;white-space:nowrap';
      button.type = 'button';
      button.addEventListener('click', () => openTaskHistory(task.id, req.project));
    }
  }
  async function open(id, project) {
    current = {id, project};
    const request = ++revision;
    title.textContent = window.t('requirement_pound_short', [id]);
    content.replaceChildren();
    text('p', window.t('loading'));
    if (!dialog.open) dialog.showModal();
    try {
      const data = await api(`/requirements/${encodeURIComponent(id)}?project=${encodeURIComponent(project)}`);
      if (request === revision && dialog.open) render(data);
    } catch (error) {
      if (request === revision && dialog.open) {
        content.replaceChildren();
        text('p', window.t('requirement_load_failed', [error.message || error]));
      }
    }
  }
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
  dialog.querySelector('[data-refresh]').addEventListener('click', () => open(current.id, current.project));
  dialog.addEventListener('close', () => { revision++; });
  window.openRequirementDetails = open;
})();
