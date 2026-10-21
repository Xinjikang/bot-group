const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
let wheel, resolve, calls = [], renders = 0;
const box = {scrollTop: 0, scrollHeight: 600, clientHeight: 200,
  addEventListener(type, fn) { if (type === 'wheel') wheel = fn; },
  getBoundingClientRect: () => ({top: 0, bottom: 200}), querySelectorAll: () => [],
  querySelector: () => null};
const S = {messageView: 0, messageRequest: null, historyRequest: null, currentThread: null,
  messagesReady: false, msgs: [], lastMsgId: 0};
const ctx = {S, $: s => s === '#msg-list' ? box : null,
  api: url => { calls.push(url); return new Promise(r => resolve = r); },
  renderMessages: () => { renders++; box.scrollHeight += 300; },
  getComputedStyle: () => ({overflowY: 'auto'})};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('async function refreshMessages()'), html.indexOf('function captureThinkingScroll(')), ctx);
(async () => {
  let pending = ctx.refreshMessages();
  await ctx.refreshMessages();
  assert.equal(calls.length, 1);
  assert.equal(calls[0], '/messages?limit=100');
  resolve({messages: [{id: 151}, {id: 250}], has_more: true});
  await pending;
  assert.equal(S.lastMsgId, 250);
  assert.equal(box.scrollTop, box.scrollHeight);
  const event = {deltaY: -100, target: box, preventDefault() {}};
  box.scrollTop = 10;
  wheel(event);
  assert.equal(calls.length, 1, 'reaching top is handled by natural scrolling');
  box.scrollTop = 0;
  wheel(event);
  wheel(event);
  assert.equal(calls.length, 2, 'one request while loading');
  assert.equal(calls[1], '/messages?before_id=151&limit=100');
  resolve({messages: [{id: 51}], has_more: true});
  await new Promise(setImmediate);
  assert.equal(S.msgs[0].id, 51);
  assert.equal(S.lastMsgId, 250);
  assert.equal(box.scrollTop, 300, 'prepend preserves viewport height offset');
  box.scrollTop = 0;
  wheel({...event, target: {scrollHeight: 200, clientHeight: 50, parentElement: box}});
  assert.equal(calls.length, 2, 'nested scroll area does not load history');
  // The page starts midway through an expanded topic. Its header precedes the
  // visible message, but inserting older replies must preserve the message.
  let inserted = 0;
  const header = {dataset: {}, closest: () => ({dataset: {thread: '42'}}),
    getBoundingClientRect: () => ({top: 20 - box.scrollTop, bottom: 50 - box.scrollTop})};
  const message = {dataset: {messageId: '51'}, closest: () => ({dataset: {thread: '42'}}),
    getBoundingClientRect: () => ({top: 80 + inserted - box.scrollTop, bottom: 180 + inserted - box.scrollTop})};
  const hidden = {dataset: {messageId: '50'}, getBoundingClientRect: () => ({top: 0, bottom: 0})};
  box.querySelectorAll = selector => selector === '[data-message-id]' ? [hidden, message]
    : selector === '.msg-group-head' ? [header] : [header, hidden, message];
  box.querySelector = selector => selector.includes('data-message-id') ? message : header;
  ctx.renderMessages = () => { inserted = 900; box.scrollHeight += 1200; box.scrollTop = 0; };
  pending = ctx.loadOlderMessages();
  resolve({messages: [{id: 1}], has_more: true});
  await pending;
  assert.equal(message.getBoundingClientRect().top, 80, 'topic prepend preserves message pixel position even with visible header');
  assert.equal(box.scrollTop, 900, 'uses message displacement instead of header or total height');
  // A collapsed topic has no visible message, so retain its visible header.
  box.scrollTop = 0;
  box.querySelectorAll = selector => selector === '[data-message-id]' ? [hidden] : [header];
  ctx.renderMessages = () => { box.scrollHeight += 200; };
  pending = ctx.loadOlderMessages();
  resolve({messages: [], has_more: true});
  await pending;
  assert.equal(box.scrollTop, 0, 'collapsed header stays put');
  pending = ctx.loadOlderMessages();
  ctx.resetMessageWindow();
  S.currentThread = 42;
  S.msgs = [{id: 999}];
  resolve({messages: [{id: 1}], has_more: false});
  await pending;
  assert.equal(S.msgs[0].id, 999, 'stale history response is discarded');
  console.log('PASS: latest page, request deduplication, wheel boundary, nested scrolling, viewport offset, stale response');
})().catch(e => { console.error(e); process.exitCode = 1; });
