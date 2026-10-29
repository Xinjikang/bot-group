/* User decisions for native Hermes approvals; all execution scope comes from the server. */
(() => {
  const panel = document.querySelector('#hermes-approvals');
  const labels = {once: '仅允许这一次', deny: '拒绝', session: '本会话允许', always: '长期允许'};
  let items = [], context = '', request = null, next = 0, signature = '', error = '';
  const busy = new Set();
  new ResizeObserver(entries => {
    panel.classList.toggle('approval-floating', entries[0].contentRect.width < 320);
  }).observe(panel.parentElement);
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;
  const scope = () => JSON.stringify([S.currentProject || null, S.currentThread || null]);
  const key = item => `${item.execution_id}:${item.generation}:${item.prompt_id}`;
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function render() {
    const current = JSON.stringify([items, [...busy], error]);
    if (current === signature) return;
    signature = current;
    panel.replaceChildren();
    panel.hidden = !allowed() || !items.length;
    if (panel.hidden) return;
    panel.append(node('strong', '需要你授权'));
    if (error) panel.append(node('p', error, 'approval-error'));
    for (const item of items) {
      const card = node('article', null, 'approval-card');
      const member = typeof memberLabelForProfile === 'function' ? memberLabelForProfile(item.profile, S.members || []) : item.profile;
      const topic = S.threads.find(t => t.id === item.thread_id);
      card.append(node('div', `${member} · ${item.project} · ${item.thread_id ? (topic?.title || '话题 ' + item.thread_id) : '项目任务'}`, 'approval-owner'));
      if (item.description) card.append(node('p', item.description));
      card.append(node('pre', item.command || 'Hermes 请求确认本次操作', 'approval-command'));
      if (item.edit) {
        const details = node('details');
        details.append(node('summary', '查看编辑内容'), node('pre', JSON.stringify(item.edit, null, 2)));
        card.append(details);
      }
      const working = busy.has(key(item));
      if (item.error || working) card.append(node('p', working ? '正在提交你的选择…' : item.error, 'approval-status'));
      const actions = node('div', null, 'approval-actions');
      for (const choice of item.choices) {
        if (!labels[choice]) continue;
        const button = node('button', labels[choice]);
        button.type = 'button';
        button.disabled = working || !!item.error || !!error;
        button.onclick = () => decide(item, choice);
        actions.append(button);
      }
      card.append(actions);
      panel.append(card);
    }
  }
  async function decide(item, choice) {
    const id = key(item), originalContext = scope();
    if (busy.has(id) || !allowed()) return;
    busy.add(id); render();
    try {
      if (choice === 'session' || choice === 'always') {
        const extent = choice === 'session' ? '本会话中符合 Hermes 授权规则的后续操作' : '今后符合 Hermes 持久授权规则的操作';
        if (!await showConfirm(`允许${extent}自动执行？\n只确认当前操作请选“仅允许这一次”。`)) return;
      }
      if (!allowed() || scope() !== originalContext) return;
      const result = await api('/hermes-approvals/' + encodeURIComponent(item.execution_id), {
        method: 'POST', body: JSON.stringify({prompt_id: item.prompt_id, generation: item.generation, choice}),
        signal: AbortSignal.timeout(25000)});
      if (scope() === originalContext) {
        items = items.filter(p => key(p) !== id);
        if (result.status === 'already_resolved') await showAlert('这条授权已在其他窗口处理，状态已更新。');
      }
    } catch (failure) {
      if (allowed() && scope() === originalContext) {
        error = failure.status ? failure.message : '授权结果暂未确认，正在重新查询，请勿重复提交。';
        render();
        await showAlert(error);
      }
    } finally {
      busy.delete(id); next = 0; render();
      refresh();
    }
  }
  async function refresh() {
    const current = scope();
    if (!allowed()) {
      context = ''; items = []; error = ''; render(); return;
    }
    if (current !== context) {
      context = current; items = []; error = ''; next = 0; render();
    }
    if (request || document.hidden || Date.now() < next) return;
    const params = new URLSearchParams();
    if (S.currentProject) params.set('project', S.currentProject);
    if (S.currentThread) params.set('thread_id', S.currentThread);
    request = current;
    try {
      const data = await api('/hermes-approvals?' + params, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (allowed() && current === scope()) {
        items = data.approvals || []; error = ''; render();
      }
    } catch {
      if (allowed() && current === scope()) {
        error = '授权状态查询失败，正在重试；也可到 Hermes 查看。'; render();
      }
    } finally {
      request = null; next = current === scope() ? Date.now() + 3000 : 0;
    }
  }
  for (const name of ['service-auth-changed', 'approval-context-changed', 'visibilitychange']) {
    document.addEventListener(name, () => { next = 0; refresh(); });
  }
  setInterval(refresh, 1000);
  refresh();
})();
