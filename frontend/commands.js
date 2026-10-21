// Command selection only inserts text; execution uses the existing /msg path.
let commandCatalog = null, commandCatalogPending = null;
const commandPicker = {target: null, items: [], selected: 0, version: 0, threadId: null};

async function loadCommandCatalog() {
  if (commandCatalog) return commandCatalog;
  if (!commandCatalogPending) {
    commandCatalogPending = api('/commands').then(data => {
      commandCatalog = data.commands;
      return commandCatalog;
    }).finally(() => { commandCatalogPending = null; });
  }
  return commandCatalogPending;
}

function hideCommandPicker() {
  commandPicker.version++;
  commandPicker.target = null;
  commandPicker.items = [];
  $('#command-pop').hidden = true;
}

function renderCommandPicker(message = '') {
  const pop = $('#command-pop'), ta = commandPicker.target;
  if (!ta?.isConnected) { hideCommandPicker(); return; }
  pop.innerHTML = message ? `<div class="mp-item" role="status">${esc(message)}</div>`
    : commandPicker.items.map((item, i) => `<div class="mp-item ${i === commandPicker.selected ? 'sel' : ''}"
        role="option" aria-selected="${i === commandPicker.selected}" data-index="${i}">
        <strong>${esc(item.name)}</strong><small>${esc(item.description)}</small></div>`).join('');
  pop.hidden = false;
  const rect = ta.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - (pop.offsetWidth || 320) - 8)) + 'px';
  const height = pop.offsetHeight;
  pop.style.top = Math.max(8, rect.bottom + height + 8 > window.innerHeight
    ? rect.top - height - 6 : rect.bottom + 6) + 'px';
  pop.querySelectorAll('[data-index]').forEach(el => el.addEventListener('mousedown', e => {
    e.preventDefault();
    selectCommand(Number(el.dataset.index));
  }));
}

async function detectCommand(ta, threadId) {
  hideCommandPicker();
  if (!/^\s*\/[a-z]*$/i.test(ta.value) || ta.selectionStart !== ta.value.length
      || ta.selectionEnd !== ta.selectionStart) return;
  const version = commandPicker.version, text = ta.value;
  commandPicker.target = ta;
  commandPicker.threadId = threadId;
  commandPicker.selected = 0;
  mpHide();
  renderCommandPicker('正在读取命令…');
  try {
    const catalog = await loadCommandCatalog();
    if (version !== commandPicker.version || !ta.isConnected || ta.value !== text) return;
    commandPicker.items = catalog.filter(item => item.name.startsWith(text.trim().toLowerCase()));
    renderCommandPicker(commandPicker.items.length ? '' : '没有匹配的命令');
  } catch (err) {
    if (version === commandPicker.version) renderCommandPicker('命令读取失败，请重新输入 /');
  }
}

function selectCommand(index) {
  const {target: ta, items, threadId} = commandPicker;
  const item = items[index];
  if (!item || !ta?.isConnected) return;
  hideCommandPicker();
  ta.value = item.name + (item.requires_target ? ' @' : ' ');
  ta.selectionStart = ta.selectionEnd = ta.value.length;
  ta.focus();
  mpDetect(ta, threadId);
}

function bindCommandInput(ta, threadId) {
  ta.addEventListener('input', e => { if (!e.isComposing) detectCommand(ta, threadId()); });
  ta.addEventListener('focus', () => detectCommand(ta, threadId()));
  ta.addEventListener('blur', () => { if (commandPicker.target === ta) hideCommandPicker(); });
  // Capture before either main-composer Enter or inline-topic Ctrl+Enter sends.
  ta.addEventListener('keydown', e => {
    if (commandPicker.target !== ta || e.isComposing
        || !['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(e.key)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.key === 'Escape') { hideCommandPicker(); return; }
    if (!commandPicker.items.length) return;
    if (e.key === 'Enter' || e.key === 'Tab') selectCommand(commandPicker.selected);
    else {
      commandPicker.selected = (commandPicker.selected + (e.key === 'ArrowDown' ? 1 : -1)
        + commandPicker.items.length) % commandPicker.items.length;
      renderCommandPicker();
    }
  }, true);
}
