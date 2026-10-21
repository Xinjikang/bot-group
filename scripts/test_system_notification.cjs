const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const source = html.slice(html.indexOf('function renderMsg('), html.indexOf('async function retractMessage('));
const ctx = {
  document: {createElement: () => ({dataset:{}, style:{}, querySelector: s => s === '.msg-retract-btn' ? null : {}})},
  safeJSON: (s, fallback) => {try {return JSON.parse(s);} catch {return fallback;}},
  esc: s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  NAME_OF: p => p === 'default' ? '蛇蛇' : p,
  AV_CLASS: () => '', fmtTs: x => x, S: {isUserIdentity:false},
};
vm.createContext(ctx);
vm.runInContext(source, ctx);
const base = {id:1, sender:'engine', sender_type:'engine', mentions:'["default"]', ts:'now'};
const alert = ctx.renderMsg({...base, content:'@default 任务失败 <unsafe>'});
assert.ok(alert.innerHTML.includes('系统通知'));
assert.ok(alert.innerHTML.includes('<span class="mention">@蛇蛇</span>'));
assert.ok(alert.innerHTML.includes('&lt;unsafe&gt;'));
assert.ok(!alert.innerHTML.includes('修订任务'));
const edit = ctx.renderMsg({...base, content:JSON.stringify({task_id:7, fields:{title:'new'}, by:'dev'})});
assert.ok(edit.innerHTML.includes('修订任务#7'));
console.log('PASS: system alert identity, mention highlight, escaping and task revision');
