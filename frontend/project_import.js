// Project creation/import: seven steps with rules and workspace resource review.
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dlg-project-import';
  dialog.setAttribute('aria-labelledby', 'import-title');
  dialog.innerHTML = `
    <header class="import-header">
      <div class="import-title-row"><h3 id="import-title">导入已有项目</h3><span id="import-step"></span></div>
      <p class="profile-help">逐步配置团队，核对协作规则并确认文件变更。</p>
      <ol class="import-steps" aria-label="导入步骤">
        <li>项目信息</li><li>协作方式</li><li>角色设置</li><li>协作规则</li><li>成员分配</li><li>工作区资源</li><li>确认导入</li>
      </ol>
    </header>
    <div class="import-body">
    <div id="import-directory">
      <h4 tabindex="-1">项目放在哪里？</h4>
      <p class="profile-help">选择已有目录，再为项目填写一个容易识别的名字。</p>
      <label for="import-root">已有项目目录</label>
      <div class="import-input-row">
        <input id="import-root" required placeholder="E:/Code/existing-project" style="flex:1;min-width:0;box-sizing:border-box;">
        <button id="import-pick-root" class="directory-picker-button" type="button" title="打开文件夹选择器" aria-label="选择已有项目目录"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v2M3 7v12a1 1 0 0 0 1 1h15l3-10H7L4 20"/></svg></button>
      </div>
      <p class="profile-help">使用本地盘普通绝对路径；目录联接、符号链接和 subst 按实际目标查重。UNC 与扩展前缀路径请改用本地盘路径。</p>
      <label>项目显示名<input id="import-name" maxlength="200" autocomplete="off" placeholder="自动使用目录名称，可修改"></label>
      <label>项目简介<textarea id="import-description" placeholder="项目目标或用途（选填）"></textarea></label>
      <label>项目模板<select id="import-template"><option value="software">软件研发</option><option value="research">产品调研</option><option value="writing">内容写作</option><option value="product-prototype">产品原型设计</option><option value="algorithm">算法研发</option><option value="testing">测试项目</option></select></label>
      <p class="profile-help">模板提供角色、默认权限和基础文件，可在后续步骤调整。</p>
      <p class="profile-help">下一步会清理可确认的初始化残留；原有项目文件和手工修改保留。</p>
    </div>
    <div id="import-existing" hidden></div>
    <div id="import-config" hidden>
      <h4 tabindex="-1">团队如何协作？</h4>
      <p class="profile-help">选择工作目录模式，现有协作约定继续保留。</p>
      <p id="import-canonical" style="overflow-wrap:anywhere;"></p>
      <div id="import-warnings" class="profile-help"></div>
      <label>工作目录模式<select id="import-mode"><option value="shared">共用项目主目录</option><option value="worktree">独立 Git worktree</option></select></label>
      <p id="import-mode-help" class="import-note">成员共用项目主目录，按约定串行修改，交接前核对并提交变更。</p>
      <div id="import-supplement-option" class="project-permission-options"><label><input id="import-supplement" type="checkbox" checked>补齐项目基础文件</label></div>
      <p id="import-files-help" class="profile-help">根据模板补齐说明及文档目录，已有文件保留。AGENTS.md 在第四步单独编辑确认。</p>
      <div id="import-git-settings" hidden>
        <p id="import-git-status" class="profile-help"></p>
        <div id="import-git-option" class="project-permission-options"><label><input id="import-git-init" type="checkbox">初始化本地 Git，并补充缺失的 .gitignore</label></div>
        <div id="import-baseline-settings" hidden>
          <label id="import-baseline-choice">工作区基线<select id="import-baseline"><option value="commit">提交选定文件后作为基线</option><option value="existing">使用已有提交作为基线</option></select></label>
          <p id="import-baseline-initial" class="import-note" hidden>当前仓库尚无提交。系统将按下面确认的文件范围创建首次提交，并将其作为工作区基线。</p>
          <p class="profile-help">选定文件由 Project Setup 提交。未选中的本地改动保留在主目录；远程仓库可后续配置。</p>
          <button id="import-git-check" type="button">重新检查 Git 和提交范围</button>
          <div id="import-commit-files"></div>
        </div>
      </div>
      <label id="import-worktree-label" hidden>worktree 根目录<input id="import-worktree" style="width:100%;box-sizing:border-box;"></label>
      <details class="import-card"><summary>查看现有规则与文档</summary><div id="import-documents"></div></details>
    </div>
    <div id="import-role-step" hidden>
      <h4 tabindex="-1">设置团队角色</h4>
      <p class="profile-help">按项目协作约定添加角色，展开卡片调整默认权限。同角色成员共享这些权限。</p>
      <details id="import-soul" class="import-card">
        <summary>SOUL 角色参考 · 查看、复制与下载</summary>
        <div id="import-soul-content"></div>
        <p id="import-soul-status" role="status"></p>
        <button id="import-soul-retry" type="button" hidden>重新读取 SOUL 参考</button>
      </details>
      <div class="import-input-row"><input id="import-role-name" maxlength="100" aria-label="角色名称" placeholder="例如：架构师、开发、审查员"><button id="import-role-add" type="button">添加角色</button></div>
      <div id="import-roles"></div>
    </div>
    <div id="import-agents-step" hidden>
      <h4 tabindex="-1">核对角色与协作规则</h4>
      <p class="profile-help">AGENTS.md 按当前模板重新生成。请检查职责、交接和验收流程；最终保存时备份并覆盖原文件。</p>
      <p id="import-agents-roles" class="import-note"></p>
      <label for="import-agents-content">AGENTS.md</label>
      <textarea id="import-agents-content" spellcheck="false" style="width:100%;min-height:320px;box-sizing:border-box;font-family:monospace;"></textarea>
      <p class="profile-help">最终确认页展示差异；修改已有 AGENTS.md 时会保留原文备份。</p>
      <label><input id="import-agents-reviewed" type="checkbox">我已核对当前角色、权限及协作方式，并确认文档中的职责与流程</label>
    </div>
    <div id="import-member-step" hidden>
      <h4 tabindex="-1">选择成员与负责人</h4>
      <p class="profile-help">勾选已有成员或创建 profile。每位成员选择一个角色，最终确认时统一入组并配置授权。</p>
      <p id="import-member-count" class="import-note"></p>
      <div id="import-members"></div>
    </div>
    <div id="import-resource-step" hidden><h4 tabindex="-1">配置工作区资源</h4><p>引擎按已选成员分配工作区。共享目录的成员共用配置；端口及数据目录在最终确认时校验并保存。可删除暂时用不到的服务。</p><div id="import-resources"></div></div>
    <div id="import-review" hidden></div>
    </div>
    <footer class="import-footer">
    <p id="import-status" role="status" style="overflow-wrap:anywhere;"></p>
    <div class="dlg-btns"><button id="import-close" type="button">取消</button><span class="import-button-spacer"></span><button id="import-back" type="button" hidden>上一步</button><button id="import-preview" class="primary" type="button">下一步</button><button id="import-next" type="button" class="primary" hidden>下一步</button><button id="import-submit" type="button" class="primary" hidden>确认导入</button><button id="import-progress" type="button" hidden>查询操作结果</button></div>
    </footer>`;
  document.body.appendChild(dialog);
  const el = id => dialog.querySelector('#import-' + id);
  el('pick-root').addEventListener('click', () => selectProjectDirectory(el('root'), el('pick-root')));
  let version = 0, busy = false, preview = null, roles = [], members = new Map(), payload = null, step = 0;
  let nameEdited = false;
  let historicalName = null;
  let submittedRequest = '';
  let ownerRole = '项目负责人';
  let selectedTemplate = null;
  let templateExplicit = false;
  el('template').addEventListener('change', () => { templateExplicit = true; });
  let referenceGeneration = 0;
  function resetReferences() {
    referenceGeneration++;
    el('soul').open = false;
    el('soul-content').replaceChildren(); el('soul-status').textContent = '';
    el('soul-retry').hidden = true;
  }
  async function loadReferences() {
    const selected = selectedTemplate, generation = ++referenceGeneration;
    el('soul-content').replaceChildren(); el('soul-retry').hidden = true;
    if (!selected || selected.id === 'blank') {
      el('soul-status').textContent = '空白项目可自行编写角色人格配置。'; return;
    }
    el('soul-status').textContent = '读取 SOUL 参考…';
    try {
      const template = await api(`/projects/templates/${encodeURIComponent(selected.id)}`);
      if (generation !== referenceGeneration || !dialog.open || selectedTemplate !== selected) return;
      if (template.version !== selected.version) throw new Error('模板版本已变化，请返回第一步重新读取目录和模板。');
      renderTemplateReferences(el('soul-content'), template, {snapshot: false});
      el('soul-status').textContent = '以下为所选模板的角色示例；自定义角色可据此调整。';
    } catch (error) {
      if (generation !== referenceGeneration || !dialog.open || selectedTemplate !== selected) return;
      el('soul-status').textContent = error.message; el('soul-retry').hidden = false;
    }
  }
  el('soul').addEventListener('toggle', () => { if (el('soul').open) loadReferences(); });
  el('soul-retry').addEventListener('click', loadReferences);
  let agentsDraft = null;
  let creationMode = 'import', commitFiles = null, reviewedFiles = [];
  let resourceDrafts = [], resourceReaders = [];
  const captureResources = () => { if (resourceReaders.length) resourceDrafts = resourceReaders.map(r => ({key: r.key, config: r.read()})); };
  function renderResourceDrafts() {
    resourceReaders = []; el('resources').replaceChildren();
    for (const resource of resourceDrafts) {
      const card = document.createElement('details'), title = document.createElement('summary'), fields = document.createElement('div');
      const bot = preview.bots.find(b => `bot-${b.bot_id}` === resource.key);
      title.textContent = resource.key === 'shared' ? `共享主目录（${members.size} 位成员）` : `独立工作区 · ${bot?.display_name || bot?.host_profile || resource.key}`;
      card.open = true; card.append(title, fields); el('resources').append(card);
      resourceReaders.push({key: resource.key, read: workspaceResourceForm(fields, resource.config)});
    }
    if (!resourceDrafts.length) el('resources').textContent = '尚未选择成员，后续可从成员的工作区按钮配置资源。';
  }
  const needsGitReview = () => el('mode').value === 'worktree' || creationMode === 'new' && el('git-init').checked;
  const invalidateRules = () => { el('agents-reviewed').checked = false; };
  el('agents-content').addEventListener('input', invalidateRules);
  function syncProjectName(root = el('root').value) {
    if (nameEdited && el('name').value.trim()) return;
    const basename = root.trim().split(/[\\/]/).filter(Boolean).pop() || '';
    el('name').value = /^[a-z]:$/i.test(basename) ? '' : basename;
    nameEdited = false;
  }
  const rootChanged = () => {
    if (historicalName !== null) { nameEdited = false; historicalName = null; }
    syncProjectName();
  };
  el('root').addEventListener('input', rootChanged);
  el('root').addEventListener('change', rootChanged);
  el('name').addEventListener('input', () => { historicalName = null; nameEdited = !!el('name').value.trim(); });
  const status = (text, error = false) => { el('status').textContent = text; el('status').dataset.error = String(error); };
  const button = (label, handler) => {
    const node = document.createElement('button'); node.type = 'button'; node.textContent = label;
    node.addEventListener('click', handler); return node;
  };
  function close() { version++; closeDialogAnchored(dialog); }
  function showStep(value) {
    step = value;
    el('step').textContent = `第 ${value + 1} 步 / 共 7 步`;
    dialog.querySelectorAll('.import-steps li').forEach((item, index) => {
      item.classList.toggle('complete', index < value);
      if (index === value) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    el('directory').hidden = value !== 0;
    el('config').hidden = value !== 1;
    el('role-step').hidden = value !== 2;
    el('agents-step').hidden = value !== 3;
    el('member-step').hidden = value !== 4;
    el('resource-step').hidden = value !== 5;
    el('review').hidden = value !== 6;
    el('back').hidden = value === 0;
    el('preview').hidden = value !== 0;
    el('next').hidden = value < 1 || value > 5;
    el('next').textContent = value === 5 ? '预览完整配置' : '下一步';
    el('submit').hidden = value !== 6;
    el('progress').hidden = value !== 6 || !payload?.request_id || submittedRequest !== payload.request_id;
    dialog.querySelector('.import-body').scrollTop = 0;
    if (dialog.open) dialog.querySelector('.import-body > div:not([hidden]) h4')?.focus();
  }
  function lock(value) {
    busy = value;
    dialog.setAttribute('aria-busy', String(value));
    for (const node of dialog.querySelectorAll('button,input,select,textarea')) node.disabled = value;
    if (!value) {
      el('supplement').disabled = creationMode === 'new';
      el('git-init').disabled = el('mode').value === 'worktree';
    }
  }
  function permissionEditor(values, onChange) {
    const box = document.createElement('div'); box.className = 'project-permission-options import-permissions';
    for (const permission of preview.permissions) {
      const label = document.createElement('label'), input = document.createElement('input');
      input.type = 'checkbox'; input.value = permission.key; input.checked = values.includes(permission.key);
      input.addEventListener('change', () => onChange([...box.querySelectorAll('input:checked')].map(n => n.value)));
      label.append(input, document.createTextNode(permission.label)); box.append(label);
    }
    return box;
  }
  function renderRoles() {
    invalidateRules();
    el('roles').replaceChildren();
    if (!roles.length) {
      const empty = document.createElement('p'); empty.className = 'import-empty';
      empty.textContent = '尚未添加角色。可以先登记项目，稍后在项目设置中补充。'; el('roles').append(empty);
    }
    for (const role of roles) {
      const box = document.createElement('section'), title = document.createElement('strong');
      box.className = 'import-card';
      const header = document.createElement('div'); header.className = 'import-card-header';
      title.textContent = role.name; header.append(title);
      if (role.name !== ownerRole) header.append(button('移除角色', () => {
        showAlert(`移除角色「${role.name}」会清除已选成员的角色绑定，派工、审查、验收等职责可能缺少执行人。请在规则审阅步骤自行核对协作正文中的角色引用与职责。`);
        roles = roles.filter(r => r !== role);
        for (const member of members.values()) if (member.role === role.name) member.role = '';
        renderRoles(); renderMembers();
      }));
      const details = document.createElement('details'), summary = document.createElement('summary');
      const caption = () => { summary.textContent = `调整权限 · 已启用 ${role.permissions.length} 项`; };
      caption(); details.append(summary, permissionEditor(role.permissions, values => {
        role.permissions = values; invalidateRules(); caption();
        showAlert(`角色「${role.name}」的权限已调整，派工、审查、验收等职责可能缺少有权限的执行人。请在规则审阅步骤自行核对协作正文与角色授权。`);
      }));
      box.append(header);
      if (role.name === ownerRole) {
        const duty = document.createElement('small');
        duty.textContent = '接收阻塞通知';
        duty.style.cssText = 'color:var(--fg-3);font-size:12px;font-weight:normal';
        header.append(duty);
      }
      box.append(details);
      el('roles').append(box);
    }
  }
  el('supplement').addEventListener('change', renderRoles);
  function renderMembers() {
    const currentPreview = preview;
    el('member-count').textContent = `已选 ${members.size} / ${preview.bots.length} 位成员`;
    MemberPicker.render(el('members'), {items: preview.bots, selected: members, template: preview.template,
      isActive: () => dialog.open && preview === currentPreview,
      onBusy: value => lock(value),
      onRefresh: items => { preview.bots = items; renderMembers(); },
      onSelect: (bot, selected) => {
        if (!preview.bots.some(item => item.bot_id === bot.bot_id)) preview.bots.push(bot);
        if (selected && !members.has(bot.bot_id)) members.set(bot.bot_id, {bot_id: bot.bot_id, role: '', permissions: []});
        if (!selected) members.delete(bot.bot_id);
        renderMembers();
      },
      extra: (row, bot) => {
        const member = members.get(bot.bot_id);
        const select = document.createElement('select'); select.setAttribute('aria-label', '成员角色');
        select.style.maxWidth = '100%';
        select.replaceChildren(new Option('角色留空', ''));
        for (const role of roles) select.add(new Option(role.name, role.name));
        select.value = member.role;
        select.addEventListener('change', () => {
          member.role = select.value;
          if (member.role === ownerRole) {
            for (const other of members.values()) if (other !== member && other.role === ownerRole) other.role = '';
          }
          renderMembers();
        });
        const extra = document.createElement('details'), title = document.createElement('summary'); title.textContent = '高级设置 · 个人额外权限';
        extra.append(title, permissionEditor(member.permissions, values => { member.permissions = values; }));
        row.append(select, extra);
      }
    });
  }
  async function openProject(project) {
    const current = version;
    try {
      const result = await api('/projects');
      if (current !== version || !dialog.open) return;
      S.projects = (result.projects || []).filter(p => !p.archived);
      renderProjectBar(); selectProject(project); close();
    } catch (err) { if (current === version && dialog.open) status('打开失败：' + err.message + '。可重试打开。', true); }
  }
  function existingProjects(projects) {
    el('existing').hidden = false;
    el('existing').replaceChildren();
    for (const project of projects) {
      if (project.archived) {
        const note = document.createElement('p'); note.textContent = `项目「${project.display_name || project.name}」已归档。`; el('existing').append(note);
      } else el('existing').append(button(`${project.display_name || project.name}（${project.name}）`, () => openProject(project.name)));
    }
  }
  window.openProjectInitialization = () => window.openProjectImport({mode: 'new'});
  window.openProjectImport = (options = {}) => {
    creationMode = options.mode === 'new' ? 'new' : 'import';
    resourceDrafts = []; resourceReaders = [];
    commitFiles = null; reviewedFiles = [];
    const creating = creationMode === 'new';
    el('supplement-option').hidden = creating;
    el('files-help').textContent = creating
      ? '系统将自动生成项目基础文件。AGENTS.md 在第四步编辑确认。'
      : '根据模板补齐说明及文档目录，已有文件保留。AGENTS.md 在第四步单独编辑确认。';
    el('title').textContent = creating ? '新建项目' : '导入已有项目';
    dialog.querySelector('.import-steps li:last-child').textContent = creating ? '确认创建' : '确认导入';
    el('submit').textContent = creating ? '确认创建' : '确认导入';
    el('directory').querySelector('h4').textContent = creating ? '项目保存在哪里？' : '项目放在哪里？';
    el('directory').querySelector('.profile-help').textContent = creating ? '选择空目录或新路径，系统将在确认后准备目录和基础文件。' : '选择已有目录，并为项目填写一个容易识别的名字。';
    el('directory').querySelector('label[for="import-root"]').textContent = creating ? '项目保存位置' : '已有项目目录';
    if (creating && !el('template').querySelector('option[value="blank"]')) el('template').add(new Option('空白项目', 'blank'));
    if (!creating) el('template').querySelector('option[value="blank"]')?.remove();
    nameEdited = false;
    historicalName = null;
    selectedTemplate = null;
    submittedRequest = '';
    templateExplicit = false;
    resetReferences();
    agentsDraft = null; el('agents-content').value = ''; invalidateRules();
    el('template').value = 'software'; el('supplement').checked = true;
    version++; busy = false; preview = null; roles = []; members = new Map(); payload = null;
    for (const id of ['root', 'name', 'description', 'worktree', 'role-name']) el(id).value = '';
    el('mode').value = 'shared'; el('worktree-label').hidden = true; el('existing').hidden = true;
    el('commit-files').replaceChildren(); el('baseline-settings').hidden = true;
    try { localStorage.removeItem('project-initialization:last'); localStorage.removeItem('project-setup:last'); } catch { /* Obsolete drafts are never loaded. */ }
    updateModeHelp(); status(''); lock(false); showStep(0); openDialogAnchored(dialog, null); el('root').focus();
  };
  dialog.addEventListener('close', () => { version++; });
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  el('close').addEventListener('click', close);
  el('preview').addEventListener('click', async () => {
    if (busy) return;
    if (!validateDialogField(el('root'))) { el('root').focus(); return; }
    const current = ++version; lock(true); status('正在读取目录…'); el('existing').hidden = true;
    try {
      const result = await api('/projects/import/prepare', {method: 'POST', body: JSON.stringify({root: el('root').value.trim(), creation_mode: creationMode, template: {id: el('template').value}, template_explicit: templateExplicit})});
      if (current !== version || !dialog.open) return;
      if (result.existing_projects.length) {
        existingProjects(result.existing_projects);
        const active = result.existing_projects.find(project => !project.archived);
        if (active) await openProject(active.name);
        else status('该目录对应的项目已归档。');
        return;
      }
      const sameDirectory = preview?.root === result.root && selectedTemplate?.id === result.template.id;
      const directoryChanged = preview?.root !== result.root;
      if (directoryChanged) { resourceDrafts = []; resourceReaders = []; members = new Map(); }
      if (!sameDirectory || selectedTemplate?.version !== result.template.version) {
        agentsDraft = null; el('agents-content').value = ''; invalidateRules();
        commitFiles = null; el('commit-files').replaceChildren();
        el('baseline').value = result.git?.has_commit ? 'existing' : 'commit';
        el('worktree').value = result.root.replace(/[\\/]+$/, '') + '-worktrees';
      }
      preview = result;
      el('git-settings').hidden = !result.git;
      el('git-option').hidden = result.git?.status !== 'missing';
      if (!sameDirectory || result.git?.status !== 'missing') el('git-init').checked = result.git?.status === 'missing';
      el('git-status').textContent = result.git ? ({
        missing: '确认后可自动初始化 Git；独立工作区会同时准备首次提交。',
        existing: result.git.has_commit ? '已有 Git 仓库，可选择已有提交或提交选定改动作为基线。' : '已有空仓库，系统将按确认的文件范围创建首次提交。',
        parent: '当前目录属于上级 Git 仓库，沿用该仓库；worktree 模式请选择仓库根目录。',
        invalid: '发现 Git 元数据但仓库检查失败，请先检查仓库状态。',
        unavailable: 'Git 命令不可用，请安装或修复 Git 后重新检查目录。'
      }[result.git.status]) : '';
      ownerRole = result.owner_role || '项目负责人';
      if (directoryChanged || selectedTemplate?.id !== result.template.id || selectedTemplate?.version !== result.template.version) {
        resetReferences();
        roles = result.default_roles.map(r => ({...r, permissions: [...r.permissions]}));
        for (const member of members.values()) if (!roles.some(r => r.name === member.role)) member.role = '';
      }
      selectedTemplate = result.template;
      if (!el('template').querySelector(`option[value="${result.template.id}"]`)) el('template').add(new Option('空白项目', result.template.id));
      el('template').value = result.template.id;
      if (directoryChanged && result.historical_config) {
        const history = result.historical_config;
        if (!nameEdited) { el('name').value = history.display_name; historicalName = history.display_name; nameEdited = true; }
        if (!el('description').value.trim()) el('description').value = history.description;
        el('mode').value = history.directory_mode || 'shared';
        el('worktree').value = history.worktree_root || result.root.replace(/[\\/]+$/, '') + '-worktrees';
        el('worktree-label').hidden = el('mode').value !== 'worktree';
        roles = history.roles.map(role => ({...role, permissions: [...role.permissions]}));
        members = new Map(history.members.map(member => [member.bot_id, {...member, permissions: [...member.permissions]}]));
        if (history.owner_role && history.owner_role !== ownerRole) {
          const previous = roles.find(role => role.name === history.owner_role);
          const current = roles.find(role => role.name === ownerRole);
          if (previous && current) {
            current.permissions = [...new Set([...previous.permissions, ...current.permissions])];
            roles = roles.filter(role => role !== previous);
          } else if (previous) previous.name = ownerRole;
          for (const member of members.values()) if (member.role === history.owner_role) member.role = ownerRole;
        }
        if (!roles.some(role => role.name === ownerRole)) roles.unshift({...result.default_roles.find(role => role.name === ownerRole)});
        resourceDrafts = history.workspace_resources || [];
        el('baseline').value = result.git?.has_commit ? history.git_baseline : 'commit';
        const names = new Set(roles.map(role => role.name));
        for (const member of members.values()) if (!names.has(member.role)) member.role = '';
      }
      const availableIds = new Set(result.bots.map(bot => bot.bot_id));
      for (const botId of members.keys()) if (!availableIds.has(botId)) members.delete(botId);
      el('canonical').textContent = result.root;
      el('warnings').textContent = [result.historical_config ? '已载入完整的已移除项目配置，可在后续步骤调整。' : '', ...result.warnings].join(' ');
      el('documents').replaceChildren();
      for (const doc of result.documents) {
        const item = document.createElement('details'), title = document.createElement('summary'), text = document.createElement('pre');
        title.textContent = `${doc.path} · ${doc.size} 字节${doc.truncated ? ' · 部分预览' : ''}`;
        text.textContent = doc.content; text.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;';
        item.append(title, text); el('documents').append(item);
      }
      if (!result.documents.length) el('documents').textContent = '暂无可预览的规则或文本文档。';
      syncProjectName(result.root);
      updateModeHelp(); renderRoles(); renderMembers(); showStep(1); status(creationMode === 'new' ? '确认创建后自动准备目录和基础文件。' : '请选择协作方式并核对 Git 基线。');
    } catch (err) { if (current === version) status('预览失败：' + err.message + '。请检查目录后重试。', true); }
    finally { if (current === version) lock(false); }
  });
  el('role-add').addEventListener('click', () => {
    const name = el('role-name').value.trim();
    if (!name || roles.some(r => r.name === name)) { status('请填写唯一的角色名称。', true); return; }
    roles.push({name, permissions: []}); el('role-name').value = ''; renderRoles(); renderMembers(); status('');
  });
  function updateModeHelp() {
    const worktree = el('mode').value === 'worktree';
    el('worktree-label').hidden = !worktree;
    el('mode-help').textContent = worktree
      ? '确认后系统自动准备 Git 基线及成员独立工作区，成员按项目约定合并成果。'
      : '成员共用项目主目录，按约定串行修改，交接前核对并提交变更。';
    el('git-option').hidden = worktree || preview?.git?.status !== 'missing';
    el('git-init').disabled = worktree;
    el('baseline-settings').hidden = !needsGitReview();
    const hasCommit = !!preview?.git?.has_commit;
    el('baseline-choice').hidden = !hasCommit;
    el('baseline-initial').hidden = hasCommit;
    if (!hasCommit) el('baseline').value = 'commit';
  }
  el('git-init').addEventListener('change', () => { commitFiles = null; updateModeHelp(); });
  el('baseline').addEventListener('change', () => { commitFiles = null; el('commit-files').replaceChildren(); });
  async function checkGit() {
    const current = version; lock(true); status('正在检查 Git 和提交范围…');
    try {
      const discovery = await api('/projects/import/preview', {method: 'POST', body: JSON.stringify({root: preview.root, creation_mode: creationMode, template: selectedTemplate})});
      if (current !== version || !dialog.open) return false;
      preview.git = discovery.git;
      el('git-status').textContent = discovery.git?.status === 'existing' ? 'Git 仓库可用。' : '确认后自动初始化本地 Git。';
      if (!discovery.git?.has_commit) el('baseline').value = 'commit';
      updateModeHelp();
      const checked = await api('/projects/import/preview', {method: 'POST', body: JSON.stringify(buildPayload(false))});
      if (current !== version || !dialog.open) return false;
      const plan = checked.file_plan.git_plan;
      el('commit-files').replaceChildren();
      if (plan) {
        commitFiles = new Set(plan.files.filter(f => f.selected).map(f => f.path));
        reviewedFiles = plan.files.map(f => f.path);
        const caption = document.createElement('p');
        caption.textContent = plan.baseline === 'existing' ? `工作区基线：${plan.head}。本地改动保留在主目录。` : '核对提交范围，可取消文件；常见凭据、依赖和产物默认排除。';
        el('commit-files').append(caption);
        if (plan.baseline === 'commit') for (const file of plan.files) {
          const row = document.createElement('details'), label = document.createElement('summary');
          const check = document.createElement('input'); check.type = 'checkbox'; check.checked = file.selected;
          check.setAttribute('aria-label', '提交 ' + file.path);
          check.addEventListener('click', event => event.stopPropagation());
          check.addEventListener('change', () => { if (check.checked) commitFiles.add(file.path); else commitFiles.delete(file.path); });
          label.append(check, document.createTextNode(' ' + file.path + (file.excluded ? '（默认排除）' : '')));
          const diff = document.createElement('pre'); diff.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere'; diff.textContent = file.diff || '空文件或二进制内容';
          row.append(label, diff); el('commit-files').append(row);
        }
      }
      status('检查完成，核对后继续。'); return true;
    } catch (err) { if (current === version) status(err.message, true); return false; }
    finally { if (current === version) lock(false); }
  }
  el('git-check').addEventListener('click', () => { commitFiles = null; checkGit(); });
  el('mode').addEventListener('change', () => { commitFiles = null; el('commit-files').replaceChildren(); updateModeHelp(); invalidateRules(); });
  el('back').addEventListener('click', () => { try { if (step === 5) captureResources(); showStep(step - 1); status(''); } catch (err) { status(err.message, true); } });
  el('next').addEventListener('click', async () => {
    if (busy || step < 1 || step > 5) return;
    if (step === 1 && el('mode').value === 'worktree' && !el('worktree').value.trim()) {
      status('请填写 worktree 根目录。', true); el('worktree').focus(); return;
    }
    if (step === 1 && needsGitReview() && commitFiles === null) {
      if (!await checkGit()) return;
    }
    if (step === 2) {
      const current = ++version; lock(true); status('正在加载协作规则…');
      try {
        if (!agentsDraft) {
          const checked = await api('/projects/import/preview', {method: 'POST', body: JSON.stringify(buildPayload(false))});
          if (current !== version || !dialog.open) return;
          agentsDraft = checked.file_plan.agents_draft;
          el('agents-content').value = agentsDraft.content;
        }
        el('agents-roles').textContent = '当前角色：' + roles.map(r => `${r.name}（${r.permissions.join('、') || '无管理权限'}）`).join('；') + '。请核对正文中的角色引用及职责。';
        showStep(3); status('角色或权限变化后，请重新核对正文；已有编辑会保留。');
      } catch (err) { if (current === version) status('读取规则失败：' + err.message, true); }
      finally { if (current === version) lock(false); }
      return;
    }
    if (step === 3 && (!el('agents-content').value.trim() || !el('agents-reviewed').checked)) {
      status('请编辑并确认 AGENTS.md 与当前角色及流程一致。', true); return;
    }
    if (step < 4) {
      showStep(step + 1); status('');
      if (step === 4) {
        const current = version, currentPreview = preview;
        lock(true);
        try {
          const items = await MemberPicker.load('');
          if (current === version && preview === currentPreview && dialog.open) { preview.bots = items; renderMembers(); }
        } catch (error) { if (current === version) status(`成员读取失败：${error.message}。可使用刷新列表重试。`, true); }
        finally { if (current === version) lock(false); }
      }
      return;
    }
    if (step === 4) {
      const current = ++version; lock(true); status('正在生成工作区资源方案…');
      try {
        const slots = el('mode').value === 'shared' ? ['shared'] : [...members.keys()].map(id => `bot-${id}`);
        const draft = buildPayload();
        draft.workspace_resources = resourceDrafts.filter(r => slots.includes(r.key));
        resourceDrafts = draft.workspace_resources;
        const checked = await api('/projects/import/preview', {method: 'POST', body: JSON.stringify(draft)});
        if (current !== version || !dialog.open) return;
        resourceDrafts = checked.workspace_resources; renderResourceDrafts();
        showStep(5); status('建议端口已避开当前占用，最终确认时再次校验。');
      } catch (err) {
        if (current === version && dialog.open) {
          // Retained resources must stay editable when a port or path now conflicts.
          if (resourceDrafts.length) { renderResourceDrafts(); showStep(5); }
          status(err.message, true);
        }
      }
      finally { if (current === version) lock(false); }
      return;
    }
    try { captureResources(); } catch (err) { status(err.message, true); return; }
    if (!el('name').value.trim()) { status('请填写项目显示名。', true); return; }
    payload = buildPayload();
    const current = ++version;
    lock(true); status('正在预览最终配置和文件…');
    try {
    const checked = await api('/projects/import/preview', {method: 'POST', body: JSON.stringify(payload)});
    if (current !== version || !dialog.open) return;
    if (checked.workspace_resources.length !== resourceDrafts.length) {
      resourceDrafts = checked.workspace_resources; renderResourceDrafts(); payload = null;
      status('已补齐新增成员的工作区资源，请检查后继续。'); return;
    }
    payload.plan_hash = checked.file_plan.plan_hash;
    payload.request_id = [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');
    const lines = [`项目：${payload.display_name}`, `目录：${payload.root}`, `工作方式：${payload.directory_mode === 'shared' ? '共用项目主目录' : '独立 Git worktree'}`, '文件：按下方清单保存已确认的规则及补齐文件'];
    if (payload.worktree_root) lines.push('worktree 根目录：' + payload.worktree_root);
    const gitPlan = checked.file_plan.git_plan;
    if (!gitPlan && checked.file_plan.initialize_git) lines.push('Git：初始化本地仓库；首次提交和远端可后续配置');
    if (gitPlan) {
      lines.push(gitPlan.initialize ? 'Git：初始化本地仓库（main）' : 'Git：复用已有仓库');
      lines.push(gitPlan.baseline === 'existing' ? `基线：${gitPlan.head}；主目录本地改动保留` : `基线：提交下列 ${gitPlan.selected.length} 个文件；提交身份 ${gitPlan.identity}`);
      for (const path of gitPlan.selected) lines.push('提交：' + path);
    }
    const permissionNames = values => values.map(p => preview.permissions.find(v => v.key === p)?.label || p).join('、');
    const memberName = member => {
      const bot = preview.bots.find(b => b.bot_id === member.bot_id);
      return bot?.display_name || bot?.host_profile || `Bot #${member.bot_id}`;
    };
    for (const role of payload.roles) {
      const members = payload.members.filter(member => member.role === role.name).map(memberName);
      lines.push(`角色：${role.name}${role.name === ownerRole ? '\n固定职能：接收阻塞通知' : ''}\n角色权限：${permissionNames(role.permissions) || '无管理权限'}\n已选成员（${members.length}）：${members.join('、') || '未分配成员'}`);
    }
    for (const member of payload.members) {
      lines.push(`成员 ${memberName(member)}：角色 ${member.role || '未分配角色'}；个人附加权限：${permissionNames(member.permissions) || '无附加权限'}`);
    }
    const title = document.createElement('h4'); title.textContent = '确认项目与团队配置'; title.tabIndex = -1;
    const text = document.createElement('div'); text.className = 'import-review-list';
    for (const line of lines) { const item = document.createElement('p'); item.style.whiteSpace = 'pre-line'; item.textContent = line; text.append(item); }
    for (const path of checked.file_plan.directories) {
      const item = document.createElement('p'); item.textContent = '新增目录：' + path; text.append(item);
    }
    for (const file of checked.file_plan.files) {
      const item = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = `${({create:'新增', replace:'修改', preserve:'保留'})[file.action]}：${file.path}`;
      item.append(summary);
      if (file.action !== 'preserve') {
        const content = document.createElement('pre'); content.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
        content.textContent = file.diff || file.content; item.append(content);
      }
      if (file.action === 'replace') item.open = true;
      text.append(item);
    }
    if (gitPlan?.baseline === 'commit') for (const file of gitPlan.files.filter(f => f.selected)) {
      const item = document.createElement('details'), summary = document.createElement('summary'), diff = document.createElement('pre');
      summary.textContent = '提交差异：' + file.path;
      diff.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere'; diff.textContent = file.diff || '空文件或二进制内容';
      item.append(summary, diff); text.append(item);
    }
    if (payload.directory_mode === 'worktree') for (const member of payload.members) {
      const bot = preview.bots.find(b => b.bot_id === member.bot_id), item = document.createElement('p');
      item.textContent = `准备工作区：${bot.display_name || bot.host_profile}（${payload.worktree_root} 下）`; text.append(item);
    }
    const resourceSummary = document.createElement('pre'); resourceSummary.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
    const resourceLines = ['工作区资源：'];
    for (const resource of resourceDrafts) {
      const bot = preview.bots.find(b => `bot-${b.bot_id}` === resource.key);
      resourceLines.push(resource.key === 'shared' ? '共享主目录' : `独立工作区 · ${bot?.display_name || bot?.host_profile || resource.key}`);
      for (const [name, service] of Object.entries(resource.config.services)) resourceLines.push(`  ${name}：${service.host}，端口 ${service.port}`);
      const browser = resource.config.browser;
      resourceLines.push(browser ? `  浏览器：${browser.host}，调试端口 ${browser.port}\n  浏览器数据目录：${browser.data_directory}` : '  浏览器：未分配');
      resourceLines.push(`  测试数据目录：${resource.config.test_data_directory}`);
    }
    resourceSummary.textContent = resourceLines.join('\n'); text.append(resourceSummary);
    el('review').replaceChildren(title, text); showStep(6); status('确认后保存规则、登记项目并补齐所列文件。AGENTS.md 使用新模板，请检查项目约定。');
    } catch (err) { if (current === version) { payload = null; status('预览失败：' + err.message, true); } }
    finally { if (current === version) lock(false); }
  });
  function buildPayload(includeEdit = true) {
    return {root: preview.root, display_name: el('name').value.trim(), description: el('description').value,
      directory_mode: el('mode').value, worktree_root: el('worktree').value.trim(),
      roles: roles.map(r => ({...r, permissions: [...r.permissions]})),
      members: [...members.values()].map(m => ({...m, permissions: [...m.permissions]})),
      owner_bot_id: [...members.values()].find(member => member.role === ownerRole)?.bot_id ?? null,
      template: selectedTemplate, supplement: creationMode === 'new' || el('supplement').checked,
      creation_mode: creationMode, git_setup: true,
      git_baseline: el('baseline').value,
      ...(step >= 5 ? {workspace_resources: resourceDrafts} : {}),
      ...(commitFiles !== null ? {commit_files: [...commitFiles], reviewed_files: reviewedFiles} : {}),
      initialize_git: preview.git?.status === 'missing' && (el('mode').value === 'worktree' || el('git-init').checked),
      ...(includeEdit && agentsDraft ? {agents_edit: {
        content: el('agents-content').value === agentsDraft.content.replace(/\r\n?/g, '\n') ? agentsDraft.content : el('agents-content').value,
        original_sha256: agentsDraft.original_sha256}} : {})};
  }
  el('submit').addEventListener('click', async () => {
    if (busy || !payload || step !== 6) return;
    const current = ++version; lock(true); status('正在保存规则、准备 Git、配置团队和工作区…');
    submittedRequest = payload.request_id;
    let polling = false;
    const timer = setInterval(async () => {
      if (polling || current !== version || !dialog.open) return;
      polling = true;
      try {
        const progress = await api('/projects/setup-progress/' + payload.request_id);
        if (current === version && dialog.open && progress.status === 'running') status(progress.phase);
      } catch { /* The submit response remains authoritative. */ }
      finally { polling = false; }
    }, 1500);
    try {
      const created = await api('/projects/import', {method: 'POST', body: JSON.stringify(payload)});
      if (current !== version || !dialog.open) return;
      // Keep the committed ID available if refreshing the project list fails.
      el('submit').hidden = true; el('back').hidden = true;
      existingProjects([{name: created.project, display_name: payload.display_name}]);
      if (creationMode === 'new' && !created.members?.length) status('项目已创建，待配置团队。打开项目后可配置成员和负责人。');
      else await openProject(created.project);
      if (created.review_reminder) showAlert(created.review_reminder);
    } catch (err) {
      if (current !== version) return;
      if (err.detail?.code === 'directory_registered') {
        el('submit').hidden = true; el('back').hidden = true;
        existingProjects(err.detail.existing_projects);
      }
      status('导入结果：' + err.message + '。', true);
    } finally { clearInterval(timer); if (dialog.open && current === version) { lock(false); el('progress').hidden = false; } }
  });
  async function queryResult() {
    if (!payload?.request_id) return;
    const current = version; lock(true);
    try {
      const progress = await api('/projects/setup-progress/' + payload.request_id);
      if (current !== version || !dialog.open) return;
      if (progress.status === 'ready') {
        el('submit').hidden = true; el('back').hidden = true;
        existingProjects([{name: progress.result.project, display_name: payload.display_name}]);
        await openProject(progress.result.project);
        if (progress.result.review_reminder) showAlert(progress.result.review_reminder);
      } else {
        status(progress.status === 'failed' ? `上次操作失败：${progress.error}。返回协作方式重新检查后，可预览并重试。` : progress.phase, progress.status === 'failed');
      }
    } catch (err) { if (current === version) status(err.message, true); }
    finally { if (current === version) lock(false); }
  }
  el('progress').addEventListener('click', queryResult);
})();
