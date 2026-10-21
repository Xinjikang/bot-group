const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const opening = {id: 2325, thread_id: 10040, ts: '2026-09-14T19:58:57'};
const middle = {id: 2359, thread_id: 10042, ts: '2026-09-14T21:03:00'};
const tail = [{id: 2398, thread_id: 10040, ts: '2026-09-14T21:25:47'},
  {id: 2401, thread_id: 10040, ts: '2026-09-14T21:34:25'}];
const ctx = {S: {msgs: [middle, ...tail], hasOlderMessages: true,
  threads: [{id: 10040, title: 'next', first_message_id: 2325, created_at: opening.ts},
    {id: 10042, title: 'chat', first_message_id: 2359, created_at: middle.ts}],
  thinkBubbles: new Map(), collapsedThreads: new Set()},
  document: {createElement: () => ({children: [], appendChild(n) { this.children.push(n); }})},
  renderThreadGroup: g => g};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('function bubbleInCurrentProject('), html.indexOf('async function loadOlderMessages(')), ctx);
vm.runInContext(html.slice(html.indexOf('function renderGroupedMessages('), html.indexOf('function renderThreadGroup(')), ctx);
let groups = ctx.renderGroupedMessages().children;
assert.deepEqual(Array.from(groups, g => g.threadId), [10040, 10042], 'initial tail stays in original topic position');
assert.equal(groups[0].partial, true);
assert.equal(groups[1].partial, false, 'complete topic does not show partial warning');
ctx.S.msgs = [opening, middle, ...tail];
groups = ctx.renderGroupedMessages().children;
assert.deepEqual(Array.from(groups, g => g.threadId), [10040, 10042], 'older page does not move or duplicate topic');
assert.equal(groups[0].msgs.length, 3);
assert.equal(groups[0].partial, false);
// Existing service versions still provide a stable opening timestamp.
delete ctx.S.threads[0].first_message_id;
delete ctx.S.threads[1].first_message_id;
ctx.S.msgs = [middle, ...tail];
assert.deepEqual(Array.from(ctx.renderGroupedMessages().children, g => g.threadId), [10040, 10042]);
console.log('PASS: initial split-page topic position, one group per topic, history merge, partial labels, older service fallback');
