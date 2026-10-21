// req#110 方案A（task#189）：网页内目录选择器，替代原生 FolderBrowserDialog。
// 原生对话框靠 SetForegroundWindow+置顶跟全屏 Chrome 抢前台，被遮挡选不了——改为
// 网页内 <dialog> 弹层（永久在浏览器内，无遮挡，远程访问可复用）。
// 依赖引擎「列目录」接口（task#188）：GET /local/directory-list?path=&offset=&limit=
// 返回 {path, parent, entries[{name, path, navigable}], total, offset, limit, has_more, error?}
// error ∈ not_found / not_directory / permission_denied（不 500）。
//
// 共享调用方：project_import.js（#import-pick-root）、index.html（pj-d-root 设置角色按钮）。
// 约定：选择框是引擎所在机器的目录，UI 明标「服务器目录」。
// normpath 说明（architect 交付 854 遗留）：list_directory 返回的 path 可能为原始未规范化值，
// 前端导航一律走 entry.path（规范路径），面包屑/parent 由接口返回，不自行拼接，规避该残留。
async function selectProjectDirectory(input, button) {
  if (button.disabled) return;

  const dlg = document.createElement('dialog');
  dlg.setAttribute('aria-labelledby', 'dir-picker-title');
  dlg.setAttribute('dir-picker-layer', ''); // req#110 #189：标记为子弹层弹窗，宿主 outside-closer 对它豁免
  dlg.style.cssText = 'width:520px;max-width:calc(100vw - 24px);box-sizing:border-box;max-height:calc(100dvh - 24px);overflow:auto;';
  dlg.innerHTML = `
    <h3 id="dir-picker-title">选择目录（服务器目录）</h3>
    <p class="profile-help" style="margin:0 0 8px;">浏览引擎所在机器的目录并选择；只列子目录。</p>
    <nav id="dir-picker-crumbs" aria-label="目录层级" style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin-bottom:8px;font-size:12.5px;"></nav>
    <div id="dir-picker-list" role="listbox" aria-label="子目录列表" tabindex="0"
      style="min-height:180px;max-height:300px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:4px;"></div>
    <p id="dir-picker-more" role="status" style="margin:6px 0 0;font-size:12px;color:var(--fg-2);"></p>
    <p id="dir-picker-status" role="status" style="overflow-wrap:anywhere;"></p>
    <div class="dlg-btns">
      <button id="dir-picker-cancel" type="button">取消</button>
      <button id="dir-picker-ok" type="button" class="primary">选择此目录</button>
    </div>`;
  document.body.appendChild(dlg);

  const list = dlg.querySelector('#dir-picker-list');
  const crumbs = dlg.querySelector('#dir-picker-crumbs');
  const moreEl = dlg.querySelector('#dir-picker-more');
  const statusText = dlg.querySelector('#dir-picker-status');
  const okBtn = dlg.querySelector('#dir-picker-ok');
  // 底部保留「取消」；目录行通过单击导航，选择操作由底部按钮完成。
  // 支持取消按钮和外部点击关闭；Escape 沿用浏览器原生 cancel 行为。

  let currentPath = (input.value || '').trim(); // 初始定位：输入框当前值（空则走接口根视图）
  let currentEntries = [];                       // 当前页可导航条目（entry.path 规范路径）
  let pageOffset = 0;
  const PAGE_SIZE = 200;                         // 与后端 DEFAULT_LIMIT 一致
  let version = 0;                               // 导航竞态守卫：快速连点只认最后一次

  function renderCrumbs(path, parent) {
    crumbs.replaceChildren();
    // 固定入口（req#110 #189 验收退回②）：「服务器根目录」= 调接口根视图（path='' 盘符列表），
    // 任何时候都能一键回到磁盘根目录换盘符（如 E:/ → C:/）——不再依赖面包屑逐级回退
    const rootChip = document.createElement('button');
    rootChip.type = 'button';
    rootChip.textContent = '服务器根目录';
    rootChip.style.cssText = 'border:none;background:transparent;color:var(--accent-2);cursor:pointer;padding:2px 4px;border-radius:4px;';
    rootChip.addEventListener('click', () => { pageOffset = 0; navigate(''); });
    crumbs.append(rootChip);
    const norm = String(path || '').replace(/\\/g, '/');
    if (!path) {
      const here = document.createElement('span');
      here.textContent = '› 磁盘根目录';
      here.setAttribute('aria-current', 'page');
      crumbs.append(here);
      return;
    }
    const parts = norm.split('/').filter(Boolean);
    const segments = [];
    if (/^[A-Za-z]:$/.test(parts[0] || '')) {
      segments.push(parts[0] + '/');
      for (let i = 1; i < parts.length; i++) segments.push(parts.slice(0, i + 1).join('/') + '/');
    } else if (norm.startsWith('/')) {
      segments.push('/');
      // 从 0 遍历，确保面包屑包含根目录下的第一级目录。
      for (let i = 0; i < parts.length; i++) segments.push('/' + parts.slice(0, i + 1).join('/'));
    } else {
      // 无盘符前缀的路径（理论不出现，接口 path 回显）：按原样整体一段
      segments.push(norm);
    }
    for (let i = 0; i < segments.length; i++) {
      crumbs.append(document.createTextNode('›'));
      const last = i === segments.length - 1;
      const chip = last ? document.createElement('span') : document.createElement('button');
      chip.type = 'button';
      if (last) {
        // req#110 #189：末级只显示目录名（非完整路径），面包屑更易读
        chip.textContent = segments[i].replace(/\/+$/, '').split('/').pop() || segments[i];
        chip.setAttribute('aria-current', 'page');
      }
      else {
        chip.textContent = segments[i];
        chip.style.cssText = 'border:none;background:transparent;color:var(--accent-2);cursor:pointer;padding:2px 4px;border-radius:4px;';
        const target = segments[i];
        chip.addEventListener('click', () => { pageOffset = 0; navigate(target); });
      }
      crumbs.append(chip);
    }
  }

  function renderRows(entries, startIndex = 0) {
    if (!startIndex) list.replaceChildren();
    if (!entries.length && !startIndex) {
      if (statusText.textContent.includes('无权限')) return; // 无权限已提示，不再叠加空目录文案
      const empty = document.createElement('p');
      empty.className = 'profile-help';
      empty.style.cssText = 'text-align:center;padding:14px;margin:0;';
      empty.textContent = '（无子目录）';
      list.append(empty);
      return;
    }
    for (const entry of entries) {
      const row = document.createElement('div');
      row.setAttribute('role', 'option');
      row.style.cssText = 'display:flex;gap:8px;align-items:center;padding:6px 10px;border-radius:6px;cursor:pointer;';
      row.addEventListener('mouseenter', () => { row.style.background = 'rgba(128,128,128,.12)'; });
      row.addEventListener('mouseleave', () => { row.style.background = 'transparent'; });
      const name = document.createElement('span');
      name.textContent = entry.name;
      name.style.cssText = 'flex:1;min-width:0;overflow-wrap:anywhere;';
      // 单击目录行进入目录，底部「选择此目录」按钮提交当前位置。
      row.append(name);
      row.addEventListener('click', () => { pageOffset = 0; navigate(entry.path); });
      list.append(row);
    }
  }

  const ERR_TEXT = {
    permission_denied: '该目录无权限访问，请选择其他目录。',
    not_found: '目录不存在，可能已被移动或删除。',
    not_directory: '该路径不是目录。',
  };

  async function navigate(path, {append = false} = {}) {
    const v = ++version;
    statusText.textContent = '正在读取…';
    try {
      let data = null;
      try {
        data = await api('/local/directory-list?path=' + encodeURIComponent(path) +
          '&offset=' + pageOffset + '&limit=' + PAGE_SIZE);
      } catch (err) {
        if (v !== version) return;
        statusText.textContent = '读取失败：' + err.message + '。请重试或直接输入路径。';
        return;
      }
      if (v !== version || !dlg.open) return;
      statusText.textContent = data.error ? ERR_TEXT[data.error] || ('读取失败：' + data.error) : '';
      currentPath = data.path || '';
      currentEntries = append ? currentEntries.concat(data.entries || []) : (data.entries || []);
      renderCrumbs(data.path, data.parent);
      if (append) {
        // req#110 #189：「加载更多」追加下一页而非替换（替换会把前几页冲掉）
        const existed = list.querySelectorAll('[role=option]').length;
        renderRows((data.entries || []), existed);
      } else {
        renderRows(currentEntries);
      }
      // 大目录分页（req#110 验收①②）
      if (data.has_more) {
        const next = document.createElement('button');
        next.type = 'button';
        next.textContent = `加载更多（第 ${data.offset + (data.entries || []).length + 1} - ${Math.min(data.total, data.offset + data.limit)} 项，共 ${data.total}）`;
        next.style.cssText = 'width:100%;margin-top:4px;border:1px dashed var(--border);background:transparent;color:var(--fg-2);cursor:pointer;padding:6px;border-radius:6px;';
        next.addEventListener('click', () => { pageOffset += data.limit; navigate(data.path, {append: true}); });
        moreEl.replaceChildren(next);
      } else {
        moreEl.replaceChildren();
      }
    } finally {
      if (v === version) okBtn.disabled = false;
    }
  }

  function finish(path) {
    // 保留输入事件派发，兼容已有监听者。
    // 后端 directory_list.py 的 entry.path 使用 os.path.join 拼接，
    // Windows 下末段是反斜杠（如 E:/Code/bot-group/frontend\assets）——回填前统一正斜杠，
    // 与面包屑/导航的斜杠风格一致（导航用正斜杠路径请求接口，后端两种都认）。
    input.value = String(path).replace(/\\/g, '/');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    dlg.close();
  }

  okBtn.addEventListener('click', () => {
    // req#110 #189 验收退回①：可确认性按「状态」判——目录加载成功（statusText 空）即可确认
    // currentPath（接口回显路径），entries 为空（合法空目录）也必须可选；
    // 加载中/失败（有状态文案）时确认无效，防选错
    if (!dlg.open || !currentPath || statusText.textContent) return;
    finish(currentPath);
  });
  // 外部点击由 openDialogCentered 处理；Escape 沿用浏览器原生 cancel 行为。
  // 「取消」关闭时保留输入值，重开仍定位到当前值对应的目录。
  const cancelBtn = dlg.querySelector('#dir-picker-cancel');
  cancelBtn.addEventListener('click', () => dlg.close());
  // dialog 关闭时释放 resize 监听、ResizeObserver 和 outsideCloser，防止泄漏。
  dlg.addEventListener('close', () => { closeDialogCentered(dlg); dlg.remove(); });

  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    // 目录选择器居中显示并限制在视口内。
    // 异步内容可能在首帧定位后改变高度；openDialogCentered
    // 已挂 ResizeObserver 自动重居中，navigate 完成后再显式补一次 place 兜底（渲染→布局稳定）。
    const recenter = openDialogCentered(dlg);
    await navigate(currentPath);     // 空值→根视图；带值→直接打开该目录
    if (dlg.open) recenter();
  } catch (err) {
    dlg.remove();
    showAlert('选择目录失败：' + err.message);
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}
