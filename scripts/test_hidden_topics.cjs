// Run: node scripts/test_hidden_topics.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const threads = [{id: 1, hidden: 1, title: 'private', status: 'closed', close_conclusion: 'secret'},
  {id: 2, hidden: 0, title: 'visible', status: 'open'}];
const ctx = {S: {threads, msgs: [{id: 10, thread_id: 1}, {id: 20, thread_id: 2}, {id: 30, thread_id: null}],
  thinkBubbles: new Map([['hidden', {thread_id: 1}], ['direct', {thread_id: null, reply_message_id: 30}]]),
  collapsedThreads: new Set(), showHidden: true},
  document: {createElement: () => ({children: [], appendChild(node) { this.children.push(node); }})},
  renderThreadGroup: group => `topic:${group.threadId}`,
  renderMsg: message => `direct:${message.id}`, renderThinkBubble: () => 'thinking'};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('function bubbleInCurrentProject('), html.indexOf('async function loadOlderMessages(')), ctx);
vm.runInContext(html.slice(html.indexOf('function renderGroupedMessages('), html.indexOf('function renderThreadGroup(')), ctx);
assert.deepEqual(Array.from(ctx.renderGroupedMessages().children), ['topic:2', 'direct:30'],
  'hidden topic content, conclusion and reasoning never enter overview, even when sidebar hidden list is expanded');
threads[0].hidden = 0;
threads[0].status = 'open';
assert.deepEqual(Array.from(ctx.renderGroupedMessages().children), ['topic:1', 'topic:2', 'direct:30'],
  'unhidden topic returns to overview');
console.log('PASS: hidden topic filtering and unhide restoration; direct messages retained');
