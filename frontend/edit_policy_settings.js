/* Global Hermes edit policy is read from the server whenever the dialog opens. */
(() => {
  const button = document.querySelector('#btn-edit-policy');
  const anchor = document.querySelector('#btn-settings');
  const dialog = document.querySelector('#dlg-edit-policy');
  const form = document.querySelector('#edit-policy-form');
  const fields = document.querySelector('#edit-policy-options');
  const save = document.querySelector('#edit-policy-save');
  const status = document.querySelector('#edit-policy-status');
  const allowed = () => S.isUserIdentity && document.querySelector('#login').hidden;
  let epoch = 0, saving = false;
  const reposition = () => { if (dialog.open) anchorDialog(dialog, anchor); };
  new ResizeObserver(reposition).observe(dialog);
  window.addEventListener('resize', reposition);
  const report = (text, error = false) => {
    status.textContent = text;
    status.dataset.error = String(error);
    status.setAttribute('role', error ? 'alert' : 'status');
  };
  function apply(data) {
    const input = [...form.elements.policy].find(input => input.value === data.policy);
    if (!input) throw new Error(window.t('ep_read_failed'));
    input.checked = true;
  }
  function visibility() {
    button.hidden = !allowed();
    if (button.hidden) {
      epoch++;
      if (dialog.open) closeDialogAnchored(dialog);
    }
  }
  button.addEventListener('click', async () => {
    if (!allowed() || dialog.open || saving) return;
    const request = ++epoch;
    for (const input of form.elements.policy) input.checked = false;
    fields.disabled = save.disabled = true;
    report(window.t('ep_loading'));
    openDialogAnchored(dialog, anchor);
    try {
      const data = await api('/system/edit-approval-policy', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (request !== epoch || !dialog.open || !allowed()) return;
      apply(data);
      fields.disabled = save.disabled = false;
      report('');
      form.querySelector('input:checked').focus();
    } catch (error) {
      if (request === epoch && dialog.open) report(window.t('ep_read_failed_retry', [error.message]), true);
    }
  });
  document.querySelector('#edit-policy-cancel').onclick = () => closeDialogAnchored(dialog);
  dialog.addEventListener('close', () => {
    epoch++;
    if (dialog.__outsideCloser) document.removeEventListener('click', dialog.__outsideCloser, true);
    anchor.focus({preventScroll: true});
  });
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); closeDialogAnchored(dialog); }
  });
  fields.addEventListener('change', () => report(''));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!allowed() || saving || save.disabled) return;
    const policy = form.elements.policy.value, request = ++epoch;
    saving = true;
    fields.disabled = save.disabled = true;
    report(window.t('ep_saving'));
    try {
      const data = await api('/system/edit-approval-policy', {
        method: 'PATCH', body: JSON.stringify({policy}), signal: AbortSignal.timeout(10000),
      });
      if (request !== epoch || !dialog.open || !allowed()) return;
      apply(data);
      report(window.t('ep_saved'));
    } catch (error) {
      if (request === epoch && dialog.open)
        report(window.t('ep_save_unconfirmed', [error.message]), true);
    } finally {
      saving = false;
      if (request === epoch) fields.disabled = save.disabled = false;
    }
  });
  document.addEventListener('service-auth-changed', visibility);
  visibility();
})();
