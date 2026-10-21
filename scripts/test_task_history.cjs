const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const elements = new Map();
const element = key => {
  if (!elements.has(key)) elements.set(key, {
    dataset: {}, open: false, textContent: '', innerHTML: '',
    addEventListener(type, fn) { this[type] = fn; },
    showModal() { this.open = true; },
    close() { this.open = false; this.closeEvent?.(); },
  });
  return elements.get(key);
};
const requests = [], pending = [];
const ctx = {
  $: element, NAME_OF: name => name === 'dev' ? '沈清' : name,
  api(url) { requests.push(url); return new Promise((resolve, reject) => pending.push({resolve, reject})); },
};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('const esc ='), html.indexOf('async function api(')), ctx);
vm.runInContext(html.slice(html.indexOf('let taskHistoryRequest ='), html.indexOf('// ======================== 任务池栏拖拽')), ctx);
const dlg = element('#dlg-task-history');
// Preserve the native close() method alongside the registered close event in this fixture.
dlg.closeEvent = dlg.close;
dlg.close = function() { this.open = false; this.closeEvent(); };
const data = (id, history = []) => ({task: {id, project:'项目 & A', title:'<img src=x onerror=alert(1)>', status:'done'}, history});

(async () => {
  const first = ctx.openTaskHistory(130, '项目 & A');
  assert.equal(requests[0], '/tasks/130/history?project=%E9%A1%B9%E7%9B%AE%20%26%20A');
  assert.equal(dlg.open, true);
  const second = ctx.openTaskHistory(131, 'beta');
  pending.shift().resolve(data(130));
  await first;
  assert.equal(element('#task-history-title').textContent, '任务 #131', 'late response cannot replace another task');
  pending.shift().resolve(data(131, [
    {ts:'2026-09-23T11:00:59',actor:'dev',type:'execution.failed',detail:JSON.stringify({failure:1,error:'<script>bad()</script>'}),meta:'{}'},
    {ts:'2026-09-23T11:06:57',actor:'dev',type:'dev',detail:'交付',result:'pass',meta:'{"commit":"abc123"}'},
    {ts:'2026-09-23T11:10:45',actor:'default',type:'complete',detail:'完成'},
  ]));
  await second;
  const body = element('#task-history-body');
  assert.ok(body.innerHTML.includes('沈清'));
  assert.ok(body.innerHTML.includes('11:00:59'));
  assert.ok(body.innerHTML.includes('开发交付'));
  assert.ok(body.innerHTML.includes('abc123'));
  assert.ok(body.innerHTML.includes('&lt;script&gt;bad()&lt;/script&gt;'));
  assert.ok(!body.innerHTML.includes('<script>'));
  assert.ok(!body.innerHTML.includes('failure'), 'retry counters stay out of the process summary');
  assert.ok(element('#task-history-summary').textContent.includes('<img'), 'title is assigned as text');

  const third = ctx.openTaskHistory(132, 'alpha');
  dlg.close();
  pending.shift().resolve(data(132));
  await third;
  assert.ok(body.innerHTML.includes('加载中'), 'response after closing is discarded');
  const failed = ctx.openTaskHistory(133, 'alpha');
  pending.shift().reject(new Error('<bad> unavailable'));
  await failed;
  assert.ok(body.innerHTML.includes('加载失败：&lt;bad&gt; unavailable'));
  element('#task-history-refresh').click();
  assert.equal(requests.at(-1), '/tasks/133/history?project=alpha');
  pending.shift().resolve(data(133));
  await new Promise(setImmediate);
  assert.ok(body.innerHTML.includes('暂无过程记录'));
  element('#task-history-close').click();
  assert.equal(dlg.open, false);
  assert.ok(!html.includes('id="fail-chip"'));
  ctx.renderTaskHistory({task:{...data(134).task, description:'<script>text</script>\n'+'正文'.repeat(6000),
    design_basis:'doc/design.md section 2',acceptance_criteria:'全部场景通过',prerequisites:'[147,148]',briefing_status:'ready'},history:[]});
  const summary = element('#task-history-summary').textContent;
  assert.ok(summary.includes('正文'.repeat(6000)), 'full description is preserved');
  assert.ok(summary.includes('<script>text</script>'), 'description uses textContent');
  assert.ok(summary.includes('doc/design.md section 2'));
  assert.ok(summary.includes('全部场景通过'));
  assert.ok(summary.includes('#147、#148'));
  assert.ok(summary.includes('已发布'));
  console.log('PASS: task history scope, stale/closed responses, escaping, timeline, errors and refresh');
})().catch(e => { console.error(e); process.exitCode = 1; });
