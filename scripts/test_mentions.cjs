// Run: node scripts/test_mentions.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const source = html.slice(html.indexOf('const mpState ='), html.indexOf('// ======================== 成员多选下拉'));
const input = () => ({value: '@', selectionStart: 1, selectionEnd: 1, isConnected: true,
  addEventListener() {}, focus() {}, getBoundingClientRect: () => ({left: 0, top: 400, bottom: 440})});
const main = input(), inline = input();
const popup = {hidden: true, style: {}, offsetHeight: 100, querySelectorAll: () => []};
const ctx = {S: {currentThread: null, members: [
  {profile: 'a', display_name: '甲', active: 1},
  {profile: 'b', display_name: '乙', active: 1},
  {profile: 'off', active: 0}, {profile: 'user', active: 1},
], threads: [{id: 1, members: '["a","off","user"]'}, {id: 2, members: ['b']}]},
  $: selector => selector === '#mention-pop' ? popup : main,
  safeJSON: (text, fallback) => { try { return JSON.parse(text); } catch { return fallback; } },
  bindCommandInput() {}, esc: String, window: {innerHeight: 800}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('frontend/member_identity.js', 'utf8'), ctx);
vm.runInContext(source, ctx);
const profiles = (id, q) => Array.from(ctx.mentionCandidates(id, q), m => m.profile);
assert.deepEqual(profiles(null), ['a', 'b']);
assert.deepEqual(profiles(1), ['a']);
assert.deepEqual(profiles(2), ['b']);
assert.deepEqual(profiles(999), [], 'missing topic never falls back to global bots');
assert.deepEqual(profiles(1, '乙'), [], 'search stays inside topic membership');
assert.deepEqual(profiles(1, '甲'), ['a']);
ctx.mpDetect(inline, 2);
ctx.mpApply(0);
assert.equal(inline.value, '@乙（b） ', 'inline topic picker inserts the shared label into its own input');
assert.equal(ctx.serializeMemberMentions(inline, undefined, ctx.mentionCandidates(2)), '@b');
assert.equal(main.value, '@', 'main composer remains untouched');
ctx.mpDetect(main, 1);
ctx.S.threads[0].members = '[]';
ctx.mpApply(0);
assert.equal(main.value, '@', 'removed member cannot be selected from stale popup');
console.log('PASS: global/topic scope, search, inactive/user filtering, inline target, removed member');
