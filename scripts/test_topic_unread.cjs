const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const box = {scrollTop: 0, scrollHeight: 100, clientHeight: 100, addEventListener() {}};
let calls = [], payload = [], resolveFetch, renders = 0;
const S = {currentThread: 1, currentProject: 'p', messageView: 1, messagesReady: false, msgs: []};
const context = {S, console, document: {visibilityState: 'visible'},
  $: () => box, renderMessages: () => {renders++;}, renderThreads: () => {},
  messageProjectUrl: url => url,
  api: async (url, options) => {
    calls.push({url, options});
    if (url.startsWith('/messages')) return resolveFetch ? new Promise(r => resolveFetch(r)) : {messages: payload};
    if (url === '/inbox/summary') return {summary: [{profile:'bot',pending:9}],
      threads:[{thread_id:1,profile:'user',pending:1},{thread_id:2,profile:'bot',pending:2}]};
    return {seen:1};
  }};
vm.createContext(context);
vm.runInContext(extract('async function loadInboxSummary()', 'function renderThreads()'), context);
vm.runInContext(extract('async function refreshMessages()', 'function resetMessageWindow()'), context);
(async () => {
  await context.loadInboxSummary();
  assert.equal(S.threadInboxSummary[1].bot, undefined, 'global pending never contaminates a topic');
  assert.equal(S.threadInboxSummary[2].bot, 2);
  calls = []; payload = [{id:5},{id:8}];
  await context.refreshMessages();
  const seen = calls.find(c => c.url === '/inbox/seen');
  assert.deepEqual(JSON.parse(seen.options.body), {profile:'user',thread_id:1,through_id:8});
  assert.equal(renders,1);
  calls = []; context.document.visibilityState = 'hidden';
  await context.refreshMessages();
  assert.ok(!calls.some(c => c.url === '/inbox/seen'), 'background tab preserves unread');
  calls = []; context.document.visibilityState = 'visible';
  let release;
  resolveFetch = r => {release=r;};
  const pending = context.refreshMessages();
  S.currentThread = 2; S.messageView++;
  release({messages:[{id:9}]});
  await pending;
  assert.ok(!calls.some(c => c.url === '/inbox/seen'), 'stale response after navigation cannot mark read');
  calls = []; resolveFetch = null;
  context.api = async (url, options) => {
    calls.push({url,options});
    if (url === '/inbox/seen') throw new Error('offline');
    return {messages:[]};
  };
  context.console = {warn: () => {}};
  await context.markVisibleThreadRead(2, [{id:9}], S.messageView);
  assert.equal(calls.length,1,'failed acknowledgement keeps summary unchanged');
  console.log('topic unread: 5 scenarios passed');
})().catch(err => {console.error(err);process.exitCode=1;});
