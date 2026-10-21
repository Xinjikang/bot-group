// Requirement details use an exact project/id pair and render untrusted text as text.
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-requirement-details';
  dialog.setAttribute('aria-labelledby', 'requirement-details-title');
  dialog.style.cssText = 'width:760px;max-width:calc(100vw - 24px);max-height:85vh;overflow:auto;box-sizing:border-box';
  dialog.innerHTML = `<div class="task-detail-head">
    <button type="button" data-refresh>刷新</button><button type="button" data-close>关闭</button></div>
    <div class="task-detail-scroll"><h3 id="requirement-details-title">需求详情</h3>
    <div data-content aria-live="polite"></div></div>`;
  document.body.appendChild(dialog);
  const content = dialog.querySelector('[data-content]');
  content.style.fontSize = '12px';
  const title = dialog.querySelector('h3');
  let revision = 0, current;
  const labels = {open:'进行中', pending:'待领取', in_progress:'执行中', blocked:'已阻塞',
    done:'已完成', cancelled:'已取消', ready:'已发布', draft:'草稿', needs_details:'待补充', missing:'缺失', invalid:'无效'};
  const label = value => labels[value] || value || '—';
  function text(tag, value, parent = content) {
    const node = document.createElement(tag);
    node.textContent = value;
    node.style.overflowWrap = 'anywhere';
    parent.appendChild(node);
    return node;
  }
  function render(data) {
    const req = data.requirement;
    title.textContent = `需求 #${req.id} · ${req.title}`;
    content.replaceChildren();
    text('p', `项目：${req.project}　版本：${req.version || '—'}　状态：${label(req.status)}${req.archived ? '（已归档）' : ''}`);
    // req#111 task#191：未过拆解门禁的需求标「待拆解授权」+原因（gate_reason 由 scanner 落库，#192）
    if (req.gate_reason) {
      const gate = text('p', `待拆解授权 · ${req.gate_reason}`);
      gate.style.color = 'var(--warn)';
      gate.setAttribute('data-requirement-gate', 'blocked');
    }
    text('p', `创建：${req.created_at || '—'}　更新：${req.updated_at || '—'}`);
    if (req.project_root) text('p', `项目目录：${req.project_root}`);
    const fields = [['title', '标题'], ['background_goal', '背景与目标'], ['scope', '范围'],
      ['approach_basis', '方案与依据'], ['acceptance', '验收标准'],
      ['undecided', '未决事项'], ['source_ref', '来源关联']];
    for (const [key, heading] of fields) {
      const section = text('section', '');
      section.setAttribute('data-requirement-field', key);
      text('h4', heading, section);
      const value = typeof req[key] === 'string' ? req[key] : '';
      text('div', value.trim() ? value : `暂未填写${heading}`, section).style.whiteSpace = 'pre-wrap';
    }
    if (req.description) {
      text('h4', '历史需求描述');
      text('div', req.description).style.whiteSpace = 'pre-wrap';
    }
    text('h4', `关联任务 · ${data.tasks.length}`).style.marginTop = '20px';
    if (!data.tasks.length) text('p', '等待拆解任务');
    for (const task of data.tasks) {
      const row = text('section', '');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:16px;border-top:1px solid var(--border);padding:12px 0';
      const section = text('div', '', row);
      section.style.cssText = 'flex:1;min-width:0';
      text('strong', `#${task.id} ${task.title}`, section);
      const assignee = task.claimed_by || task.next_bot;
      text('p', `${label(task.status)} · ${label(task.briefing_status)} · ${assignee ? memberLabelForProfile(assignee, S.members) : '待分配'}`, section);
      let deps;
      try { deps = JSON.parse(task.prerequisites || '[]'); } catch { deps = null; }
      text('p', `前置依赖：${Array.isArray(deps) ? (deps.map(id => '#' + id).join('、') || '无') : '数据无效'}`, section);
      const blockers = task.dependency_blockers || [];
      if (blockers.length) text('p', '等待：' + blockers.map(dep => `${dep.id ? '#' + dep.id : ''} ${dep.title}（${label(dep.status)}）`).join('、'), section);
      const button = text('button', '查看任务记录', row);
      button.style.cssText = 'flex-shrink:0;white-space:nowrap';
      button.type = 'button';
      button.addEventListener('click', () => openTaskHistory(task.id, req.project));
    }
  }
  async function open(id, project) {
    current = {id, project};
    const request = ++revision;
    title.textContent = `需求 #${id}`;
    content.replaceChildren();
    text('p', '加载中…');
    if (!dialog.open) dialog.showModal();
    try {
      const data = await api(`/requirements/${encodeURIComponent(id)}?project=${encodeURIComponent(project)}`);
      if (request === revision && dialog.open) render(data);
    } catch (error) {
      if (request === revision && dialog.open) {
        content.replaceChildren();
        text('p', `加载失败：${error.message || error}`);
      }
    }
  }
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
  dialog.querySelector('[data-refresh]').addEventListener('click', () => open(current.id, current.project));
  dialog.addEventListener('close', () => { revision++; });
  window.openRequirementDetails = open;
})();
