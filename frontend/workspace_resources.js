// Shared form for setup and existing member workspaces.
window.workspaceResourceForm = (container, config) => {
  container.replaceChildren();
  container.classList.add('workspace-resource-form');
  const field = (parent, label, value, type = 'text') => {
    const row = document.createElement('label'), input = document.createElement('input');
    row.textContent = label; input.type = type; input.value = value; input.required = true;
    input.setAttribute('aria-label', label);
    input.style.cssText = 'width:100%;box-sizing:border-box';
    if (type === 'number') { input.min = '1024'; input.max = '65535'; }
    row.append(input); parent.append(row); return input;
  };
  const rows = document.createElement('div'); container.append(rows);
  const services = [];
  const add = (name = '', value = {port: '', host: '127.0.0.1'}) => {
    const box = document.createElement('fieldset'); box.className = 'workspace-resource-card';
    const fields = document.createElement('div'); fields.className = 'workspace-resource-grid workspace-service-grid'; box.append(fields);
    const row = {box, name: field(fields, '服务名称', name), port: field(fields, '服务端口', value.port, 'number'), host: field(fields, '监听地址', value.host)};
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '移除服务';
    remove.onclick = () => { services.splice(services.indexOf(row), 1); box.remove(); };
    fields.append(remove); rows.append(box); services.push(row);
    row.name.dialogValidation = value => {
      const name = value.trim();
      return name && services.some(other => other !== row && other.name.value.trim() === name) ? '服务名称重复，请使用唯一名称。' : '';
    };
  };
  Object.entries(config.services).forEach(([name, value]) => add(name, value));
  const addButton = document.createElement('button'); addButton.type = 'button'; addButton.textContent = '添加服务'; addButton.onclick = () => add(); container.append(addButton);
  const browserBox = document.createElement('fieldset'); browserBox.className = 'workspace-resource-card';
  const enabledLabel = document.createElement('label'), enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = !!config.browser;
  enabledLabel.append(enabled, document.createTextNode('分配浏览器调试资源')); browserBox.append(enabledLabel);
  const browserFields = document.createElement('div'); browserFields.className = 'workspace-resource-grid workspace-browser-grid'; browserBox.append(browserFields);
  const browser = config.browser || {port: '', host: '127.0.0.1', data_directory: ''};
  const port = field(browserFields, '浏览器调试端口', browser.port, 'number');
  const host = field(browserFields, '浏览器监听地址', browser.host);
  const directory = field(browserFields, '浏览器用户数据目录', browser.data_directory);
  directory.parentElement.classList.add('workspace-resource-wide');
  const toggle = () => { browserFields.hidden = !enabled.checked; }; enabled.onchange = toggle; toggle();
  container.append(browserBox);
  const testData = field(container, '测试数据目录', config.test_data_directory);
  return () => {
    const invalid = [...container.querySelectorAll('input')].filter(input => !validateDialogField(input));
    if (invalid.length) { invalid[0].focus(); throw new Error('请按红色提示修正工作区资源配置。'); }
    const result = Object.create(null);
    for (const row of services) {
      const name = row.name.value.trim();
      if (!name || Object.hasOwn(result, name)) throw new Error('请填写唯一的服务名称');
      result[name] = {port: Number(row.port.value), host: row.host.value.trim()};
    }
    return {services: result, browser: enabled.checked ? {port: Number(port.value), host: host.value.trim(), data_directory: directory.value.trim()} : null,
      test_data_directory: testData.value.trim()};
  };
};

window.openWorkspaceResources = async (project, profile, onClose = () => {}) => {
  const dialog = document.createElement('dialog'); dialog.className = 'workspace-resource-dialog'; dialog.style.cssText = 'width:600px;max-width:calc(100vw - 24px);max-height:85dvh;overflow:auto;padding:20px';
  const title = document.createElement('h3'); title.textContent = '工作区资源配置';
  const note = document.createElement('p'), form = document.createElement('div'), status = document.createElement('p'); status.setAttribute('role', 'status');
  status.className = 'workspace-resource-status';
  const save = document.createElement('button'), close = document.createElement('button');
  const actions = document.createElement('div'); actions.className = 'dlg-btns'; actions.append(save, close);
  save.textContent = '保存资源配置'; close.textContent = '关闭'; save.disabled = true;
  dialog.append(title, note, form, status, actions); document.body.append(dialog); dialog.showModal();
  let saving = false;
  close.onclick = () => dialog.close(); dialog.onclose = () => { dialog.remove(); onClose(); }; dialog.oncancel = e => { if (saving) e.preventDefault(); };
  const url = `/projects/${encodeURIComponent(project)}/workspaces/${encodeURIComponent(profile)}/resources`;
  try {
    const data = await api(url);
    if (!dialog.open) return;
    note.textContent = `目录：${data.path}。共用成员：${data.members.join('、')}。保存后下次唤醒生效。`;
    note.style.overflowWrap = 'anywhere';
    const read = workspaceResourceForm(form, data.config); save.disabled = false;
    status.textContent = data.revision ? '已加载保存的配置' : '以下为建议配置，保存后由引擎维护';
    save.onclick = async () => {
      if (saving) return;
      status.dataset.error = 'false';
      try {
        const config = read(); saving = true;
        dialog.querySelectorAll('button,input').forEach(n => n.disabled = true);
        status.textContent = '正在校验并保存…';
        const result = await api(url, {method: 'PUT', body: JSON.stringify({config, revision: data.revision})});
        data.revision = result.revision; status.textContent = '已保存，下次唤醒生效。';
      } catch (err) { status.textContent = err.message; status.dataset.error = 'true'; }
      finally { saving = false; dialog.querySelectorAll('button,input').forEach(n => n.disabled = false); }
    };
  } catch (err) { status.textContent = err.message; status.dataset.error = 'true'; }
};
