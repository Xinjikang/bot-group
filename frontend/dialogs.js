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
