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
// 按 /reasoning 的 session+wake_started_at 匹配解析（不依赖排队顺序；队列中自动轮询与
// 显式轮询并存，顺序随前端行为变化，固定 shift 会取错请求）
function resolveReasoning(requests, session, wakeStarted, payload) {
  for (let i = requests.length - 1; i >= 0; i--) {
    const url = requests[i].url;
    if (url.includes(`session=${session}`) && url.includes(`wake_started_at=${wakeStarted}`)) {
      const [req] = requests.splice(i, 1);
      req.resolve(payload);
      return;
    }
  }
  throw new Error(`no queued /reasoning for ${session} @ ${wakeStarted}`);
}
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
let finished = false;
// 悬挂守卫：pending promise 不保持 event loop，静默 exit 0 是假 pass——哨兵 + 超时显式失败
const guard = setTimeout(() => {
  if (!finished) { console.error('FAIL: lifecycle scenario hung (sentinel not set)'); process.exit(1); }
}, 20000);
async function main() {
  const h = harness(), {ctx, requests, timers} = h;
  // 第一批：/thinking 实采载荷 → 气泡创建与归属（loadThinking 自动排队 run1 轮询）
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
  // 第二批启动（同绑定新批次）→ 新气泡；旧批展示窗口被新批次封口（startThinkBubble 自动排队 run2 轮询）
  ctx.startThinkBubble(secondEntry);
  assert.equal(ctx.S.thinkRuns.size, 2, 'second batch is a separate run');
  const run2 = [...ctx.S.thinkRuns.values()].find(r => r !== run1);
  assert.ok(run1.wake_ended_at != null, 'new batch closes the old display window');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'thinking', 'second batch bubble active');
  // 新批自身轮询：实采响应（process_ended_at=null）→ 保持活跃
  resolveReasoning(requests, 'dev-1-p-work', 1006, payloads.new_reasoning);
  await tick();
  assert.equal(run2.finished, false, 'new batch stays active on its own poll');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'thinking', 'new batch bubble still thinking');
  // 旧批迟到查询（run1 自动轮询的响应）：第二批已启动，旧请求按 end 不匹配被弃 → 前端
  // finally 以新边界重排轮询（定时器回调即 pollReasoning）→ 重取响应（process_ended_at=1005）折叠旧批
  resolveReasoning(requests, 'dev-1-p-work', 1000, payloads.old_reasoning);
  await tick();
  assert.equal(run1.finished, false, 'stale-bound response is discarded, not folded');
  const refetchPoll = ctx.pollReasoning(run1);
  resolveReasoning(requests, 'dev-1-p-work', 1000, payloads.old_reasoning);
  await refetchPoll;
  assert.equal(run1.finished, true, 'refetch with the new bound folds the old batch');
  assert.equal([...ctx.S.thinkBubbles.values()][0].status, 'inactive', 'old batch cleared despite new batch');
  assert.equal(run2.finished, false, 'new batch still active while old batch folds');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'thinking', 'new batch bubble still thinking');
  // 新批结束：/reasoning 实采响应（process_ended_at=1010）→ 折叠
  const poll3 = ctx.pollReasoning(run2);
  resolveReasoning(requests, 'dev-1-p-work', 1006, payloads.new_reasoning_ended);
  await poll3;
  assert.equal(run2.finished, true, 'second batch end folds the run');
  assert.equal([...ctx.S.thinkBubbles.values()][1].status, 'inactive', 'second batch cleared on end');
  finished = true;
  console.log('PASS: isolated-API payloads drive ownership, old-batch fold with new batch active, end clearing');
}
main().catch(error => { clearTimeout(guard); console.error(error); process.exitCode = 1; });
