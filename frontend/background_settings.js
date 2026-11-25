/* The local engine owns the background; the planet remains the default. */
(() => {
  const key = 'ge_viewport_background', limit = 10 * 1024 * 1024;
  const root = document.documentElement;
  let objectURL = null, busy = false, reloadPending = false;
  async function request(method = 'GET', body, migrate = false) {
    const headers = S.token ? {Authorization: 'Bearer ' + S.token} : {};
    if (migrate) headers['If-None-Match'] = '*';
    const response = await fetch('/appearance/background', {method, headers, body, cache: 'no-store'});
    if (!response.ok) {
      const info = await response.json().catch(() => ({}));
      const error = new Error(typeof info.detail === 'string' ? info.detail : window.t('background_server_error'));
      error.status = response.status;
      throw error;
    }
    return response;
  }
  function apply(blob) {
    const old = objectURL;
    objectURL = blob ? URL.createObjectURL(blob) : null;
    if (objectURL) {
      root.style.setProperty('--viewport-image', `url("${objectURL}")`);
      root.style.setProperty('--viewport-size', 'cover');
    } else {
      root.style.removeProperty('--viewport-image');
      root.style.removeProperty('--viewport-size');
    }
    if (old) URL.revokeObjectURL(old);
  }
  function removeLegacy() {
    try { localStorage.removeItem(key); } catch (_) { /* Server settings take precedence. */ }
  }
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-background';
  dialog.setAttribute('aria-labelledby', 'background-title');
  dialog.innerHTML = `<h3 id="background-title">${window.t("background_title")}</h3>
    <p class="profile-help">${window.t("background_hint")}</p>
    <p class="profile-help">${window.t("background_formats")}</p>
    <input id="background-file" type="file" accept="image/png,image/jpeg,image/webp" hidden>
    <p id="background-status" class="profile-help" role="status" aria-live="polite"></p>
    <div class="dlg-btns"><button type="button" id="background-select">${window.t("choose_image")}</button><button type="button" id="background-reset">恢复默认行星图</button><button type="button" id="background-close">关闭</button></div>`;
  document.body.append(dialog);
  const input = dialog.querySelector('input'), status = dialog.querySelector('#background-status');
  function lock(value) {
    busy = value;
    dialog.querySelectorAll('button').forEach(b => { b.disabled = value && b.id !== 'background-close'; });
  }
  function report(message, error = false) {
    status.textContent = message;
    status.dataset.kind = error ? 'error' : 'info';
    status.setAttribute('role', error ? 'alert' : 'status');
    status.setAttribute('aria-live', error ? 'assertive' : 'polite');
    if (dialog.open) anchorDialog(dialog, document.querySelector('#btn-settings'));
  }
  async function load() {
    if (busy) return;
    lock(true);
    try {
      let response = await request();
      if (response.status === 204 && response.headers.get('X-Background-Configured') === 'false') {
        let legacy;
        try { legacy = localStorage.getItem(key); } catch (_) { /* Optional migration. */ }
        if (typeof legacy === 'string' && legacy.length <= 2 * 1024 * 1024 && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(legacy)) {
          const blob = await (await fetch(legacy)).blob();
          try { await request('PUT', blob, true); }
          catch (error) { if (error.status !== 412) { apply(blob); throw error; } }
          response = await request();
        }
      }
      apply(response.status === 204 ? null : await response.blob());
      removeLegacy();
      report('');
    } catch (error) { report(error.message, true); }
    finally {
      lock(false);
      if (reloadPending) { reloadPending = false; load(); }
    }
  }
  document.querySelector('#btn-background').onclick = () => {
    openDialogAnchored(dialog, document.querySelector('#btn-settings'));
    load();
  };
  dialog.querySelector('#background-close').onclick = () => closeDialogAnchored(dialog);
  dialog.querySelector('#background-select').onclick = () => input.click();
  dialog.querySelector('#background-reset').onclick = async () => {
    if (busy) return;
    lock(true);
    try {
      await request('DELETE');
      removeLegacy(); apply(null); report(window.t('background_default_restored'));
    } catch (error) { report(error.message + window.t('background_kept'), true); }
    finally { lock(false); }
  };
  input.onchange = async () => {
    const file = input.files[0]; input.value = '';
    if (!file || busy) return;
    lock(true); report(window.t('background_saving'));
    let preview;
    try {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error(window.t('background_bad_format'));
      if (file.size > limit) throw new Error(window.t('background_too_large'));
      preview = URL.createObjectURL(file);
      const image = new Image(); image.src = preview;
      try { await image.decode(); } catch (_) { throw new Error(window.t('background_bad_image')); }
      if (image.naturalWidth * image.naturalHeight > 25_000_000) throw new Error(window.t('background_too_many_pixels'));
      await request('PUT', file);
      removeLegacy(); apply(file); report(window.t('background_saved'));
    } catch (error) { report(error.message + ' 原背景已保留。', true); }
    finally { if (preview) URL.revokeObjectURL(preview); lock(false); }
  };
  document.addEventListener('background-auth-ready', () => {
    if (busy) reloadPending = true;
    else load();
  });
  load();
})();
