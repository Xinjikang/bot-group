// Run: node scripts/test_wake_liveness.cjs
// req#58 task#105：判活逻辑纯函数 + /thinking 心跳兜底（96e22a2 被 req#51 重构冲掉后补回）。
// 纯函数单测 + pollReasoning 兜底链路（vm 执行生产代码段，无网络）。
//
// 夹具注意：startThinkBubble 内 `{...data, lastContentAt: Date.now()}` 后置覆盖——测试
// 传不进陈旧 lastContentAt（生产正确行为，wake.spawned 事件本无此字段）。静默场景统一用
// makeStale()：等首轮落定后直接改写 run.lastContentAt 注入静默，再手动 pollReasoning 一轮。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const section = (from, to) => html.slice(html.indexOf(from), html.indexOf(to, html.indexOf(from)));

const WAKE = 120000; // 与前端 WAKE_SILENCE_MS 一致，防漂移

function harness({ thinking = [], failThinking = false } = {}) {
  const requests = [];
  const alerts = [];
  const store = new Map();
  const ctx = {
    S: {thinkRuns: new Map(), thinkBubbles: new Map()},
    console, Date, Map, Symbol, Number, JSON, encodeURIComponent,
    setTimeout() { return 1; }, clearTimeout() {}, renderMessages() {},
    window: {addEventListener() {}},
    localStorage: {getItem: () => null, setItem: () => {}},
    // req#134 task#256：manual 状态的取消意图与提示需要会话存储与告警出口。
    sessionStorage: {
      getItem: key => store.has(key) ? store.get(key) : null,
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: key => store.delete(key),
    },
    NAME_OF: profile => profile,
    showAlert(message) { alerts.push(message); },
    api(url) {
      return new Promise((resolve, reject) => {
        if (url === '/thinking' && failThinking) reject(new Error('engine down'));
        else resolve(url === '/thinking' ? {thinking} : {entries: [], latest_id: 0});
        requests.push(url);
      });
    },
  };
  vm.createContext(ctx);
  // startThinkBubble → saveThinkingState 一整段：判活纯函数 + 心跳兜底 + pollReasoning 全覆盖
  vm.runInContext(section('function startThinkBubble(', 'window.addEventListener("pagehide"'), ctx);
  // The browser regression covers bubble rendering independently of liveness.
  ctx.renderThinkBubbles = () => ctx.saveThinkingState();
  return {ctx, requests, alerts};
}
const wake = (over = {}) => ({profile: 'bot', session: 'bot-xjk-chat-42', thread_id: 42,
  wake_started_at: 100, ...over});
const beat = (over = {}) => wake(over);
const tick = () => new Promise(resolve => setImmediate(resolve));

// vm 跨 realm 的 Object.prototype 不同，assert.deepEqual 判「非引用相等」会误报——
// verdict 只有两个原始值字段，按字段比较。
const verdictIs = (v, active, reason, msg) =>
  assert.ok(v.active === active && v.reason === reason, `${msg} (got ${JSON.stringify({...v})})`);

// ---------- 测试辅助：从 thinkRuns 拿当前 run（startThinkBubble 返回 undefined，不回传 source） ----------
const currentRun = (h) => [...h.ctx.S.thinkRuns.values()][0];
const currentBubble = (h) => [...h.ctx.S.thinkBubbles.values()][0];
// startThinkBubble 自带首轮 pollReasoning（lastContentAt 被重置为 now，判活恒 active）——
// 等它落定后注入静默超时，再手动拉一轮触发判活兜底。
async function makeStale(h, run) {
  while (run.polling) await tick();
  run.lastContentAt = Date.now() - WAKE - 1000;
  await h.ctx.pollReasoning(run);
  await tick();
}

