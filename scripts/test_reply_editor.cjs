// Run: node scripts/test_reply_editor.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
let composing = false, renders = 0;
const editor = {value: '尚未发送的草稿', selectionStart: 2, selectionEnd: 5,
  selectionDirection: 'forward', scrollTop: 30, isConnected: true,
  setAttribute(name) { this[name] = true; }, removeAttribute(name) { delete this[name]; },
  matches: selector => selector === '.tr-text', focus() { this.focused = true; },
  setSelectionRange(start, end, direction) {
    this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
  }};
const bar = {dataset: {thread: '42'}, editor};
const box = {scrollHeight: 500, scrollTop: 400, clientHeight: 100,
  messageView: 0,
  matches: () => false,
  querySelector: () => composing ? editor : null,
  querySelectorAll: () => [bar], contains: element => element === editor,
  set innerHTML(value) {
    assert.fail('same-view updates must never clear the message list');
  },
  appendChild() { editor.isConnected = true; this.scrollHeight = 700; }};
const ctx = {S: {msgs: [{id: 1}], currentThread: null, messageView: 0,
  // renderSignature（req#40 task#77）会读这些渲染状态字段
  threads: [], thinkBubbles: new Map(), collapsedThreads: []},
  $: () => box, document: {activeElement: editor, getSelection: () => null,
    addEventListener() {}, createDocumentFragment: () => ({childNodes: [], appendChild(node) { this.childNodes.push(node); }})},
  window: {addEventListener() {}},
  captureThinkingScroll() {}, restoreThinkingScroll() {}, activeBubblesForView: () => [],
  thinkingLayoutSignature: () => 'layout', updateHistoryHint() {}, renderChatImages() {},
  renderGroupedMessages() {
    renders++;
    return {};
  }};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('// req#40 task#77：渲染签名'), // renderSignature 定义在 renderMessages 之前（req#40 task#77），切片必须带上它
  html.indexOf('// 群聊总视图：话题消息')), ctx);
// Reconciliation owns DOM retention; verify the live-editor branch directly.
ctx.syncMessageNode({classList: {contains: () => true}, nodeType: 1}, {});
ctx.syncMessageChildren = () => {
  assert.equal(editor.isConnected, true, 'reply editor stays connected during reconciliation');
  box.scrollHeight = 700;
};
ctx.renderMessages();
assert.equal(editor.value, '尚未发送的草稿');
assert.equal(editor.focused, true);
assert.equal(editor['data-refresh-focus'], undefined, 'temporary focus style clears after focus restoration');
assert.equal(editor.selectionStart, 2);
assert.equal(editor.selectionEnd, 5);
assert.equal(editor.scrollTop, 30);
assert.equal(box.scrollTop, 400, 'incoming messages do not pull the editing viewport to bottom');
composing = true;
ctx.renderMessages();
assert.equal(renders, 1, 'IME composition defers destructive redraw');
composing = false;
// renderSignature（req#40 task#77）没变会跳过重绘——改变消息序列强制走重建路径
ctx.S.msgs = [{id: 1}, {id: 2}];
ctx.renderMessages();
assert.equal(renders, 2, 'render resumes after composition');
console.log('PASS: reply DOM reuse, draft/selection/focus/scroll retention, IME redraw deferral');
