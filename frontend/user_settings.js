/* Display names are server-owned presentation data; routing keeps the user profile. */
let userNameRequest = 0;
let userNameSaving = false;

function applyUserDisplayName(data) {
  if (data.profile !== 'user' || typeof data.display_name !== 'string' || !data.display_name.trim())
    throw new Error(window.t('name_load_failed'));
  const changed = S.userDisplayName !== data.display_name;
  S.userDisplayName = data.display_name;
  document.querySelector('#topbar-user-name').textContent = data.display_name;
  document.querySelector('#topbar-user-name').title = data.display_name;
  document.querySelector('#btn-user-name').title = `${window.t('my_name_title')}${data.display_name}`;
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
  catch (error) { document.querySelector('#btn-user-name').title = `${window.t('name_read_failed')}${error.message}`; }
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
    document.querySelector('#topbar-user-name').hidden = button.hidden;
    if (button.hidden) {
      ++userNameRequest;
      closeDialogAnchored(dialog);
      S.userDisplayName = window.t('user');
      button.title = window.t('my_display_name');
    }
  }
  button.addEventListener('click', async () => {
    if (dialog.open || userNameSaving) return;
    input.value = '';
    input.disabled = save.disabled = true;
    report(window.t('name_reading'));
    openDialogAnchored(dialog, button);
    try {
      const data = await loadUserDisplayName();
      if (!data || !dialog.open) return;
      input.value = data.display_name;
      input.disabled = save.disabled = false;
      report('');
      input.focus();
      input.select();
    } catch (error) { report(`${window.t('name_read_failed')}${error.message}${window.t('name_retry')}`, true); }
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
      report(window.t('name_invalid'), true);
      input.focus();
      return;
    }
    userNameSaving = true;
    const request = ++userNameRequest, token = S.token;
    input.disabled = save.disabled = true;
    report(window.t('name_saving'));
    try {
      const data = await api('/user/display-name', {method: 'PATCH', body: JSON.stringify({display_name: name})});
      if (request !== userNameRequest || token !== S.token || !S.isUserIdentity) return;
      applyUserDisplayName(data);
      closeDialogAnchored(dialog);
    } catch (error) { report(`${window.t('name_save_failed')}${error.message}${window.t('name_saved_retry')}`, true); }
    finally {
      userNameSaving = false;
      input.disabled = save.disabled = false;
    }
  });
  document.addEventListener('service-auth-changed', updateVisibility);
  document.addEventListener('ui-language-changed', () => {
    // 按钮 title 是动态文案（成功=称呼、失败=错误前缀）：按当前语言重设。
    updateVisibility();
    refreshUserDisplayName();
  });
  updateVisibility();
});
