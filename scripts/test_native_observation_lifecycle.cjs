// Run: node scripts/test_native_observation_lifecycle.cjs
// #277 验收 5：用本任务真实接口载荷（/thinking 观察条目）验证现有
// loadThinking/startThinkBubble/applyExecutionState 的归属、活动状态与结束清除。
// 观察条目形状来自 engine/native_observation.py _entry；结束载荷为 /reasoning
// 在观察批次结束后返回的 process_ended_at。纯 vm 执行生产函数，无网络与文件写入。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
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
const START = 1234567.89;
const observation = {profile: 'hermes-a', session: 'hermes-a-p-work', thread_id: null,
  project: 'p', kind: 'work', state: 'native_running', observed: true,
  wake_started_at: START, spawned_at: '2026-11-03T10:00:00'};
const tick = () => new Promise(resolve => setImmediate(resolve));
async function main() {
  const h = harness(), {ctx, requests} = h;
  ctx.snapshot = {runs: [observation], thinking: []};
  await ctx.loadThinking();
  assert.equal(ctx.S.thinkRuns.size, 1, 'observation entry creates a collector run');
  const source = [...ctx.S.thinkRuns.values()][0];
  assert.equal(source.profile, 'hermes-a', 'profile ownership');
  assert.equal(source.session, 'hermes-a-p-work', 'work session ownership');
  assert.equal(source.thread_id, null, 'work binding has no thread');
  assert.equal(source.project, 'p', 'project ownership');
  assert.equal(source.state, 'native_running', 'observation state lands on the run');
  assert.equal(ctx.applyExecutionState(source), '', 'observation is not an execution stall state');
  assert.equal([...ctx.S.thinkBubbles.values()][0].status, 'thinking', 'bubble active while observed');
  // 话题归属：chat 绑定条目同样落在现有渲染路径
  const chat = {...observation, session: 'hermes-a-p-chat-42', thread_id: 42, kind: 'chat',
    wake_started_at: START + 1000};
  ctx.startThinkBubble(chat);
  const chatSource = [...ctx.S.thinkRuns.values()].find(r => r.thread_id === 42);
  assert.equal(chatSource.session, 'hermes-a-p-chat-42', 'chat thread ownership');
  assert.equal([...ctx.S.thinkBubbles.values()].length, 2, 'chat observation renders separately');
  // 结束清除：/reasoning 在观察批次结束后返回 process_ended_at（真实结束载荷）
  const poll = ctx.pollReasoning(source);
  requests.shift().resolve({entries: [], latest_id: 0, replies: [], process_ended_at: START + 30});
  await poll;
  assert.equal(source.finished, true, 'observation end folds the run');
  assert.equal([...ctx.S.thinkBubbles.values()][0].status, 'inactive',
    'ended observation clears the active state');
  // 话题观察同样按结束载荷折叠，之后快照轮询不再复活任何已结束观察
  const chatPoll = ctx.pollReasoning(chatSource);
  requests.shift().resolve({entries: [], latest_id: 0, replies: [], process_ended_at: START + 1030});
  await chatPoll;
  assert.equal(chatSource.finished, true);
  ctx.snapshot = {runs: [], thinking: []};
  await ctx.loadThinking();
  assert.equal(requests.length, 0, 'periodic snapshot does not revive ended observations');
  assert.equal([...ctx.S.thinkRuns.values()].filter(r => !r.finished).length, 0,
    'all observation runs ended after the payload clears');
  console.log('PASS: observation payload ownership, active state and end clearing via existing frontend functions');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