async function main() {
  // ---------- 0. 纯函数：判据与优先级 ----------
  const pure = harness();
  const L = pure.ctx.evaluateWakeLiveness;
  // 120s 窗口常量防漂移：恰好 120s 不算静默（严格 >），超过才算
  verdictIs(L(wake({lastContentAt: 1000}), 1000 + 1), true, '', 'fresh silence stays active');
  verdictIs(L(wake({lastContentAt: 1000}), 1000 + WAKE + 1), false, 'silent', 'past 120s silent -> silent');
  verdictIs(L(wake({finished: true}), 1000 + WAKE + 1), false, 'finished', 'finished wins');
  verdictIs(L(wake({stopped: true}), 1000), false, 'stopped', 'stopped wins');
  verdictIs(L(wake({processEndedAt: 500}), 1000), false, 'processEnded', 'processEnded wins');
  // 判据优先级：finished > stopped > processEnded > silent
  verdictIs(L(wake({finished: true, stopped: true}), 0), false, 'finished', 'finished before stopped');
  verdictIs(L(wake({stopped: true, processEndedAt: 1}), 0), false, 'stopped', 'stopped before processEnded');

  // ---------- 1. 慢工具：静默 120s+ 但心跳在 → 兜底复核不判 inactive ----------
  const slow = harness({thinking: [beat()]});
  slow.ctx.startThinkBubble(wake());
  await makeStale(slow, currentRun(slow));
  assert.equal(currentBubble(slow).status, 'thinking',
    'heartbeat alive keeps bubble thinking past 120s silence');
  assert.ok(slow.requests.includes('/thinking'), 'fallback actually consulted /thinking');

  // ---------- 2. 崩溃兜底：静默 120s+ 且无心跳 → inactive + 折叠（验收②回归场景） ----------
  const dead = harness({thinking: []});
  dead.ctx.startThinkBubble(wake());
  await makeStale(dead, currentRun(dead));
  const deadBubble = currentBubble(dead);
  assert.equal(deadBubble.status, 'inactive', 'no heartbeat + 120s silence -> inactive (crash bottoming out)');
  assert.equal(deadBubble.collapsed, true, 'inactive folds the bubble');

  // ---------- 3. 复活：已误判 inactive 的气泡，心跳回来 → 恢复 thinking 并补跑迟到推理 ----------
  const revive = harness({thinking: []});
  revive.ctx.startThinkBubble(wake());
  const reviveRun = currentRun(revive);
  await makeStale(revive, reviveRun); // 第一轮：心跳空表 → inactive
  assert.equal(currentBubble(revive).status, 'inactive');
  // 第二轮：心跳续租上了（换 mock）→ 复活 + 迟到推理落进气泡
  revive.ctx.api = async (url) => url === '/thinking'
    ? {thinking: [beat()]}
    : {entries: [{id: 2, reasoning: 'late reasoning'}], latest_id: 2};
  await revive.ctx.pollReasoning(reviveRun);
  await tick();
  const rb = currentBubble(revive);
  assert.equal(rb.status, 'thinking', 'heartbeat return revives a wrongly-inactive bubble');
  assert.equal(rb.entries.length, 1, 'late reasoning appended after revival');

  // Work and chat slots are distinct, even when an exit event was missed.
  const cross = harness({thinking: [{profile: 'bot', thread_id: null}]});
  cross.ctx.startThinkBubble(wake());
  await makeStale(cross, currentRun(cross));
  assert.equal(currentBubble(cross).status, 'inactive',
    'work heartbeat cannot keep a chat bubble alive');
  for (const other of [
    beat({session: 'bot-q-work', thread_id: null}),
    beat({session: 'bot-p-work', thread_id: null, wake_started_at: 101}),
    beat(), {profile: 'bot', thread_id: null},
  ]) {
    const isolated = harness({thinking: [other]});
    isolated.ctx.startThinkBubble(wake({session: 'bot-p-work', thread_id: null}));
    await makeStale(isolated, currentRun(isolated));
    assert.equal(currentBubble(isolated).status, 'inactive', 'other project/run/chat or unidentified heartbeat cannot revive work');
  }
  const work = harness({thinking: [beat({session: 'bot-p-work', thread_id: null})]});
  work.ctx.startThinkBubble(wake({session: 'bot-p-work', thread_id: null}));
  await makeStale(work, currentRun(work));
  assert.equal(currentBubble(work).status, 'thinking', 'same work run stays alive');

  // ---------- 5. 硬信号不复核：processEndedAt 判 inactive，心跳在也不复活 ----------
  const hard = harness({thinking: [beat()]});
  hard.ctx.startThinkBubble(wake({processEndedAt: 500}));
  const hardRun = currentRun(hard);
  while (hardRun.polling) await tick();
  await tick();
  assert.equal(currentBubble(hard).status, 'inactive',
    'processEndedAt is a hard signal; heartbeat must not override it');

  // ---------- 6. /thinking 查询失败：显示事实标签，不折叠也不臆测运行中 ----------
  const fail = harness({failThinking: true});
  fail.ctx.startThinkBubble(wake());
  await makeStale(fail, currentRun(fail));
  const failBubble = currentBubble(fail);
  assert.equal(failBubble.status, 'stalled', 'heartbeat query failure shows a factual state');
  assert.equal(failBubble.stallLabel, '执行状态查询失败', 'query failure label is factual');
  assert.equal(failBubble.collapsed, false, 'heartbeat query failure must not fold');
  // 查询恢复且心跳在 → 解除查询失败标签，回到思考态。
  fail.ctx.api = async (url) => url === '/thinking'
    ? {thinking: [beat()]}
    : {entries: [], latest_id: 0};
  await fail.ctx.pollReasoning(currentRun(fail));
  await tick();
  assert.equal(currentBubble(fail).status, 'thinking', 'successful query clears the failure label');
  assert.equal(currentBubble(fail).stallLabel || '', '', 'no stale query failure label');

  // ---------- 7. 执行停在 manual（req#134 task#256，验收 A3） ----------
  // 未发起叫停（回执/查询中断）→「执行状态查询失败」，只报告事实，不打断用户。
  const stall = harness();
  stall.ctx.startThinkBubble(wake({state: 'manual'}));
  const stallBubble = currentBubble(stall);
  assert.equal(stallBubble.status, 'stalled', 'manual execution must not keep showing 思考中');
  assert.equal(stallBubble.stallLabel, '执行状态查询失败', 'no cancel intent -> 执行状态查询失败');
  assert.equal(stall.alerts.length, 0, 'non-cancel stall only labels, never interrupts');

  // 本页发起叫停后到达截止时间 →「停止请求已发送，结束状态确认超时」，一次性告警。
  const timeout = harness();
  timeout.ctx.startThinkBubble(wake({state: 'running'}));
  timeout.ctx.markCancelIntent(currentRun(timeout));
  timeout.ctx.syncThinkRunState(currentRun(timeout), wake({state: 'manual'}));
  assert.equal(currentBubble(timeout).stallLabel, '停止请求已发送，结束状态确认超时',
    'cancel intent -> stop request sent, confirmation timed out');
  assert.equal(timeout.alerts.length, 1, 'stop timeout announces once');
  assert.ok(!/待人工|需人工|人工处理/.test(timeout.alerts[0]), timeout.alerts[0]);
  timeout.ctx.syncThinkRunState(currentRun(timeout), wake({state: 'manual'}));
  assert.equal(timeout.alerts.length, 1, 'stop timeout alert is one-shot');

  // 引擎载荷带 cancel_reason 时以引擎为准（不再依赖本页意图）。
  const authoritative = harness();
  authoritative.ctx.startThinkBubble(wake({state: 'manual', cancel_reason: 'user'}));
  assert.equal(currentBubble(authoritative).stallLabel, '停止请求已发送，结束状态确认超时',
    'engine cancel_reason is authoritative when present');

  // 取消进行中先给「正在取消…」。
  const cancelling = harness();
  cancelling.ctx.startThinkBubble(wake({state: 'cancelling'}));
  assert.equal(currentBubble(cancelling).stallLabel, '正在取消…', 'cancelling label before deadline');

  // 静默判活不得覆盖 manual；状态回正常时才恢复思考态。
  const silentManual = harness();
  silentManual.ctx.startThinkBubble(wake({state: 'manual'}));
  await makeStale(silentManual, currentRun(silentManual));
  assert.equal(currentBubble(silentManual).status, 'stalled', 'silence must not fold a manual execution');
  silentManual.ctx.syncThinkRunState(currentRun(silentManual), wake({state: 'running'}));
  assert.equal(currentBubble(silentManual).status, 'thinking', 'state back to running clears the stall label');

  // 硬信号优先：已停止/进程退出不再显示状态标签。
  const hardStop = harness();
  hardStop.ctx.startThinkBubble(wake({state: 'manual', stopped: true}));
  assert.notEqual(currentBubble(hardStop).status, 'stalled', 'stopped hard signal clears the manual label');

  console.log('PASS: wake liveness with exact session/run matching, null isolation, hard signals, '
    + 'query failure and factual execution state labels');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
