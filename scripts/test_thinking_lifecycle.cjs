// Run: node scripts/test_thinking_lifecycle.cjs
// Execute production functions with controlled responses; no network or file writes.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const section = (from, to) => html.slice(html.indexOf(from), html.indexOf(to, html.indexOf(from)));
function harness(storage = new Map()) {
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const ctx = {
    S: {thinkRuns: new Map(), thinkBubbles: new Map()},
    console, Date, Map, Symbol, Number, JSON, encodeURIComponent,
    setTimeout(fn, delay) { timers.set(++timerId, {fn, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); }, renderMessages() {},
    window: {addEventListener() {}},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    snapshot: {thinking: []},
    api(url) {
      if (url === '/thinking') return ctx.snapshot instanceof Error ? Promise.reject(ctx.snapshot) : Promise.resolve(ctx.snapshot);
      return new Promise((resolve, reject) => requests.push({url, resolve, reject}));
    },
  };
  vm.createContext(ctx);
  vm.runInContext(section('function startThinkBubble(', '// 当前视图相关'), ctx);
  vm.runInContext(section('async function loadThinking()', '// Wake collectors'), ctx);
  vm.runInContext(section('function refreshThinkRunsForMessage(', '// 叫停一个 bot'), ctx);
  // Lifecycle tests isolate collection/persistence; real DOM updates run in Chrome.
  ctx.renderThinkBubbles = () => ctx.saveThinkingState();
  return {ctx, requests, storage, timers};
}
const wake = (thread_id = null, start = 100) => ({profile: 'bot', session: `bot-${thread_id}`, thread_id, wake_started_at: start});
const messageEvent = (run, message_id) => ({sender: run.profile, project: run.project,
  thread_id: run.thread_id, message_id,
  execution: {session: run.session, wake_started_at: run.wake_started_at}});
const legacyStorage = storage => {
  const saved = JSON.parse(storage.get('group-engine.thinking.v2'));
  for (const run of saved.runs) delete run.replyReconciled;
  return new Map([['group-engine.thinking.v2', JSON.stringify(saved)]]);
};
const tick = () => new Promise(resolve => setImmediate(resolve));
async function main() {
  const fresh = harness();
  const recovered = {...wake(42), project: 'p', spawned_at: '2026-09-22T10:00:00'};
  fresh.ctx.snapshot = {runs: [recovered, {profile: 'bot', thread_id: null}], thinking: []};
  await fresh.ctx.loadThinking();
  assert.equal(fresh.ctx.S.thinkRuns.size, 1, 'empty browser recovers only exact recorded runs');
  const recoveredRun = [...fresh.ctx.S.thinkRuns.values()][0];
  assert.equal(recoveredRun.project, 'p');
  fresh.requests.shift().resolve({entries: [{id: 7, reasoning: 'recovered'}], latest_id: 7});
  await tick();
  fresh.ctx.startThinkBubble(recovered);
  await fresh.ctx.loadThinking();
  assert.equal(fresh.ctx.S.thinkRuns.size, 1, 'SSE and repeated snapshots merge idempotently');
  assert.match(fresh.requests.shift().url, /after_id=7/, 'snapshot preserves incremental cursor');
  fresh.ctx.snapshot = new Error('offline');
  await fresh.ctx.loadThinking();
  assert.equal(fresh.ctx.S.thinkRuns.size, 1, 'snapshot failure preserves collectors');
  assert.match(html, /es.onopen\s*=.*loadThinking\(\)/, 'SSE reconnect recovers missed spawns');
  const reversed = harness();
  reversed.ctx.startThinkBubble(wake(42, 200));
  reversed.ctx.snapshot = {runs: [wake(42, 100)]};
  await reversed.ctx.loadThinking();
  assert.equal(reversed.ctx.S.thinkRuns.get('bot-42:100').wake_ended_at, 200,
    'late snapshot cannot collect reasoning from a newer SSE wake');
  const projects = harness();
  const p = {...wake(null, 100), session: 'bot-p-work', project: 'p'};
  const q = {...wake(null, 101), session: 'bot-q-work', project: 'q'};
  projects.ctx.startThinkBubble(p);
  projects.ctx.startThinkBubble(q);
  projects.requests.shift().resolve({entries: [], latest_id: 0});
  projects.requests.shift().resolve({entries: [], latest_id: 0});
  await tick();
  projects.ctx.startThinkBubble({...p, wake_started_at: 102});
  projects.requests.shift().resolve({entries: [], latest_id: 0});
  await tick();
  projects.ctx.refreshThinkRunsForMessage(messageEvent(p, 51));
  assert.equal(projects.requests.length, 1, 'only the exact execution refreshes, even after a newer wake');
  assert.ok([...projects.ctx.S.thinkBubbles.values()].every(b => b.reply_message_id == null), 'delayed SSE cannot bind latest p or q');
  projects.requests.shift().resolve({entries: [{id: 1, reply_message_id: 51}], latest_id: 1});
  await tick();
  const projectBubbles = [...projects.ctx.S.thinkBubbles.values()];
  assert.deepEqual(projectBubbles.map(b => b.reply_message_id), [51, null, null], 'persisted reply belongs only to the original p run');
  assert.equal(projectBubbles[1].collapsed, false, 'q bubble stays open');
  assert.equal(projectBubbles[2].collapsed, false, 'later p bubble stays open');
  projects.ctx.refreshThinkRunsForMessage({sender: 'bot', thread_id: null, message_id: 999});
  assert.equal(projects.requests.length, 0, 'legacy events without execution do not fan out');
  for (const override of [{sender: 'other'}, {project: 'q'}, {thread_id: 42},
    {execution: {session: p.session, wake_started_at: '100'}},
    {execution: {session: p.session, wake_started_at: 999}}]) {
    projects.ctx.refreshThinkRunsForMessage({...messageEvent(p, 51), ...override});
  }
  assert.equal(projects.requests.length, 0, 'mismatched or unknown execution events do not query history');
  assert.deepEqual(projectBubbles.map(b => b.reply_message_id), [51, null, null], 'legacy SSE without project cannot assign a reply');

  const exited = harness();
  exited.ctx.startThinkBubble(wake(42));
  exited.requests.shift().resolve({entries: [{id: 1, reasoning: 'partial output'}], latest_id: 1, process_ended_at: 110});
  await tick();
  const exitedRun = [...exited.ctx.S.thinkRuns.values()][0];
  assert.equal(exitedRun.finished, true, 'successful final read ends collection without assistant final text');
  assert.equal(exited.timers.size, 0, 'ended execution schedules no recurring poll');
  await exited.ctx.loadThinking();
  assert.equal(exited.requests.length, 0, 'periodic snapshot does not revive an ended execution');
  assert.equal([...exited.ctx.S.thinkBubbles.values()][0].status, 'inactive', 'process exit stops animation without final text');
  const lateExitPoll = exited.ctx.pollReasoning(exitedRun, true);
  exited.requests.shift().resolve({entries: [{id: 2, reply_message_id: 90}], latest_id: 2, process_ended_at: 110});
  await lateExitPoll;
  assert.equal([...exited.ctx.S.thinkBubbles.values()][0].status, 'replied', 'late reply still finalizes an exited bubble');
  for (const exitCode of [0, 1]) {
    const emptyExit = harness();
    emptyExit.ctx.startThinkBubble(wake());
    emptyExit.requests.shift().resolve({entries: [], latest_id: 0, replies: [], process_ended_at: 110, exit_code: exitCode});
    await tick();
    assert.equal(emptyExit.timers.size, 0, 'empty completed/failed executions stop polling');
    const reloadedExit = harness(emptyExit.storage);
    reloadedExit.ctx.restoreThinkingState();
    await reloadedExit.ctx.loadThinking();
    assert.equal(reloadedExit.requests.length, 0, 'empty terminal execution remains stopped after reload');
  }
  const layout = {bubbles: [], renderMsg: (m, bubbles = []) => `message:${m.id}${bubbles.map(b => `+${b.key}`).join('')}`,
    renderThinkBubble: b => `bubble:${b.key}`};
  layout.thinkingBubblesForDisplay = () => layout.bubbles;
  vm.createContext(layout);
  layout.S = {currentThread: 42};
  vm.runInContext(section('function bubbleInCurrentProject(', 'async function loadOlderMessages('), layout);
  vm.runInContext(section('function appendTopicConversation(', 'function renderMessages('), layout);
  const messages = [{id: 1, ts: '2026-09-14T21:03:47'}, {id: 2, ts: '2026-09-14T21:04:53'}];
  for (const parent of [null, 1]) {
    layout.bubbles = [
      {key: 'previous', thread_id: 42, reply_message_id: 2, status: 'replied', spawned_at: messages[0].ts},
      {key: 'next', thread_id: 42, parent_id: parent, reply_message_id: null, status: 'thinking', spawned_at: messages[0].ts},
    ];
    const nodes = [];
    layout.appendTopicConversation({appendChild: n => nodes.push(n)}, messages, 42);
    assert.deepEqual(nodes, ['message:1', 'message:2+previous', 'bubble:next'],
      'live next speaker follows completed replies even with an older time or parent anchor');
    layout.bubbles[1].reply_message_id = 3;
    layout.bubbles[1].status = 'replied';
    const afterReply = [];
    layout.appendTopicConversation({appendChild: n => afterReply.push(n)},
      [...messages, {id: 3, ts: '2026-09-14T21:05:25'}], 42);
    assert.deepEqual(afterReply, ['message:1', 'message:2+previous', 'message:3+next']);
  }
  for (const thread of [null, 40]) {
    const continuing = harness();
    continuing.ctx.startThinkBubble(wake(thread));
    continuing.requests.shift().resolve({entries: [
      {id: 1, reasoning: 'before reply'}, {id: 2, reply_message_id: 90},
    ], latest_id: 2});
    await tick();
    const continuingRun = [...continuing.ctx.S.thinkRuns.values()][0];
    const continuePoll = continuing.ctx.pollReasoning(continuingRun);
    continuing.requests.shift().resolve({entries: [
      {id: 3, tool_calls: [{name: 'terminal', args_preview: 'run tests'}]},
    ], latest_id: 3});
    await continuePoll;
    const continued = [...continuing.ctx.S.thinkBubbles.values()];
    assert.equal(continued.length, 2, 'work after a reply gets a separate visible segment');
    assert.equal(continued[0].status, 'replied');
    assert.equal(continued[0].collapsed, true, 'the previous reply keeps its folded history');
    assert.equal(continued[1].status, 'thinking');
    assert.equal(continued[1].collapsed, false);
    assert.equal(continued[1].reply_message_id, null);
    assert.equal(continued[1].parent_id, 90);
    assert.deepEqual(Array.from(continued[1].entries, e => e.id), [3]);

    const liveRestore = harness(continuing.storage);
    liveRestore.ctx.restoreThinkingState();
    assert.equal(liveRestore.ctx.S.thinkBubbles.size, 2, 'refresh preserves a live post-reply segment');
    assert.equal([...liveRestore.ctx.S.thinkBubbles.values()][1].status, 'thinking');

    const oldLive = JSON.parse(continuing.storage.get('group-engine.thinking.v2'));
    oldLive.bubbles[0].entries.push(...oldLive.bubbles.pop().entries);
    oldLive.runs[0].currentKey = oldLive.bubbles[0].key;
    oldLive.runs[0].tailStart = 1;
    const legacyStorage = new Map([['group-engine.thinking.v2', JSON.stringify(oldLive)]]);
    const legacyLive = harness(legacyStorage);
    legacyLive.ctx.restoreThinkingState();
    const recoveredTail = [...legacyLive.ctx.S.thinkBubbles.values()][1];
    assert.equal(recoveredTail.status, 'thinking', 'old hidden live tail becomes active on refresh');
    assert.equal(recoveredTail.collapsed, false);
    assert.deepEqual(Array.from(recoveredTail.entries, e => e.id), [3]);
    assert.equal([...legacyLive.ctx.S.thinkBubbles.values()][0].entries.length, 1);

    const finishPoll = continuing.ctx.pollReasoning(continuingRun);
    continuing.requests.shift().resolve({entries: [], latest_id: 3, process_ended_at: 110});
    await finishPoll;
    assert.equal(continuing.ctx.S.thinkBubbles.size, 1, 'confirmed exit folds cleanup back into the reply');
    assert.deepEqual(Array.from([...continuing.ctx.S.thinkBubbles.values()][0].entries, e => e.id), [1, 3]);

    const trailing = harness();
    trailing.ctx.startThinkBubble(wake(thread));
    const reading = [...trailing.ctx.S.thinkBubbles.values()][0];
    reading.keepOpen = true;
    trailing.requests.shift().resolve({entries: [
      {id: 1, reasoning: 'before reply'}, {id: 2, reply_message_id: 90},
    ], latest_id: 2});
    await tick();
    const run = [...trailing.ctx.S.thinkRuns.values()][0];
    const poll = trailing.ctx.pollReasoning(run);
    trailing.requests.shift().resolve({entries: [
      {id: 3, reasoning: 'check inbox after replying'}, {id: 4, finished: true},
    ], latest_id: 4});
    await poll;
    let bubbles = [...trailing.ctx.S.thinkBubbles.values()];
    assert.equal(bubbles.length, 1, 'post-reply cleanup stays in the original bubble');
    assert.equal(bubbles[0].status, 'replied');
    assert.equal(bubbles[0].collapsed, false, 'reply preserves an explicitly opened reading panel');
    assert.deepEqual(Array.from(bubbles[0].entries, e => e.id), [1, 3]);
    const saved = harness(trailing.storage);
    saved.ctx.restoreThinkingState();
    assert.equal(saved.ctx.S.thinkBubbles.size, 1, 'refresh preserves appended cleanup');

    // Simulate the old persisted layout from the reported screenshot.
    const legacy = JSON.parse(trailing.storage.get('group-engine.thinking.v2'));
    legacy.bubbles[0].entries.pop();
    const orphan = {...legacy.bubbles[0], key: run.key + ':2',
      reply_message_id: null, status: 'inactive', entries: [{id: 3, reasoning: 'cleanup'}]};
    legacy.bubbles.push(orphan);
    legacy.runs[0].currentKey = orphan.key;
    legacy.runs[0].segment = 2;
    trailing.storage.set('group-engine.thinking.v2', JSON.stringify(legacy));
    const migrated = harness(trailing.storage);
    migrated.ctx.restoreThinkingState();
    bubbles = [...migrated.ctx.S.thinkBubbles.values()];
    assert.equal(bubbles.length, 1, 'legacy trailing bubble merges on refresh');
    assert.equal(bubbles[0].entries.length, 2);
    assert.equal(bubbles[0].status, 'replied');
  }
  for (const parentId of [null, 2325]) {
  const topic = harness();
  vm.runInContext(section('function thinkingBubblesForDisplay()', 'function renderThinkBubble('), topic.ctx);
  const firstWake = {...wake(40, 100), parent_id: parentId, round: 1};
  topic.ctx.startThinkBubble(firstWake);
  topic.requests.shift().resolve({entries: [{id: 1, reasoning: 'read inbox'}], latest_id: 1});
  await tick();
  topic.ctx.startThinkBubble({...firstWake, wake_started_at: 125});
  topic.requests.shift().resolve({entries: [{id: 2, reasoning: 'continue thinking'}], latest_id: 2});
  await tick();
  let displayed = topic.ctx.thinkingBubblesForDisplay();
  assert.equal(displayed.length, 1, 'same topic turn appends across wakes');
  assert.deepEqual(Array.from(displayed[0].entries, e => e.id), [1, 2]);
  assert.equal(topic.ctx.S.thinkBubbles.size, 2, 'collection windows remain separate');
  const current = [...topic.ctx.S.thinkRuns.values()][1];
  const replyPoll = topic.ctx.pollReasoning(current);
  topic.requests.shift().resolve({entries: [{id: 3, reply_message_id: 2326}], latest_id: 3});
  await replyPoll;
  displayed = topic.ctx.thinkingBubblesForDisplay();
  assert.equal(displayed.length, 1);
  assert.equal(displayed[0].reply_message_id, 2326);
  assert.equal(displayed[0].status, 'replied');
  assert.equal(displayed[0].collapsed, true);
  topic.ctx.saveThinkingState();
  const topicRestored = harness(topic.storage);
  vm.runInContext(section('function thinkingBubblesForDisplay()', 'function renderThinkBubble('), topicRestored.ctx);
  topicRestored.ctx.restoreThinkingState();
  const restoredDisplay = topicRestored.ctx.thinkingBubblesForDisplay();
  assert.equal(restoredDisplay.length, 1, 'reply remains merged after refresh with null or explicit parent');
  assert.equal(restoredDisplay[0].status, 'replied');
  assert.deepEqual(Array.from(restoredDisplay[0].entries, e => e.id), [1, 2]);
  topic.ctx.startThinkBubble({...firstWake, wake_started_at: 150});
  assert.equal(topic.ctx.thinkingBubblesForDisplay().length, 2, 'actual reply ends the merged bubble');
  topic.ctx.startThinkBubble({...firstWake, wake_started_at: 175, parent_id: 2327, round: 2});
  assert.equal(topic.ctx.thinkingBubblesForDisplay().length, 3, 'new topic turn stays separate');
  }

  const h = harness(), {ctx, requests} = h;
  ctx.startThinkBubble(wake());
  ctx.refreshThinkRunsForMessage(messageEvent(wake(), 11));
  assert.equal([...ctx.S.thinkBubbles.values()][0].reply_message_id, null, 'SSE never guesses reply ownership');
  requests.shift().resolve({entries: [{id: 1, reasoning: 'late reasoning'}, {id: 2, reply_message_id: 11}], latest_id: 2});
  await tick();
  assert.equal(requests.length, 1, 'a message during collection queues a boundary refresh');
  requests.shift().resolve({entries: [], latest_id: 2});
  await tick();
  let bubbles = [...ctx.S.thinkBubbles.values()];
  assert.equal(bubbles[0].entries[0].reasoning, 'late reasoning');
  assert.equal(bubbles[0].reply_message_id, 11);
  assert.equal(bubbles[0].status, 'replied');
  const source = [...ctx.S.thinkRuns.values()][0];
  const pending = ctx.pollReasoning(source);
  requests.shift().resolve({entries: [{id: 3, reasoning: 'second reasoning'}, {id: 4, reply_message_id: 12}, {id: 5, finished: true}], latest_id: 5});
  await pending;
  bubbles = [...ctx.S.thinkBubbles.values()];
  assert.equal(bubbles.length, 2);
  assert.deepEqual(bubbles.map(b => b.reply_message_id), [11, 12]);
  assert.equal(bubbles[1].entries[0].reasoning, 'second reasoning');
  assert.equal(bubbles[1].parent_id, 11);
  assert.equal(source.finished, true);
  ctx.startThinkBubble(wake());
  assert.equal(ctx.S.thinkRuns.size, 1, 'duplicate wake must not duplicate bubbles');

  const restored = harness(h.storage);
  restored.ctx.restoreThinkingState();
  assert.equal(restored.ctx.S.thinkBubbles.size, 2);
  assert.equal([...restored.ctx.S.thinkRuns.values()][0].after_id, 5);
  await restored.ctx.loadThinking();
  assert.equal(restored.requests.length, 0, 'completed wakes stay complete after reload');

  const compacted = harness();
  for (const data of [
    {...wake(null, 100), session: 'bot-p-work', project: 'p'},
    {...wake(null, 110), session: 'bot-q-work', project: 'q'},
    {...wake(42, 120), session: 'bot-p-chat-42', project: 'p'},
    {...wake(null, 200), session: 'bot-p-work', project: 'p'},
  ]) {
    compacted.ctx.startThinkBubble(data);
    compacted.requests.shift().resolve({entries: [
      {id: 1, timestamp: data.wake_started_at + 1, reasoning: 'before compaction'},
      {id: 2, timestamp: data.wake_started_at + 2, finished: true},
    ], latest_id: 2});
    await tick();
  }
  compacted.ctx.refreshThinkRunsForMessage(messageEvent({...wake(), session: 'bot-p-work', project: 'p'}, 6871));
  assert.equal(compacted.requests.length, 1, 'compacted collector refreshes only its exact execution');
  compacted.requests.shift().resolve({entries: [
    {id: 3, timestamp: 105, reasoning: 'after compaction'}, {id: 4, timestamp: 107, finished: true},
  ], latest_id: 4, replies: [{message_id: 6871, sent_at: 106}], process_ended_at: 108});
  await tick();
  const repaired = [...compacted.ctx.S.thinkBubbles.values()].find(b => b.reply_message_id === 6871);
  assert.equal(repaired.sourceKey, 'bot-p-work:100', 'persisted send provenance assigns the reply to its original wake');
  assert.deepEqual(Array.from(repaired.entries, e => e.id), [1, 3]);
  assert.equal([...compacted.ctx.S.thinkBubbles.values()].filter(b => b.reply_message_id != null).length, 1);

  const cached = harness();
  cached.ctx.startThinkBubble({...wake(null, 300), project: 'p'});
  cached.requests.shift().resolve({entries: [{id: 1, timestamp: 301, reasoning: 'cached before compaction'},
    {id: 2, timestamp: 302, finished: true}], latest_id: 2});
  await tick();
  const recover = harness(legacyStorage(cached.storage));
  recover.ctx.restoreThinkingState();
  await recover.ctx.loadThinking();
  assert.equal(recover.requests.length, 1, 'an orphan marked complete by the old client is rechecked on reload');
  recover.requests.shift().reject(new Error('temporary reasoning failure'));
  await tick();
  await recover.ctx.loadThinking();
  assert.equal(recover.requests.length, 1, 'failed cache recovery remains eligible for retry');
  // The reply arrives while the cache recovery request still sees no send.
  recover.ctx.refreshThinkRunsForMessage(messageEvent({...wake(null, 300), project: 'p'}, 6871));
  recover.requests.shift().resolve({entries: [], latest_id: 2, replies: []});
  await tick();
  assert.equal(recover.requests.length, 1, 'a concurrent reply survives the in-flight recovery');
  recover.requests.shift().resolve({entries: [{id: 3, timestamp: 303, reasoning: 'cached after compaction'},
    {id: 4, timestamp: 305, finished: true}], latest_id: 4,
    replies: [{message_id: 6871, sent_at: 304}], process_ended_at: 306});
  await tick();
  assert.equal(recover.ctx.S.thinkBubbles.size, 1);
  assert.equal([...recover.ctx.S.thinkBubbles.values()][0].reply_message_id, 6871);
  await recover.ctx.loadThinking();
  assert.equal(recover.requests.length, 0, 'repaired completion does not restart periodic polling');

  const orphan = harness(legacyStorage(cached.storage));
  orphan.ctx.restoreThinkingState();
  await orphan.ctx.loadThinking();
  orphan.requests.shift().resolve({entries: [], latest_id: 2, replies: [], process_ended_at: 306});
  await tick();
  const orphanReload = harness(orphan.storage);
  orphanReload.ctx.restoreThinkingState();
  await orphanReload.ctx.loadThinking();
  assert.equal(orphanReload.requests.length, 0, 'successful empty recovery is remembered across reloads');
  assert.equal(orphan.timers.size, 0);

  const failedExit = harness();
  failedExit.ctx.startThinkBubble(wake());
  const failedRun = [...failedExit.ctx.S.thinkRuns.values()][0];
  failedRun.processEndedAt = 110;
  failedExit.requests.shift().reject(new Error('final read failed'));
  await tick();
  assert.equal(failedRun.finished, false, 'exit event alone cannot discard an unread tail');
  assert.equal(failedExit.timers.size, 1, 'failed final read retains a retry');
  const retryExit = failedExit.ctx.pollReasoning(failedRun);
  failedExit.requests.shift().resolve({entries: [{id: 1, reasoning: 'last output'}], latest_id: 1, process_ended_at: 110});
  await retryExit;
  assert.equal(failedRun.finished, true);
  assert.equal(failedExit.timers.size, 0);
  assert.equal([...failedExit.ctx.S.thinkBubbles.values()][0].entries[0].reasoning, 'last output');

  const exitRace = harness();
  exitRace.ctx.startThinkBubble(wake());
  const racingRun = [...exitRace.ctx.S.thinkRuns.values()][0];
  racingRun.processEndedAt = 110;
  exitRace.ctx.pollReasoning(racingRun, true);
  exitRace.requests.shift().resolve({entries: [], latest_id: 0, process_ended_at: null});
  await tick();
  assert.equal(racingRun.finished, false, 'response started before exit cannot finalize collection');
  assert.equal(exitRace.requests.length, 1, 'exit during request schedules one final refresh');
  exitRace.requests.shift().resolve({entries: [{id: 1, reasoning: 'flushed tail'}], latest_id: 1, process_ended_at: 110});
  await tick();
  assert.equal(racingRun.finished, true);
  assert.equal(exitRace.timers.size, 0);
  assert.equal([...exitRace.ctx.S.thinkBubbles.values()][0].entries[0].reasoning, 'flushed tail');

  const history = harness();
  for (let i = 1; i <= 200; i++) {
    history.ctx.S.thinkRuns.set(`bot-p-work:${i}`, {...p, key: `bot-p-work:${i}`,
      wake_started_at: i, finished: true, after_id: 0});
  }
  history.ctx.refreshThinkRunsForMessage(messageEvent(p, 6871));
  assert.equal(history.requests.length, 1, '200 cached executions cause one exact message refresh');
  history.requests.shift().resolve({entries: [], latest_id: 0, replies: [], process_ended_at: 108});
  await tick();
  assert.equal(history.timers.size, 0);

  const live = harness();
  live.ctx.startThinkBubble(wake(1));
  live.requests.shift().resolve({entries: [], latest_id: 0});
  await tick();
  const old = [...live.ctx.S.thinkBubbles.values()][0];
  old.status = 'inactive';
  live.ctx.startThinkBubble(wake(2));
  live.requests.shift().resolve({entries: [{id: 1, reasoning: 'B only'}], latest_id: 1});
  await tick();
  await live.ctx.loadThinking();
  assert.equal(old.status, 'inactive', 'another topic never reactivates old bubble');
  live.ctx.saveThinkingState();
  const resumed = harness(live.storage);
  resumed.ctx.restoreThinkingState();
  await resumed.ctx.loadThinking();
  assert.equal(resumed.requests.length, 2, 'unfinished wakes resume polling after reload');

  const race = harness();
  const http = harness();
  http.ctx.startThinkBubble(wake(null, 400));
  const httpSource = [...http.ctx.S.thinkRuns.values()][0];
  http.requests.shift().resolve({entries: [
    {id: 1, timestamp: 401, reasoning: 'before first HTTP reply'},
    {id: 2, timestamp: 403, reasoning: 'before second HTTP reply'},
    {id: 3, timestamp: 405, reasoning: 'continued work'},
  ], latest_id: 3, replies: [{message_id: 51, sent_at: 402}, {message_id: 52, sent_at: 404}]});
  await tick();
  let httpBubbles = [...http.ctx.S.thinkBubbles.values()];
  assert.deepEqual(httpBubbles.map(b => b.reply_message_id), [51, 52, null]);
  assert.deepEqual(httpBubbles.map(b => Array.from(b.entries, e => e.id)), [[1], [2], [3]]);
  const httpKeys = httpBubbles.map(b => b.key);
  await http.ctx.loadThinking();
  http.requests.shift().resolve({entries: [{id: 4, timestamp: 406, finished: true}], latest_id: 4,
    replies: [{message_id: 51, sent_at: 402}, {message_id: 52, sent_at: 404}]});
  await tick();
  httpBubbles = [...http.ctx.S.thinkBubbles.values()];
  assert.deepEqual(httpBubbles.map(b => b.key), httpKeys.slice(0, 2));
  assert.deepEqual(httpBubbles.map(b => Array.from(b.entries, e => e.id)), [[1], [2, 3]]);
  const httpRestore = harness(http.storage);
  httpRestore.ctx.restoreThinkingState();
  assert.deepEqual([...httpRestore.ctx.S.thinkBubbles.values()].map(b => b.reply_message_id), [51, 52]);

  const lateHttp = harness();
  lateHttp.ctx.startThinkBubble(wake(null, 500));
  lateHttp.requests.shift().resolve({entries: [], latest_id: 0, replies: [{message_id: 60, sent_at: 502}]});
  await tick();
  await lateHttp.ctx.loadThinking();
  lateHttp.requests.shift().resolve({entries: [{id: 1, timestamp: 501, reasoning: 'late flush'},
    {id: 2, timestamp: 503, finished: true}], latest_id: 2, replies: [{message_id: 60, sent_at: 502}]});
  await tick();
  assert.equal(lateHttp.ctx.S.thinkBubbles.size, 1);
  assert.equal([...lateHttp.ctx.S.thinkBubbles.values()][0].entries[0].reasoning, 'late flush');

  race.ctx.startThinkBubble(wake(null, 100));
  race.ctx.startThinkBubble(wake(null, 200));
  race.requests.shift().resolve({entries: [{id: 99, reasoning: 'new wake data'}], latest_id: 99});
  await tick();
  const first = [...race.ctx.S.thinkRuns.values()][0];
  assert.equal(first.after_id, 0, 'unbounded response must be refetched after newer wake');
  assert.equal([...race.ctx.S.thinkBubbles.values()][0].entries.length, 0);
  console.log('PASS: late data, consecutive replies, duplicate wake, refresh, topic isolation, wake race; JS syntax');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
