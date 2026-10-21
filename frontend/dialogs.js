/* Application notifications. Calls queue in order and resolve on dismissal. */
(() => {
  const pending = [];
  let dialog, message, button, cancel, input, title, active;

  function showNext() {
    if (active || !pending.length) return;
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.id = 'app-alert';
      dialog.setAttribute('role', 'alertdialog');
      dialog.setAttribute('aria-labelledby', 'app-alert-title');
      dialog.setAttribute('aria-describedby', 'app-alert-message');
      title = document.createElement('h3');
      title.id = 'app-alert-title';
      title.textContent = '提示';
      message = document.createElement('div');
      message.id = 'app-alert-message';
      input = document.createElement('input');
      input.setAttribute('aria-labelledby', 'app-alert-message');
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.isComposing) {
          event.preventDefault();
          dialog.close('confirm');
        }
      });
      const actions = document.createElement('div');
      actions.className = 'dlg-btns';
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'primary';
      button.textContent = '确定';
      button.autofocus = true;
      button.addEventListener('click', () => dialog.close('confirm'));
      cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = '取消';
      cancel.addEventListener('click', () => dialog.close('cancel'));
      actions.append(cancel, button);
      dialog.append(title, message, input, actions);
      dialog.addEventListener('cancel', event => {
        event.preventDefault();
        dialog.close('cancel');
      });
      dialog.addEventListener('close', () => {
        const finished = active;
        active = null;
        if (finished.focus?.isConnected) finished.focus.focus({preventScroll: true});
        finished.resolve(finished.kind === 'prompt'
          ? (dialog.returnValue === 'confirm' ? input.value : null)
          : finished.kind === 'confirm' ? dialog.returnValue === 'confirm' : undefined);
        showNext();
      });
      document.body.append(dialog);
    }
    active = pending.shift();
    active.focus = document.activeElement;
    message.textContent = active.message;
    title.textContent = active.kind === 'alert' ? '提示' : active.kind === 'confirm' ? '确认操作' : '请输入';
    dialog.setAttribute('role', active.kind === 'alert' ? 'alertdialog' : 'dialog');
    input.hidden = active.kind !== 'prompt';
    input.value = active.defaultValue;
    cancel.hidden = active.kind === 'alert';
    dialog.returnValue = '';
    dialog.showModal();
    (active.kind === 'prompt' ? input : button).focus();
  }

  const enqueue = (kind, message = '', defaultValue = '') => new Promise(resolve => {
    pending.push({kind, message: String(message), defaultValue: String(defaultValue), resolve});
    showNext();
  });
  window.showAlert = message => enqueue('alert', message);
  window.showConfirm = message => enqueue('confirm', message);
  window.showPrompt = (message, defaultValue) => enqueue('prompt', message, defaultValue);
})();

/* Field errors share one presentation across static and dynamically built dialogs. */
(() => {
  const errors = new WeakMap();
  let sequence = 0;
  const eligible = field => field instanceof HTMLElement && field.matches('input,textarea,select')
    && field.closest('dialog')?.open && !field.matches('[type=hidden],[type=radio],[type=checkbox],[type=file]');
  window.dialogFieldError = (field, message) => {
    let error = errors.get(field);
    if (message && !error) {
      error = document.createElement('p');
      error.id = `dialog-field-error-${++sequence}`;
      error.className = 'dialog-field-error';
      error.setAttribute('role', 'status');
      const parent = field.parentElement;
      const anchor = ['flex', 'inline-flex'].includes(getComputedStyle(parent).display) ? parent : field;
      anchor.after(error);
      errors.set(field, error);
    }
    if (message) {
      error.textContent = message;
      field.setAttribute('aria-invalid', 'true');
      const ids = new Set((field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
      ids.add(error.id);
      field.setAttribute('aria-describedby', [...ids].join(' '));
    } else if (error) {
      field.removeAttribute('aria-invalid');
      const ids = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== error.id);
      if (ids.length) field.setAttribute('aria-describedby', ids.join(' '));
      else field.removeAttribute('aria-describedby');
      error.remove(); errors.delete(field);
    }
    return !message;
  };
  window.validateDialogField = field => {
    if (!eligible(field) || field.disabled || field.closest('[hidden]')) return true;
    let message = field.dialogValidation?.(field.value) || '';
    if (!message && field.required && !field.value.trim()) message = '此项为必填，请填写内容。';
    if (!message && field.maxLength >= 0 && field.value.length > field.maxLength) message = `最多填写 ${field.maxLength} 个字符。`;
    if (!message && field.minLength > 0 && field.value && field.value.length < field.minLength) message = `至少填写 ${field.minLength} 个字符。`;
    if (!message && !field.validity.valid) {
      const validity = field.validity;
      if (validity.badInput) message = '请输入有效数字。';
      else if (validity.rangeUnderflow || validity.rangeOverflow) message = `请输入 ${field.min || '最小值'} 至 ${field.max || '最大值'} 范围内的数值。`;
      else if (validity.stepMismatch) message = field.step === '1' || !field.step ? '请输入整数。' : `请按 ${field.step} 的步长填写数值。`;
      else message = field.dataset.validationMessage || field.validationMessage;
    }
    return dialogFieldError(field, message);
  };
  for (const eventName of ['input', 'change', 'focusout', 'compositionend', 'invalid']) {
    document.addEventListener(eventName, event => {
      if (!event.isComposing && eligible(event.target)) validateDialogField(event.target);
    }, true);
  }
  document.addEventListener('close', event => {
    if (event.target instanceof HTMLDialogElement) {
      event.target.querySelectorAll('input,textarea,select').forEach(field => dialogFieldError(field, ''));
    }
  }, true);
})();
