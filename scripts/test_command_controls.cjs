// Isolated command picker and member reset behavior; no network or bot execution.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const catalog = [
  {name:'/new', description:'Reset session', requires_target:true},
  {name:'/help', description:'Help', requires_target:false},
  {name:'/stop', description:'Stop current session', requires_target:true},
  {name:'/steer', description:'Steer channel pending', requires_target:true},
];
function input(value = '/') {
  const handlers = {};
  return {value, selectionStart:value.length, selectionEnd:value.length, isConnected:true,
    handlers, addEventListener(name, fn) { handlers[name] = fn; }, focus() {},
    getBoundingClientRect: () => ({left:10, top:500, bottom:550})};
}
const popup = {hidden:true, innerHTML:'', style:{}, offsetHeight:120, offsetWidth:320,
  querySelectorAll: () => []};
let response = {commands:catalog}, apiError = null, calls = [], mentions = [], alerts = [];
const ctx = vm.createContext({
  $: () => popup, esc:String, window:{innerWidth:800, innerHeight:600},
  mpHide() {}, mpDetect: (ta, threadId) => mentions.push([ta.value, threadId]),
  api: async (path, options) => {
    calls.push({path, options});
    if (apiError) throw apiError;
    return response;
  },
  showAlert: async text => alerts.push(text),
});
vm.runInContext(fs.readFileSync('frontend/commands.js', 'utf8'), ctx);
const settings = fs.readFileSync('frontend/project_settings.js', 'utf8');
vm.runInContext(settings.slice(settings.indexOf('function sessionResetButton('), settings.indexOf('function renderProjectMembers(')), ctx);
assert.equal(ctx.sessionResetButton({active:0, work_session_exists:true}).disabled, false);
assert.equal(ctx.sessionResetButton({active:1, work_session_exists:false}).label, '暂无会话');
assert.equal(ctx.sessionResetButton({active:0, work_session_exists:null}).disabled, true);
vm.runInContext(settings.slice(settings.indexOf('const resettingMemberSessions'),
  settings.indexOf('function openWorkspaceEditor')), ctx);
