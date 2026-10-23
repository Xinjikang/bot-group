/* Display names are server-owned presentation data; routing keeps the user profile. */
let userNameRequest = 0;
let userNameSaving = false;

function applyUserDisplayName(data) {
  if (data.profile !== 'user' || typeof data.display_name !== 'string' || !data.display_name.trim())
    throw new Error('称呼读取失败，请确认引擎已更新并重启');
  const changed = S.userDisplayName !== data.display_name;
  S.userDisplayName = data.display_name;
  document.querySelector('#btn-user-name').title = `我的称呼：${data.display_name}`;
  if (changed) {
    renderMessages();
    renderThreads();
    document.querySelectorAll('.task-event-actor[data-profile="user"]').forEach(node => {
      node.textContent = data.display_name;
    });
  }
}

async function loadUserDisplayName() {
  const request = ++userNameRequest, token = S.token;
  const data = await api('/user/display-name', {cache: 'no-store'});
  if (request !== userNameRequest || token !== S.token || !S.isUserIdentity) return null;
  applyUserDisplayName(data);
  return data;
}

async function refreshUserDisplayName() {
  if (!S.isUserIdentity || userNameSaving || document.querySelector('#dlg-user-name').open) return;
  try { await loadUserDisplayName(); }
  catch (error) { document.querySelector('#btn-user-name').title = `称呼读取失败：${error.message}`; }
}

document.addEventListener('DOMContentLoaded', () => {
  const button = document.querySelector('#btn-user-name');
  const dialog = document.querySelector('#dlg-user-name');
  const input = document.querySelector('#user-name-input');
  const save = document.querySelector('#user-name-save');
  const status = document.querySelector('#user-name-status');
  const report = (message, error = false) => {
    status.textContent = message;
    status.dataset.error = String(error);
    status.setAttribute('role', error ? 'alert' : 'status');
  };
  function updateVisibility() {
    button.hidden = !S.isUserIdentity || !document.querySelector('#login').hidden;
    if (button.hidden) {
      ++userNameRequest;
      closeDialogAnchored(dialog);
      S.userDisplayName = '用户';
    }
  }
  button.addEventListener('click', async () => {
    if (dialog.open || userNameSaving) return;
    input.value = '';
    input.disabled = save.disabled = true;
    report('正在读取称呼…');
    openDialogAnchored(dialog, button);
    try {
      const data = await loadUserDisplayName();
      if (!data || !dialog.open) return;
      input.value = data.display_name;
      input.disabled = save.disabled = false;
      report('');
      input.focus();
      input.select();
    } catch (error) { report(`读取失败：${error.message}。请关闭后重试。`, true); }
  });
  document.querySelector('#user-name-cancel').onclick = () => closeDialogAnchored(dialog);
  dialog.addEventListener('close', () => {
    if (dialog.__outsideCloser) document.removeEventListener('click', dialog.__outsideCloser, true);
  });
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); closeDialogAnchored(dialog); }
    if (event.key === 'Enter' && event.isComposing) event.preventDefault();
  });
  document.querySelector('#user-name-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled || userNameSaving || !S.isUserIdentity) return;
    const name = input.value.trim();
    if (!name || name.length > 200 || /[\x00-\x1f\x7f-\x9f\u2028\u2029]/.test(input.value)) {
      report('请输入 1–200 个字符的单行称呼。', true);
      input.focus();
      return;
    }
    userNameSaving = true;
    const request = ++userNameRequest, token = S.token;
    input.disabled = save.disabled = true;
    report('正在保存…');
    try {
      const data = await api('/user/display-name', {method: 'PATCH', body: JSON.stringify({display_name: name})});
      if (request !== userNameRequest || token !== S.token || !S.isUserIdentity) return;
      applyUserDisplayName(data);
      closeDialogAnchored(dialog);
    } catch (error) { report(`保存失败：${error.message}。输入已保留，可以重试。`, true); }
    finally {
      userNameSaving = false;
      input.disabled = save.disabled = false;
    }
  });
  document.addEventListener('service-auth-changed', updateVisibility);
  updateVisibility();
});
