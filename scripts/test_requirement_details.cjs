const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children = []; this.events = {}; this.style = {}; this.open = false; this.parts = {}; }
  setAttribute() {}
  appendChild(n) { this.children.push(n); }
  replaceChildren() { this.children = []; }
  querySelector(q) { return this.parts[q] ||= new Element(); }
  addEventListener(e, f) { this.events[e] = f; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
}
const body = new Element(), calls = [], pending = [];
const ctx = {document:{body, createElement:() => new Element()}, window:{}, encodeURIComponent,
  api:url => { calls.push(url); return new Promise((resolve,reject) => pending.push({resolve,reject})); },
  openTaskHistory:() => {}};
vm.runInNewContext(fs.readFileSync('frontend/requirement_details.js','utf8'), ctx);
const dialog = body.children[0], content = dialog.querySelector('[data-content]');
const flatten = n => [n, ...n.children.flatMap(flatten)];
const packet = id => ({requirement:{id,project:'a/b',title:'<img onerror=alert(1)>',description:'正文\n'+'x'.repeat(12000)},
  tasks:[{id:7,title:'task',status:'pending',prerequisites:'[6]',dependency_blockers:[{id:6,title:'前置',status:'blocked'}]}]});
(async () => {
  const old = ctx.window.openRequirementDetails(1,'a/b');
  const fresh = ctx.window.openRequirementDetails(2,'a/b');
  pending[1].resolve(packet(2)); await fresh;
  pending[0].resolve(packet(1)); await old;
  assert.equal(calls[0], '/requirements/1?project=a%2Fb');
  assert.ok(dialog.querySelector('h3').textContent.startsWith('需求 #2'));
  assert.ok(flatten(content).some(n => n.textContent === packet(2).requirement.description));
  assert.ok(flatten(content).some(n => n.textContent?.includes('已阻塞')));
  assert.ok(flatten(content).every(n => !n.innerHTML));
  const failed = ctx.window.openRequirementDetails(3,'a/b');
  pending[2].reject(new Error('offline')); await failed;
  assert.ok(flatten(content).some(n => n.textContent === '加载失败：offline'));
  const closing = ctx.window.openRequirementDetails(4,'a/b');
  dialog.close(); pending[3].resolve(packet(4)); await closing;
  assert.equal(dialog.open, false);
  const structured = ctx.window.openRequirementDetails(5,'a/b');
  const data = packet(5);
  Object.assign(data.requirement, {background_goal:'背景\n目标', scope:'范围',
    approach_basis:'方案依据', acceptance:'验收标准', undecided:'无',
    source_ref:'thread_id=10082; message_id=5600 <script>bad</script>'});
  pending[4].resolve(data); await structured;
  for (const key of ['title','background_goal','scope','approach_basis','acceptance','undecided','source_ref']) {
    assert.ok(flatten(content).some(n => n.textContent === data.requirement[key]));
  }
  assert.ok(flatten(content).every(n => !n.innerHTML));
  const empty = ctx.window.openRequirementDetails(6,'a/b');
  pending[5].resolve({requirement:{id:6,project:'a/b',title:'空值',scope:'  ',source_ref:null},tasks:[]}); await empty;
  for (const heading of ['背景与目标','范围','方案与依据','验收标准','未决事项','来源关联']) {
    assert.ok(flatten(content).some(n => n.textContent === `暂未填写${heading}`));
  }
  console.log('PASS: requirement scope, full text, safe rendering, dependency reason, stale response, error and close');
})().catch(error => { console.error(error); process.exitCode = 1; });
