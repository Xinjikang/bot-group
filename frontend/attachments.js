/* Durable chat attachments; drafts are isolated by project and topic. */
const imageDrafts = new Map();
const imageSends = new Set();
const imageEditors = new Set();
function refreshImageEditors() {
  for (const editor of imageEditors) {
    if (editor.container.isConnected) editor.refresh();
    else imageEditors.delete(editor);
  }
}
function imageDestinationKey(destination) {
  if (destination.draft) return JSON.stringify([destination.project, destination.draft]);
  return JSON.stringify([destination.project, destination.thread_id || null]);
}

async function uploadImageDraft(project, draft) {
  const ids = [];
  for (const item of draft) {
    if (!item.id) {
      const uploaded = await api('/attachments?' + new URLSearchParams({project, filename: item.file.name}), {
        method: 'POST', headers: {'Content-Type': item.file.type}, body: item.file
      });
      item.id = uploaded.id;
    }
    ids.push(item.id);
  }
  return ids;
}
function createImageComposer(container, textarea, destination) {
  const toolbar = document.createElement('div');
  toolbar.className = 'image-toolbar';
  toolbar.innerHTML = `<button type="button" class="image-add" title="${window.t('att_add_image')}" aria-label="${window.t('att_add_image')}">＋</button><input type="file" accept="image/png,image/jpeg,image/webp" multiple hidden><div class="image-drafts"></div>`;
  container.appendChild(toolbar);
  const addButton = toolbar.querySelector('button');
  const inputWrap = document.createElement('div');
  inputWrap.className = 'image-input-wrap';
  textarea.before(inputWrap);
  inputWrap.append(textarea, addButton);
  const input = toolbar.querySelector('input');
  const previews = toolbar.querySelector('.image-drafts');
  const current = () => {
    const key = imageDestinationKey(destination());
    if (!imageDrafts.has(key)) imageDrafts.set(key, []);
    return imageDrafts.get(key);
  };
  function refresh() {
    renderCommandReceipt();
    if (container.id === 'composer') updateComposerState();
    previews.replaceChildren();
    const key = imageDestinationKey(destination());
    addButton.disabled = imageSends.has(key);
    addButton.title = imageSends.has(key) ? window.t('att_sending') : window.t('att_add_image');
    addButton.setAttribute('aria-label', addButton.title);
    toolbar.hidden = current().length === 0;
    current().forEach(item => {
      const chip = document.createElement('span');
      const img = document.createElement('img');
      img.src = item.url;
      img.alt = item.file.name;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.title = window.t('att_remove_image');
      remove.onclick = e => {
        e.stopPropagation();
        if (imageSends.has(key)) return;
        const draft = imageDrafts.get(key);
        draft.splice(draft.indexOf(item), 1);
        URL.revokeObjectURL(item.url);
        refreshImageEditors();
      };
      chip.append(img, remove);
      previews.append(chip);
    });
  }
  function add(files) {
    const dest = destination();
    if (!dest.project) return showAlert(window.t('att_select_project_first'));
    if (imageSends.has(imageDestinationKey(dest))) return;
    const selected = [...files];
    if (current().length + selected.length > 4) return showAlert(window.t('att_max_four'));
    if (selected.some(f => !['image/png','image/jpeg','image/webp'].includes(f.type) || f.size > 10 * 1024 * 1024)) {
      return showAlert(window.t('att_type_limit'));
    }
    current().push(...selected.map(file => ({file, url: URL.createObjectURL(file)})));
    refreshImageEditors();
  }
  toolbar.addEventListener('click', e => e.stopPropagation());
  addButton.onclick = e => { e.stopPropagation(); input.click(); };
  input.onchange = () => { add(input.files); input.value = ''; };
  textarea.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); add(files); }
  });
  container.addEventListener('dragover', e => {
    if ([...e.dataTransfer.types].includes('Files')) e.preventDefault();
  });
  container.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) { e.preventDefault(); e.stopPropagation(); add(e.dataTransfer.files); }
  });
  imageEditors.add({container, refresh});
  refresh();
  return {refresh};
}

const commandReceipts = new Map();
function renderCommandReceipt() {
  const box = document.querySelector('#command-receipt');
  if (!box) return;
  const receipt = commandReceipts.get(S.currentProject);
  box.hidden = !receipt;
  if (!receipt) return;
  box.dataset.state = receipt.state;
  box.querySelector('.command-receipt-text').textContent = receipt.text;
  box.querySelector('button').onclick = () => {
    commandReceipts.delete(S.currentProject);
    renderCommandReceipt();
  };
}
function setCommandReceipt(body, text, state) {
  commandReceipts.set(body.project, {text, state});
  renderCommandReceipt();
}

