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
      const error = new Error(typeof info.detail === 'string' ? info.detail : '服务端背景读写失败，请确认引擎已更新并重启。');
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
  dialog.innerHTML = `<h3 id="background-title">舷窗背景</h3>
    <p class="profile-help">图片保存在本地服务端，同一服务的页面共用背景。恢复默认会释放当前图片占用的空间。</p>
    <p class="profile-help">支持 PNG、JPEG、WebP，最大 10 MiB、2500 万像素。</p>
    <input id="background-file" type="file" accept="image/png,image/jpeg,image/webp" hidden>
    <p id="background-status" class="profile-help" role="status" aria-live="polite"></p>
    <div class="dlg-btns"><button type="button" id="background-select">选择图片</button><button type="button" id="background-reset">恢复默认行星图</button><button type="button" id="background-close">关闭</button></div>`;
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
      removeLegacy(); apply(null); report('已恢复默认行星图。');
    } catch (error) { report(error.message + ' 原背景已保留。', true); }
    finally { lock(false); }
  };
  input.onchange = async () => {
    const file = input.files[0]; input.value = '';
    if (!file || busy) return;
    lock(true); report('正在保存图片…');
    let preview;
    try {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('请选择 PNG、JPEG 或 WebP 图片。');
      if (file.size > limit) throw new Error('图片过大，最大为 10 MiB。');
      preview = URL.createObjectURL(file);
      const image = new Image(); image.src = preview;
      try { await image.decode(); } catch (_) { throw new Error('图片损坏或无法解码，请重新选择。'); }
      if (image.naturalWidth * image.naturalHeight > 25_000_000) throw new Error('图片最多为 2500 万像素。');
      await request('PUT', file);
      removeLegacy(); apply(file); report('背景已保存，深浅主题均生效。');
    } catch (error) { report(error.message + ' 原背景已保留。', true); }
    finally { if (preview) URL.revokeObjectURL(preview); lock(false); }
  };
  document.addEventListener('background-auth-ready', () => {
    if (busy) reloadPending = true;
    else load();
  });
  load();
})();
