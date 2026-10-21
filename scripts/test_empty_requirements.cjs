// Run: node scripts/test_empty_requirements.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
class Element {
  constructor() { this.children = []; this.scrollTop = 0; this.dataset = {}; }
  set innerHTML(value) { this.markup = value; this.children = []; }
  appendChild(node) { this.children.push(node); }
  querySelector() { return this; }
  querySelectorAll() { return []; }
  addEventListener() {}
}
const box = new Element();
const ctx = {S: {currentProject: 'cicp'}, $: () => box, esc: String,
  NAME_OF: String, document: {createElement: () => new Element(), createDocumentFragment: () => new Element()}};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('const STALE_MS ='), html.indexOf('async function adminTaskAction(')), ctx);
const flatten = node => [node, ...node.children.flatMap(flatten)];
const req = {id: 12, project: 'cicp', title: '多机构支持', version: 'v0.2', status: 'open'};
ctx.renderTasks([req], [], []);
let nodes = flatten(box);
assert.equal(nodes.filter(n => n.className === 'req-block').length, 1);
assert.ok(nodes.some(n => n.textContent === '待拆分 · 0 个任务'));
assert.ok(nodes.some(n => n.textContent === '需求已创建，等待拆分任务'));
assert.ok(!nodes.some(n => n.markup?.includes('任务池为空')));
ctx.renderTasks([req], [{id: 1, requirement_id: 12, status: 'done'}], []);
nodes = flatten(box);
assert.ok(!nodes.some(n => n.textContent === '待拆分 · 0 个任务'));
assert.equal(nodes.filter(n => n.className === 'req-block').length, 1, 'history group remains without duplicate pending group');
ctx.renderTasks([{...req, status: 'done'}], [], []);
assert.equal(flatten(box).filter(n => n.className === 'req-block').length, 1, 'closed empty requirement remains visible to bots');
assert.equal(flatten(box).filter(n => n.className === 'task-history').length, 1);
ctx.S.isUserIdentity = true;
ctx.renderTasks([req], [], []);
assert.equal(flatten(box).filter(n => n.textContent === '关闭需求').length, 1);
for (const status of ['done', 'cancelled']) {
  for (const archived of [false, true]) {
    ctx.renderTasks([{...req, status, archived}], [], []);
    nodes = flatten(box);
    if (status === 'cancelled') {
      assert.equal(nodes.filter(n => n.className === 'req-block').length, 0, 'withdrawn requirements without tasks stay hidden');
      assert.equal(nodes.filter(n => n.className === 'req-archive-btn').length, 0);
      continue;
    }
    assert.equal(nodes.filter(n => n.className === 'req-block').length, 1);
    assert.ok(nodes.some(n => n.textContent === '历史 0'));
    assert.ok(!nodes.some(n => n.textContent === '待拆分 · 0 个任务'));
    const archive = nodes.filter(n => n.className === 'req-archive-btn');
    assert.equal(archive.length, 1);
    assert.equal(archive[0].textContent, archived ? '取消归档' : '归档');
    assert.equal(archive[0].dataset.req, req.id);
    assert.equal(archive[0].dataset.project, 'cicp');
    assert.equal(nodes.filter(n => n.textContent === '关闭需求').length, 0);
  }
}
ctx.S.isUserIdentity = false;
ctx.renderTasks([{...req, status: 'cancelled'}], [], []);
assert.equal(flatten(box).filter(n => n.className === 'req-block').length, 0, 'withdrawn empty requirements stay hidden for bots');
ctx.renderTasks([req, {id: 13, title: '登录页', status: 'open'}],
  [{id: 2, requirement_id: 12, status: 'pending'}], []);
assert.equal(flatten(box).filter(n => n.className === 'req-block').length, 2, 'active tasks and undecomposed requirements coexist');
console.log('PASS: empty requirements, close/history/archive recovery, bot visibility and mixed task groups');