async function sendImageMessage(body) {
  const key = imageDestinationKey(body);
  const draft = imageDrafts.get(key) || [];
  if ((!body.text && !draft.length) || imageSends.has(key)) return false;
  imageSends.add(key);
  const text = body.text?.trim() || '';
  const isCommand = text.startsWith('/') && !/^\/steer(?:\s|$)/.test(text);
  let commandResultReceived = false;
  if (isCommand) setCommandReceipt(body, window.t('att_cmd_running'), 'pending');
  refreshImageEditors();
  try {
    const ids = [];
    for (const item of draft) {
      if (!item.id) {
        const uploaded = await api('/attachments?' + new URLSearchParams({project: body.project, filename: item.file.name}), {
          method: 'POST', headers: {'Content-Type': item.file.type}, body: item.file
        });
        item.id = uploaded.id;
      }
      ids.push(item.id);
    }
    const result = await api('/msg', {method: 'POST', body: JSON.stringify({...body, attachment_ids: ids})});
    if (result.handled && !(result.command === '/steer' && result.message_id)) {
      commandResultReceived = true;
      setCommandReceipt(body, result.detail || `${result.command} ${result.ok ? window.t('att_cmd_done') : window.t('att_cmd_failed')}`,
        result.ok ? 'success' : 'error');
      if (!result.ok) throw new Error(result.detail || window.t('att_cmd_failed_msg'));
      await showAlert(result.detail || window.t('att_cmd_completed', [result.command]));
    }
    draft.forEach(item => URL.revokeObjectURL(item.url));
    imageDrafts.delete(key);
    return true;
  } catch (err) {
    if (isCommand && !commandResultReceived) setCommandReceipt(body, err.message, 'error');
    throw err;
  } finally {
    imageSends.delete(key);
    refreshImageEditors();
  }
}

const displayedImageUrls = new Map();
// Keep decoded image elements through synchronous message-list redraws.
const chatImageNodes = new Map();
const pendingChatImages = new Set();
const chatImageObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    chatImageObserver.unobserve(entry.target);
    pendingChatImages.delete(entry.target);
    loadChatImage(entry.target);
  }
}, {rootMargin: '300px'});
async function loadChatImage(img) {
  try {
    const response = await fetch('/attachments/' + encodeURIComponent(img.dataset.attachmentId), {
      headers: S.token ? {Authorization: 'Bearer ' + S.token} : {}
    });
    if (!response.ok) throw new Error(window.t('att_image_load_failed', [response.status]));
    const blob = await response.blob();
    if (!img.isConnected) return;
    const url = URL.createObjectURL(blob);
    displayedImageUrls.set(img, url);
    img.src = url;
    img.onclick = () => {
      const dialog = document.createElement('dialog');
      dialog.className = 'image-preview';
      const original = document.createElement('img');
      original.src = URL.createObjectURL(blob);
      const close = document.createElement('button');
      close.textContent = window.t('att_close');
      close.onclick = () => dialog.close();
      dialog.append(close, original);
      document.body.appendChild(dialog);
      dialog.addEventListener('close', () => { URL.revokeObjectURL(original.src); dialog.remove(); });
      dialog.showModal();
    };
  } catch (error) {
    img.alt = window.t('att_click_retry', [error.message]);
    img.onclick = () => loadChatImage(img);
  }
}
new MutationObserver(() => {
  for (const [id, node] of chatImageNodes) {
    if (!node.isConnected) chatImageNodes.delete(id);
  }
  for (const node of pendingChatImages) {
    if (!node.isConnected) { chatImageObserver.unobserve(node); pendingChatImages.delete(node); }
  }
  for (const [node, url] of displayedImageUrls) {
    if (!node.isConnected) { URL.revokeObjectURL(url); displayedImageUrls.delete(node); }
  }
}).observe(document.documentElement, {childList: true, subtree: true});
function renderChatImages(container, attachments) {
  for (const item of attachments || []) {
    const existing = chatImageNodes.get(item.id);
    if (existing) {
      if (existing.parentNode !== container) container.appendChild(existing);
      continue;
    }
    const img = document.createElement('img');
    img.className = 'chat-image';
    img.alt = item.filename;
    img.title = item.filename + ' · ' + window.t('att_view_original');
    img.width = item.width;
    img.height = item.height;
    img.style.width = Math.min(item.width, 420, 320 * item.width / item.height) + 'px';
    img.style.aspectRatio = item.width + ' / ' + item.height;
    img.dataset.attachmentId = item.id;
    chatImageNodes.set(item.id, img);
    container.appendChild(img);
    pendingChatImages.add(img);
    chatImageObserver.observe(img);
  }
}
