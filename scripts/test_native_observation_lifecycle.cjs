// Run: node scripts/test_native_observation_lifecycle.cjs
// #277 验收 5 + node1715：消费隔离 API 实际生成的 /thinking 与 /reasoning 响应
// （scripts/native_observation_payloads.py 经真实 api.get_thinking/get_reasoning 产生，
// 覆盖 t1000 busy→t1005 idle→t1006 busy→t1010 idle 反例时序），驱动现有
// loadThinking/startThinkBubble/applyExecutionState/pollReasoning 生产函数（vm 执行），
// 验证：观察条目归属、旧批折叠（迟到 /reasoning 终态 1005）时新批仍活跃、新批结束折叠。
// 渲染为桩（无真实 DOM），无网络请求；payload 采集走隔离 API（临时 SQLite + 打桩状态）。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const child = require('node:child_process');
const html = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'index.html'), 'utf8');
const section = (from, to) => html.slice(html.indexOf(from), html.indexOf(to, html.indexOf(from)));
function harness() {
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const ctx = {
    S: {thinkRuns: new Map(), thinkBubbles: new Map()},
    console, Date, Map, Symbol, Number, JSON, encodeURIComponent,
    setTimeout(fn, delay) { timers.set(++timerId, {fn, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); }, renderMessages() {},
    window: {addEventListener() {}},
    localStorage: {getItem: () => null, setItem() {}},
    snapshot: {thinking: []},
    api(url) {
      if (url === '/thinking') return ctx.snapshot instanceof Error ? Promise.reject(ctx.snapshot) : Promise.resolve(ctx.snapshot);
      return new Promise((resolve, reject) => requests.push({url, resolve, reject}));
    },
  };
  vm.createContext(ctx);
  vm.runInContext(section('function startThinkBubble(', '// 当前视图相关'), ctx);
  vm.runInContext(section('async function loadThinking()', '// Wake collectors'), ctx);
  ctx.renderThinkBubbles = () => {};
  return {ctx, requests, timers};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
// 隔离 API 实采载荷（真实 get_thinking/get_reasoning 输出，非手工字面量）
const payloads = JSON.parse(child.execFileSync(
  path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe'),
  ['-X', 'utf8', '-B', path.join(__dirname, 'native_observation_payloads.py')],
  {cwd: path.join(__dirname, '..'), env: {...process.env, PYTHONPATH: ''}, encoding: 'utf8'}));
const firstEntry = payloads.first_thinking.runs.find(e => e.observed === true);
const secondEntry = payloads.second_thinking.runs.find(e => e.observed === true);
assert.ok(firstEntry && secondEntry, 'payloads carry observed entries');
assert.notEqual(secondEntry.wake_started_at, firstEntry.wake_started_at, 'second batch is a new epoch');
assert.equal(payloads.old_reasoning.process_ended_at, 1005, 'isolated API keeps the old terminal');
async function main() {
  const h = harness(), {ctx, requests} = h;
  // 第一批：/thinking 实采载荷 → 气泡创建与归属
  ctx.snapshot = payloads.first_thinking;
  await ctx.loadThinking();
  assert.equal(ctx.S.thinkRuns.size, 1, 'first batch creates a collector run');
  const run1 = [...ctx.S.thinkRuns.values()][0];
  assert.equal(run1.profile, firstEntry.profile, 'profile ownership from payload');
  assert.equal(run1.session, firstEntry.session, 'session ownership from payload');
  assert.equal(run1.project, firstEntry.project, 'project ownership from payload');
  assert.equal(run1.state, 'native_running', 'observation state lands on the run');
  assert.equal(ctx.applyExecutionState(run1), '', 'observation is not an execution stall state');
  assert.equal([...ctx.S.thinkBubbles.values()][0].status, 'thinking', 'first batch bubble active');
  // 第二批启动（同绑定新批次）→ 新气泡；旧批展示窗口被新批次封口
  ctx.startThinkBubble(secondEntry);
  assert.equal(ctx.S.thinkRuns.size, 2, 'second batch is a separate run');
  const run2 = [...ctx.S.thinkRuns.values()].find(r => r !== run1);
  assert.ok(run1.wake_ended_at != null, 'new batch closes the old display window');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'thinking', 'second batch bubble active');
  // startThinkBubble 为第二批自动排队的轮询：实采响应（process_ended_at=null）→ 保持活跃
  requests.shift().resolve(payloads.new_reasoning);
  await tick();
  assert.equal(run2.finished, false, 'new batch stays active on its own poll');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'thinking', 'new batch bubble still thinking');
  // 旧批迟到查询：/reasoning 实采响应（process_ended_at=1005）→ 旧批折叠
  const poll1 = ctx.pollReasoning(run1);
  requests.shift().resolve(payloads.old_reasoning);
  await poll1;
  assert.equal(run1.finished, true, 'late old-batch reasoning folds the run');
  assert.equal([...ctx.S.thinkBubbles.values()][0].status, 'inactive', 'old batch cleared despite new batch');
  // 新批结束：/reasoning 实采响应（process_ended_at=1010）→ 折叠
  const poll3 = ctx.pollReasoning(run2);
  requests.shift().resolve(payloads.new_reasoning_ended);
  await poll3;
  assert.equal(run2.finished, true, 'second batch end folds the run');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'inactive', 'second batch cleared on end');
  console.log('PASS: isolated-API payloads drive ownership, old-batch fold with new batch active, end clearing');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
