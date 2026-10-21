// The navigation queue is local to this browser and independent of bot inboxes.
(() => {
  const box = document.querySelector('#msg-list');
  const row = document.createElement('div');
  row.id = 'message-navigation';
  row.setAttribute('aria-label', '消息导航');
  row.innerHTML = '<button type="button" id="message-bottom" hidden>回到底部</button>'
    + '<button type="button" id="message-mentions" hidden></button>';
  box.after(row);
  const bottom = row.querySelector('#message-bottom');
  const mentions = row.querySelector('#message-mentions');
  const states = new Map();
  let navigating = false;
  let readTimer = null;
  let visibleSince = new Map();
  const storagePrefix = 'ge_mentions_v1:';
  const storageNames = ['localStorage', 'sessionStorage'];

  function savedState(storageName, project) {
    try {
      const saved = JSON.parse(globalThis[storageName].getItem(storagePrefix + project));
      if (saved && Number.isSafeInteger(saved.cursor) && saved.cursor >= 0
          && Array.isArray(saved.pending)) return saved;
    } catch {}
    return null;
  }

  function mergeSaved(state, saved) {
    if (!saved) return;
    const pending = new Map(saved.pending.filter(m => m && Number.isSafeInteger(m.id)
      && m.id > 0 && m.id <= saved.cursor).map(m => [m.id, m]));
    const cursor = state.cursor;
    // Below a page's scan cursor, absence from its queue means acknowledged.
    // Merge acknowledgements while retaining unread IDs newer than either scan.
    for (const id of state.pending.keys()) {
      if (id <= saved.cursor && !pending.has(id)) state.pending.delete(id);
    }
    for (const [id, message] of pending) {
      if (cursor === null || id > cursor) state.pending.set(id, message);
    }
    state.cursor = Math.max(cursor || 0, saved.cursor);
  }

  function visibleMentions() {
    if (!S.messagesReady || document.visibilityState !== 'visible' || !document.hasFocus()
        || document.querySelector('dialog[open]')) return [];
    const bounds = box.getBoundingClientRect();
    if (!box.getClientRects().length || bounds.height <= 0) return [];
    return pendingForView().filter(message => {
      const node = box.querySelector(`[data-message-id="${message.id}"]`);
      if (!node || !node.getClientRects().length) return false;
      const rect = node.getBoundingClientRect();
      const visible = Math.min(rect.bottom, bounds.bottom) - Math.max(rect.top, bounds.top);
      return rect.width > 0 && rect.height > 0
        && visible >= Math.min(rect.height, bounds.height) * .5;
    });
  }

  function readVisible() {
    clearTimeout(readTimer);
    const now = performance.now();
    const next = new Map();
    const state = S.currentProject ? stateFor(S.currentProject) : null;
    let changed = false;
    for (const message of visibleMentions()) {
      const since = visibleSince.get(message.id) ?? now;
      if (now - since >= 1000) {
        state.pending.delete(message.id);
        changed = true;
      } else next.set(message.id, since);
    }
    visibleSince = next;
    if (changed) {
      save(S.currentProject, state);
      update();
    }
    if (next.size) readTimer = setTimeout(readVisible, 1050);
  }

  function stateFor(project) {
    if (!states.has(project)) {
      const state = {cursor: null, pending: new Map(), request: null};
      for (const storageName of storageNames) mergeSaved(state, savedState(storageName, project));
      states.set(project, state);
    }
    return states.get(project);
  }

  function save(project, state) {
    if (state.cursor === null) return;
    for (const storageName of storageNames) mergeSaved(state, savedState(storageName, project));
    const value = JSON.stringify({cursor: state.cursor,
      pending: [...state.pending.values()].sort((a, b) => a.id - b.id)});
    // A per-tab backup survives reload even when shared localStorage is full.
    for (const storageName of storageNames) {
      try {
        const storage = globalThis[storageName];
        if (storage.getItem(storagePrefix + project) !== value) storage.setItem(storagePrefix + project, value);
      } catch (error) { console.warn('Mention navigation storage unavailable:', storageName, error); }
    }
  }

  function isMention(message) {
    let recipients;
    try { recipients = typeof message.mentions === 'string'
      ? JSON.parse(message.mentions) : message.mentions; } catch { return false; }
    return message.sender !== 'user' && message.sender_type === 'bot'
      && !message.retract_by && Array.isArray(recipients) && recipients.includes('user');
  }

  function pendingForView() {
    if (!S.currentProject) return [];
    return [...stateFor(S.currentProject).pending.values()]
      .filter(m => S.currentThread == null || m.thread_id === S.currentThread)
      .sort((a, b) => a.id - b.id);
  }

  function update() {
    bottom.hidden = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const count = pendingForView().length;
    mentions.hidden = count === 0;
    mentions.disabled = navigating;
    mentions.textContent = `有人 @你 · ${count}`;
    mentions.title = `还有 ${count} 条 @你的消息，点击查看下一条`;
    mentions.setAttribute('aria-label', mentions.title);
    readVisible();
  }

  // Reconcile retractions/edits from the ordinary timeline without re-adding
  // acknowledgements when the same topic is polled again.
  function observe(messages) {
    const project = S.currentProject;
    if (!project) return;
    const state = stateFor(project);
    for (const message of messages) {
      if (message.project === project && !isMention(message)) state.pending.delete(message.id);
    }
    if (state.cursor !== null) save(project, state);
    update();
  }

  async function refresh() {
    const project = S.currentProject;
    if (!project) return;
    const state = stateFor(project);
    if (state.request) return state.request;
    state.request = (async () => {
      try {
        // First use starts with the latest window. Thereafter the persisted
        // cursor catches every new message, including those received offline.
        let more;
        do {
          const initial = state.cursor === null;
          const query = initial ? 'limit=100' : state.cursor === 0 ? 'since_id=0'
            : `since_id=${state.cursor}&limit=500`;
          const result = await api(`/messages?${query}&project=${encodeURIComponent(project)}`);
          for (const message of result.messages || []) {
            if (message.project !== project) continue;
            if ((initial || message.id > state.cursor) && isMention(message)) {
              state.pending.set(message.id, {id: message.id, thread_id: message.thread_id});
            }
          }
          const previous = state.cursor;
          state.cursor = (result.messages || []).reduce((id, m) => Math.max(id, m.id), state.cursor || 0);
          more = !initial && result.has_more === true && state.cursor > (previous || 0);
          save(project, state);
          if (S.currentProject === project) update();
        } while (more);
      } catch (error) { console.warn('Mention navigation refresh failed:', error); }
      finally { state.request = null; }
    })();
    return state.request;
  }

  async function nextMention() {
    if (navigating) return;
    const target = pendingForView()[0];
    if (!target) return;
    const project = S.currentProject;
    const state = stateFor(project);
    let view = S.messageView;
    const current = () => S.currentProject === project && S.messageView === view;
    navigating = true;
    update();
    try {
      // Recheck the target before acknowledging: it may have been retracted
      // while this browser was closed or while its topic was off screen.
      const result = await api(`/messages?since_id=${target.id - 1}&before_id=${target.id + 1}`
        + `&limit=1&project=${encodeURIComponent(project)}`);
      if (!current()) return;
      const message = (result.messages || []).find(m => m.id === target.id);
      if (!message || !isMention(message)) {
        state.pending.delete(target.id);
        save(project, state);
        return;
      }
      if (target.thread_id != null && !S.currentThread
          && !S.threads.some(t => t.id === target.thread_id && !t.hidden)) {
        // Hidden topics can still be opened directly without changing their
        // persisted visibility setting.
        const selecting = selectThread(target.thread_id);
        view = S.messageView;
        await selecting;
        if (!current()) return;
      }
      if (target.id > (S.msgs.at(-1)?.id || 0)) {
        await refreshMessages();
        if (!current()) return;
      }
      // Keep the main timeline continuous while locating older persisted IDs.
      while (!S.msgs.some(m => m.id === target.id) && S.hasOlderMessages && !S.currentThread) {
        if (S.historyRequest === view) {
          await new Promise(resolve => setTimeout(resolve, 50));
        } else {
          const first = S.msgs[0]?.id;
          await loadOlderMessages();
          if (current() && S.msgs[0]?.id === first) break;
        }
        if (!current()) return;
      }
      if (!current()) return;
      S.collapsedThreads.delete(target.thread_id);
      renderMessages();
      const node = box.querySelector(`[data-message-id="${target.id}"]`);
      if (!node || !node.getClientRects().length) {
        throw new Error('消息暂未加载或话题已隐藏，请展开对应话题后重试');
      }
      box.scrollTop += node.getBoundingClientRect().top - box.getBoundingClientRect().top
        - Math.max(0, (box.clientHeight - node.offsetHeight) / 2);
      node.animate([{outline: '2px solid var(--cyan)'}, {outline: '2px solid transparent'}],
        {duration: 1200});
      state.pending.delete(target.id);
      save(project, state);
    } catch (error) {
      if (current()) showAlert('定位 @消息失败：' + error.message);
    } finally {
      navigating = false;
      update();
    }
  }

  bottom.addEventListener('click', () => { box.scrollTop = box.scrollHeight; update(); });
  mentions.addEventListener('click', nextMention);
  box.addEventListener('scroll', update, {passive: true});
  new ResizeObserver(update).observe(box);
  // Topic refreshes and folding may change visibility without scrolling.
  new MutationObserver(update).observe(box, {childList: true, subtree: true, attributes: true});
  document.addEventListener('visibilitychange', update);
  window.addEventListener('focus', update);
  window.addEventListener('blur', () => { visibleSince.clear(); clearTimeout(readTimer); });
  window.addEventListener('storage', event => {
    if (!event.key?.startsWith(storagePrefix)) return;
    const project = event.key.slice(storagePrefix.length);
    const state = states.get(project);
    if (!state) return;
    save(project, state);
    if (S.currentProject === project) update();
  });
  globalThis.messageNavigation = {
    observe, refresh, update,
    reset() {
      visibleSince.clear(); clearTimeout(readTimer);
      bottom.hidden = true; mentions.hidden = true;
    },
  };
  update();
})();
