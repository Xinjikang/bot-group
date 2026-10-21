const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const slice = (a, b) => {
  const start = html.indexOf(a), end = html.indexOf(b, start);
  assert.ok(start >= 0 && end > start, `Missing script boundary: ${a} / ${b}`);
  return html.slice(start, end);
};
const elements = new Map();
const storage = new Map();
const element = selector => {
  if (!elements.has(selector)) elements.set(selector, {value: '', dataset: {}, children: [],
    addEventListener(type, handler) { this[type] = handler; },
    querySelector(sel) { return this.querySelectorList(sel)[0] || null; },
    querySelectorList(sel) {
      const cls = String(sel).replace('.', '');
      const out = [];
      const walk = n => { if (String(n.className||'').split(' ').includes(cls)) out.push(n); (n.children||[]).forEach(walk); };
      walk(this); return out;
    },
    appendChild(n) { this.children.push(n); n.parentElement = this; return n; },
    // innerHTML 粗解析 class，让 querySelector('.req-title') 能命中
    set innerHTML(v) {
      this.children = [];
      const re = /<(\w+)[^>]*class="([^"]*)"[^>]*>/g; let m;
      const walkFn = this.querySelectorList;
      while ((m = re.exec(v))) {
        const child = {className: m[2], children: [], dataset: {}, addEventListener() {},
          appendChild(x) { this.children.push(x); return x; },
          querySelectorList(sel) {
            const cls = String(sel).replace('.', '');
            const out = [];
            const walk = n => { if (String(n.className||'').split(' ').includes(cls)) out.push(n); (n.children||[]).forEach(walk); };
            walk(this); return out;
          }};
        this.children.push(child);
      }
      this._html = v;
    },
    get innerHTML() { return this._html || ''; },
    querySelectorAll(sel) { return this.querySelectorList(sel); },
    remove() {}, focus() {}, removeAttribute() {},
    scrollTop: 0, scrollHeight: 0, clientHeight: 0, isConnected: true,
    replaceChildren(...options) {this.options=options;}, add(option) {this.options.push(option);}});
  return elements.get(selector);
};
let requests = [], pending = [], redraws = [];
const ctx = {S: {currentProject: 'xjk', currentThread: 42, projects: [{name:'xjk'}, {name:'cicp'}],
  threads: [{id:42, project:'xjk'}], thinkRuns:new Map(), msgs:[{id:1, project:'xjk'}],
  messageView:0, messageRequest:null, messagesReady:true, lastMsgId:1},
  localStorage: {getItem:key=>storage.get(key), setItem:(key,value)=>storage.set(key,value), removeItem:key=>storage.delete(key)},
  $: element, esc: s => s, closeProjectDrop() {}, toggleProjectDrop() {}, openProjectDetail() {},
  document: {querySelectorAll: () => [], dispatchEvent() {}},
  Event: class Event { constructor(type) { this.type = type; } },
  Option: function(text,value) {this.text=text;this.value=value;},
  resetPjAgentsTab() {}, selectProjectTab() {}, openDialogAnchored() {},
  resetProjectPermissions() {},   // 1faa2f2 在 openProjectDetail 里新增调用（project_settings.js），vm 切片须 stub
  renderThreads() {}, updateComposerState() {}, loadTasks() {}, mpHide() {},
  composerImages: {refresh() {}},
  loadProjects: async () => {}, loadThreads: async () => {}, closeDialogAnchored() {},
  loadMembers: async () => {},
  refreshUserDisplayName: async () => {},
  showAlert: message => {throw Error(message);},
  renderMessages() {redraws.push(ctx.S.msgs.map(m=>m.id));},
  api(url, options) {requests.push({url,options}); return new Promise((resolve, reject) => pending.push({resolve, reject}));},
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('frontend/member_identity.js', 'utf8'), ctx);
vm.runInContext(slice('function restoreCurrentProject()', 'function renderProjectBar()'),ctx);
vm.runInContext(slice('function renderProjectBar()', 'async function toggleProjectDrop()'),ctx);
vm.runInContext(slice('async function refreshMessages()', 'function captureThinkingScroll('),ctx);
vm.runInContext(slice('function selectGroup()', '// ======================== 任务池投影'),ctx);
vm.runInContext(slice('$("#pj-detail-form").addEventListener("submit"', 'async function loadInboxSummary()'),ctx);
(async () => {
  ctx.restoreCurrentProject();
  assert.equal(ctx.S.currentProject,'xjk','first visit selects a concrete default');
  storage.set('group-engine.current-project','cicp');
  ctx.restoreCurrentProject();
  assert.equal(ctx.S.currentProject,'cicp','refresh restores selected project');
  storage.set('group-engine.current-project','archived');
  ctx.restoreCurrentProject();
  assert.equal(ctx.S.currentProject,'xjk','missing project falls back to active project');
  ctx.renderProjectBar();
  assert.ok(element('#project-bar').innerHTML.includes('data-project="xjk"'));
  assert.ok(!element('#project-bar').innerHTML.includes('data-project="cicp"'));
  const old = ctx.refreshMessages();
  ctx.selectProject('cicp');
  assert.equal(storage.get('group-engine.current-project'),'cicp');
  assert.equal(ctx.S.currentThread,null);
  assert.equal(ctx.S.lastMsgId,0);
  assert.equal(redraws.at(-1).length,0,'old messages disappear immediately');
  assert.ok(element('#project-bar').innerHTML.includes('data-project="cicp"'));
  assert.ok(!element('#project-bar').innerHTML.includes('data-project="xjk"'));
  assert.equal(requests.at(-1).url,'/messages?limit=100&project=cicp');
  pending.shift().resolve({messages:[{id:2, project:'xjk'}]});
  await old;
  assert.equal(ctx.S.msgs.length,0,'old project response discarded');
  pending.shift().resolve({messages:[{id:3, project:'cicp'}],has_more:true});
  await new Promise(setImmediate);
  assert.equal(ctx.S.msgs[0].project,'cicp');
  assert.equal(ctx.bubbleInCurrentProject({thread_id:42}),false);
  assert.equal(ctx.bubbleInCurrentProject({profile:'bot',session:'bot-xjk-work'}),false);
  assert.equal(ctx.bubbleInCurrentProject({profile:'bot',session:'bot-cicp-work'}),true);
  ctx.S.currentProject='项目 & A';
  assert.equal(ctx.messageProjectUrl('/messages?limit=100'),'/messages?limit=100&project=%E9%A1%B9%E7%9B%AE%20%26%20A');
  const form=element('#pj-detail-form');
  ctx.S.members=[{profile:'user',display_name:'User'},{profile:'lead-a',display_name:'Lead A'}];
  ctx.S.projects.find(p=>p.name==='cicp').owner_profile='lead-a';
  vm.runInContext(slice('function openProjectDetail(', 'let pjDetailFromDrop ='),ctx);
  ctx.openProjectDetail('cicp');
  assert.equal(element('#pj-d-owner').value,'lead-a');
  assert.deepEqual(element('#pj-d-owner').options.map(o=>o.value),['','lead-a']);
  ctx.openProjectDetail(null);
  assert.equal(element('#pj-d-owner').value,'');
  element('#pj-d-owner').value='lead-a';
  form.dataset={isNew:'1',origName:''};
  element('#pj-d-name').value='cicp';
  element('#pj-d-id').value='cicp';
  element('#pj-d-root').value='E:/Code/cicp';
  element('#pj-d-desc').value='首次创建描述';
  const submit=form.submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(requests.at(-1).options.body),{display_name:'cicp',root:'E:/Code/cicp',description:'首次创建描述',owner_profile:'lead-a',directory_mode:'shared',worktree_root:''});
  pending.shift().resolve({project:'cicp'});
  await submit;
  pending.shift().resolve({messages:[],has_more:false});
  await new Promise(setImmediate);
  assert.equal(ctx.S.currentProject,'cicp');
  let releaseProjects;
  const loads = [];
  ctx.S.currentProject = null;
  storage.set('group-engine.current-project','cicp');
  ctx.loadProjects = () => new Promise(resolve => { releaseProjects = resolve; });
  for (const name of ['loadThreads','loadInboxSummary','loadTasks','refreshMessages','loadThinking']) {
    ctx[name] = async () => { loads.push([name, ctx.S.currentProject]); };
  }
  ctx.showLogin = ctx.restoreThinkingState = ctx.connectSSE = ctx.loadMembers = ctx.setInterval = () => {};
  vm.runInContext(slice('async function boot()', '// Refresh unfinished collectors'),ctx);
  const boot = ctx.boot();
  await new Promise(setImmediate);
  assert.equal(loads.length,0,'no view data loads before project list resolves');
  releaseProjects();
  await boot;
  assert.equal(loads.length,5);
  assert.ok(loads.every(([,project]) => project === 'cicp'),'all panels boot with the same restored project');
  // ---- req#76 task#130：归档按钮 + 显示已归档开关 ----
  // boot 时 loadTasks 被 stub 掉了，先 vm 注入真实 loadTasks/renderTasks/archiveRequirement
  vm.runInContext(slice('async function loadTasks()', 'const STALE_MS'),ctx);
  vm.runInContext(slice('const STALE_MS', 'function renderTasks('),ctx);
  vm.runInContext(slice('function renderTasks(', 'async function adminTaskAction('),ctx);
  ctx.S.showArchived = false;
  ctx.S.isUserIdentity = true;
  const renderTasks = [];
  const realRenderTasks = ctx.renderTasks;
  ctx.renderTasks = (...args) => { renderTasks.push(args); };
  // loadTasks: showArchived=false → 不带 include_archived；归档需求被前端兜底过滤
  const reqCountBefore = requests.length;
  const tasksLoad = ctx.loadTasks();
  assert.equal(requests[reqCountBefore].url,'/tasks','showArchived=false requests plain /tasks');
  pending.shift().resolve({requirements:[
    {id:1, project:'cicp', status:'open', archived:0},
    {id:2, project:'cicp', status:'done', archived:1},
    {id:3, project:'cicp', status:'done', archived:0},
  ], tasks:[{id:10, project:'cicp', status:'done', requirement_id:3}], pending_approval:[]});
  await tasksLoad;
  assert.deepEqual(renderTasks.at(-1)[0].map(r=>r.id),[1,3],'archived requirement hidden by default');
  // showArchived=true → 带 include_archived=1 且归档需求放行
  ctx.S.showArchived = true;
  const archivedReqIdx = requests.length;
  const archivedLoad = ctx.loadTasks();
  assert.equal(requests[archivedReqIdx].url,'/tasks?include_archived=1','showArchived=true requests full list');
  pending.shift().resolve({requirements:[
    {id:1, project:'cicp', status:'open', archived:0},
    {id:2, project:'cicp', status:'done', archived:1},
  ], tasks:[], pending_approval:[]});
  await archivedLoad;
  assert.deepEqual(renderTasks.at(-1)[0].map(r=>r.id),[1,2],'archived shown when toggle on');
  // bot 身份：请求同样拉全量（数据可见），但按钮不渲染
  ctx.S.isUserIdentity = false;
  const botLoad = ctx.loadTasks();
  pending.shift().resolve({requirements:[{id:2, project:'cicp', status:'done', archived:1}], tasks:[], pending_approval:[]});
  await botLoad;
  assert.deepEqual(renderTasks.at(-1)[0].map(r=>r.id),[2],'bot still fetches archived data (no filter by identity)');
  // 归档/取消归档按钮渲染规则（真实 renderTasks 跑一遍）：user 身份 + 历史区 + done 需求 → 「归档」
  // archived=1 → 「取消归档」；bot 身份 → 无按钮；开关行同样只给 user
  ctx.S.isUserIdentity = true;
  ctx.renderTasks = realRenderTasks;
  ctx.document = {createDocumentFragment: () => element('#frag-' + Math.random()),
    createElement: () => element('__tmp_' + Math.random()), addEventListener() {}};
  const reqDone = {id: 7, project: 'cicp', status: 'done', archived: 0};
  const reqArch = {id: 8, project: 'xjk', status: 'cancelled', archived: 1};
  const taskDone = {id: 70, project: 'cicp', status: 'done', requirement_id: 7};
  const taskArch = {id: 80, project: 'xjk', status: 'cancelled', requirement_id: 8};
  ctx.renderTasks([reqDone, reqArch], [taskDone, taskArch], []);
  const taskBox = element('#task-list');
  const btns = taskBox.querySelectorAll('req-archive-btn');
  assert.equal(btns.length, 2, 'user identity: archive buttons on both done/cancelled requirements');
  // 2c663ec：历史区需求按 id 倒序渲染 → req8 在前
  assert.equal(btns[0].dataset.req, 8);
  assert.equal(btns[0].textContent, '取消归档', 'archived requirement renders unarchive label');
  assert.equal(btns[1].dataset.req, 7);
  assert.equal(btns[1].textContent, '归档', 'unarchived requirement renders archive label');
  assert.equal(taskBox.querySelectorAll('archive-toggle').length, 1, 'show-archived toggle rendered for user');
  assert.equal(taskBox.querySelectorAll('tad-view').length, 2, 'completed and cancelled tasks both have a view button');
  ctx.S.isUserIdentity = false;
  ctx.renderTasks([reqDone, reqArch], [taskDone, taskArch], []);
  assert.equal(element('#task-list').querySelectorAll('req-archive-btn').length, 0, 'bot identity renders no archive button');
  assert.equal(element('#task-list').querySelectorAll('archive-toggle').length, 0, 'bot identity renders no toggle');
  assert.equal(element('#task-list').querySelectorAll('tad-view').length, 2, 'history remains readable without admin controls');
  // archiveRequirement：POST archive/unarchive；失败时 alert 且不静默
  // archiveRequirement 成功路径会调真实 loadTasks() -> api('/tasks') 顶进 pending 队列，
  // 把后面 pending.shift() 弄串（unarchP 永远等不到 resolve）——archive 测试期间 stub 掉。
  vm.runInContext(slice('async function archiveRequirement(', 'async function adminTaskAction('),ctx);
  ctx.loadTasks = () => {};
  ctx.showConfirm = async () => true;
  ctx.showAlert = () => {};
  ctx.S.isUserIdentity = true;
  ctx.S.currentProject = 'cicp';
  // 2c663ec 倒序后 btns[0]=req8(xjk,取消归档)、btns[1]=req7(cicp,归档)
  const archP = btns[1].click();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1).url, '/admin/requirement/7/archive');
  assert.equal(requests.at(-1).options.method, 'POST');
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {project: 'cicp'}, 'archive sends the clicked requirement project');
  pending.shift().resolve({archived: 1, requirement: 7});
  await archP;
  ctx.S.currentProject = 'xjk';
  const unarchP = btns[0].click();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1).url, '/admin/requirement/8/unarchive');
  assert.equal(requests.at(-1).options.method, 'POST');
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {project: 'xjk'}, 'unarchive uses the clicked requirement project after a project switch');
  pending.shift().resolve({archived: 0, requirement: 8});
  await unarchP;
  // 失败路径：api reject -> catch 调 alert（不静默）。alert 用 spy 验证被调到。
  const alerts = [];
  ctx.showAlert = (m) => { alerts.push(m); };
  const failP = ctx.archiveRequirement('8', true, 'xjk');
  await new Promise(setImmediate);
  pending.shift().reject(new Error('backend rejects'));
  await failP;
  assert.deepEqual(alerts, ['归档失败: backend rejects'], 'api failure surfaces via alert');
  console.log('PASS: req#76 archived toggle url, fallback filter, include_archived on');
  console.log('PASS: current chip, immediate reset, scoped request, stale response, reasoning scope, create description');
})().catch(e=>{console.error(e);process.exitCode=1;});
