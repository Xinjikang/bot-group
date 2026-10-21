const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('frontend/message_navigation.js', 'utf8');
const key = 'ge_mentions_v1:p';
const storage = () => ({
  values: new Map(), fail: false,
  getItem(k) { return this.values.get(k) ?? null; },
  setItem(k, value) { if (this.fail) throw new Error('QuotaExceededError'); this.values.set(k, value); },
});
function tab(local, session = storage()) {
  const listeners = {}, buttons = {};
  for (const name of ['#message-bottom', '#message-mentions']) buttons[name] = {
    hidden: true, setAttribute() {}, addEventListener() {},
  };
  const box = {scrollHeight: 100, scrollTop: 0, clientHeight: 100, after() {}, addEventListener() {}};
  const ctx = {
    localStorage: local, sessionStorage: session,
    S: {currentProject: 'p', currentThread: null, messagesReady: false},
    document: {querySelector: () => box, createElement: () => ({
      setAttribute() {}, querySelector: s => buttons[s],
    }), addEventListener() {}},
    window: {addEventListener(type, callback) { listeners[type] = callback; }},
    ResizeObserver: class { observe() {} }, MutationObserver: class { observe() {} },
    performance: {now: () => 0}, clearTimeout() {}, setTimeout() {},
    console: {warn() {}}, api: async () => ({messages: []}),
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return {ctx, session, listeners, button: buttons['#message-mentions'],
    read(id) { ctx.messageNavigation.observe([{id, project: 'p', retract_by: 'user'}]); },
    poll() { ctx.messageNavigation.observe([]); },
  };
}
function seed(local, cursor = 10, pending = [{id: 5, thread_id: null}]) {
  local.setItem(key, JSON.stringify({cursor, pending}));
}

// A second page's stale queue must never resurrect acknowledged IDs.
{
  const local = storage(); seed(local);
  const first = tab(local), second = tab(local);
  first.read(5);
  second.poll();
  assert.equal(tab(local).button.hidden, true, 'stale page restored an acknowledged mention');
}

// Failed persistent writes still preserve acknowledgement across this tab's reload.
{
  const local = storage(); seed(local);
  const first = tab(local); local.fail = true;
  first.read(5);
  assert.equal(first.button.hidden, true);
  assert.equal(tab(local, first.session).button.hidden, true, 'quota failure restored an acknowledged mention');
}

// Merging independent pages keeps newer unread messages and the highest cursor.
{
  const local = storage(); seed(local);
  const first = tab(local);
  seed(local, 20, [{id: 5, thread_id: null}, {id: 15, thread_id: 7}]);
  first.read(5);
  const saved = JSON.parse(local.getItem(key));
  assert.equal(saved.cursor, 20);
  assert.deepEqual(saved.pending, [{id: 15, thread_id: 7}]);
  const second = tab(local);
  first.read(15);
  second.listeners.storage({key, storageArea: local});
  assert.equal(second.button.hidden, true, 'storage events update the other page');
}
console.log('PASS: stale page merge, storage failure reload, newer unread preservation and storage event');