const tick = () => new Promise(resolve => setImmediate(resolve));
function key(ta, name, isComposing = false) {
  const event = {key:name, isComposing, prevented:false, stopped:false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }};
  ta.handlers.keydown(event);
  return event;
}
(async () => {
  const main = input(), inline = input('/ne');
  ctx.bindCommandInput(main, () => null);
  await ctx.detectCommand(main, null);
  assert.equal(popup.hidden, false);
  assert(popup.innerHTML.includes('/new') && popup.innerHTML.includes('/help'));
  assert.equal(calls[0].path, '/commands');
  assert.equal(key(main, 'Enter', true).prevented, false, 'IME confirmation stays with IME');
  const selected = key(main, 'Enter');
  assert(selected.prevented && selected.stopped, 'selection never submits the form');
  assert.equal(main.value, '/new @');
  assert.deepEqual(mentions.pop(), ['/new @', null]);
  assert.equal(calls.length, 1, 'choosing a command only inserts text');
  await ctx.detectCommand(inline, 12);
  assert(!popup.innerHTML.includes('/help'), 'prefix filters menu');
  ctx.selectCommand(0);
  assert.equal(inline.value, '/new @');
  assert.deepEqual(mentions.pop(), ['/new @', 12], 'target menu stays in inline topic');
  main.value = '/'; main.selectionStart = main.selectionEnd = 1;
  await ctx.detectCommand(main, null);
  key(main, 'ArrowDown'); key(main, 'Tab');
  assert.equal(main.value, '/help ');
  for (const command of ['/stop', '/steer']) {
    const target = input(command);
    await ctx.detectCommand(target, 12);
    ctx.selectCommand(0);
    assert.equal(target.value, command + ' @');
    assert.deepEqual(mentions.pop(), [command + ' @', 12]);
  }
  main.value = '/'; main.selectionStart = main.selectionEnd = 1;
  await ctx.detectCommand(main, null);
  key(main, 'Escape');
  assert.equal(popup.hidden, true);
  const ordinary = input('hello /');
  await ctx.detectCommand(ordinary, null);
  assert.equal(popup.hidden, true, 'embedded slash stays ordinary text');
  vm.runInContext('commandCatalog = null', ctx);
  let resolveCatalog;
  ctx.api = () => new Promise(resolve => { resolveCatalog = resolve; });
  const pending = ctx.detectCommand(main, null);
  ctx.hideCommandPicker();
  resolveCatalog({commands:catalog});
  await pending;
  assert.equal(popup.hidden, true, 'late response cannot reopen a blurred menu');
  vm.runInContext('commandCatalog = null', ctx);
  ctx.api = async () => { throw new Error('offline'); };
  await ctx.detectCommand(main, null);
  assert(popup.innerHTML.includes('命令读取失败'));
  ctx.hideCommandPicker();

  let finishReset;
  ctx.api = async (path, options) => {
    calls.push({path, options});
    return new Promise(resolve => { finishReset = resolve; });
  };
  const button = {disabled:false, textContent:'重置会话'};
  const before = calls.length;
  const resetting = ctx.resetMemberSession('project-detail', 'default', button);
  await tick();
  assert(button.disabled);
  await ctx.resetMemberSession('project-detail', 'default', button);
  assert.equal(calls.length, before + 1, 'duplicate reset stays blocked');
  assert.equal(calls.at(-1).path, '/projects/project-detail/members/default/reset-session');
  assert.equal(calls.at(-1).options.method, 'POST');
  assert.equal(calls.at(-1).options.body, undefined, 'scope is fixed by the admin endpoint');
  finishReset({handled:true, ok:true, detail:'@default 的工作会话已切换。'});
  await resetting;
  assert.equal(alerts.at(-1), '@default 的工作会话已切换。');
  assert.equal(button.disabled, false);
  ctx.api = async () => ({handled:true, ok:false, detail:'目标忙碌'});
  await ctx.resetMemberSession('p', 'default', button);
  assert.equal(alerts.at(-1), '目标忙碌');
  assert.equal(button.textContent, '重置会话');
  ctx.api = async () => { throw new Error('network failed'); };
  await ctx.resetMemberSession('p', 'default', button);
  assert.equal(alerts.at(-1), 'network failed');
  assert.equal(button.disabled, false);
  const status = {isConnected:true, hidden:true, textContent:''};
  const alertCount = alerts.length;
  ctx.api = async () => ({handled:true, ok:true, detail:'reset complete'});
  await ctx.resetMemberSession('p', 'default', button, status);
  assert.equal(status.textContent, 'reset complete');
  assert.equal(status.hidden, false);
  assert.equal(alerts.length, alertCount, 'member row feedback keeps project details open');
  const html = fs.readFileSync('frontend/index.html', 'utf8');
  vm.runInContext(html.slice(html.indexOf('async function stopBot('), html.indexOf('const themeBtn')), ctx);
  const targetRun = {profile:'bot', project:'p', thread_id:12};
  const otherRun = {profile:'bot', project:'q', thread_id:12};
  const workRun = {profile:'bot', project:'p', thread_id:null};
  ctx.S = {thinkRuns:new Map([['target',targetRun],['other',otherRun],['work',workRun]]), thinkBubbles:new Map()};
  ctx.showConfirm = async () => true;
  ctx.renderThinkBubbles = () => {};
  ctx.loadThinking = () => {};
  let stopBody;
  ctx.api = async (path, options) => { assert.equal(path, '/stop'); stopBody = JSON.parse(options.body); return {stopped:true}; };
  await ctx.stopBot('bot', 'p', 12);
  assert.deepEqual(stopBody, {profile:'bot', project:'p', thread_id:12});
  assert.equal(targetRun.stopped, true);
  assert.equal(otherRun.stopped, undefined);
  assert.equal(workRun.stopped, undefined);
  console.log('PASS: command menu, member reset feedback, scoped stop request and thinking state');
})().catch(error => { console.error(error); process.exitCode = 1; });
