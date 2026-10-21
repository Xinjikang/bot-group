// Selected labels retain the existing member.profile routing identity.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const c = vm.createContext({});
vm.runInContext(fs.readFileSync('frontend/member_identity.js', 'utf8'), c);
const hermes = {profile: 'default', host_profile: 'default', display_name: 'Hermes'};
const codex = {profile: 'bot-36', host_profile: 'default', display_name: 'Codex'};
const members = [hermes, codex];
function input(value = '') {
  const handlers = {};
  const ta = {value, selectionStart: 0, selectionEnd: 0,
    addEventListener(name, fn) { handlers[name] = fn; }, handlers};
  c.bindMemberMentions(ta);
  return ta;
}
function select(ta, member, start = ta.value.length, end = start) {
  return c.insertMemberMention(ta, member, start, end, 'p');
}
function edit(ta, start, end, text, type = 'insertText') {
  ta.selectionStart = start; ta.selectionEnd = end;
  ta.handlers.beforeinput({inputType: type});
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  ta.handlers.input({inputType: type});
}
const wire = (ta, roster = members, project = 'p') => c.serializeMemberMentions(ta, project, roster);
let ta = input();
select(ta, codex); select(ta, hermes);
assert.equal(ta.value, '@Codex（default） @Hermes（default） ');
assert.equal(wire(ta), '@bot-36 @default');
edit(ta, 0, 0, 'hello ');
edit(ta, ta.value.length, ta.value.length, '你好');
assert.equal(wire(ta), 'hello @bot-36 @default 你好');
assert.throws(() => wire(ta, [hermes]), /重新选择/);
assert.throws(() => wire(ta, members, 'other'), /重新选择/);
assert.equal(wire(ta, members.map(m => ({...m, display_name: 'renamed'}))), 'hello @bot-36 @default 你好');

// Same visible label still retains the member selected at each position.
const twin = {...codex, profile: 'bot-37'};
ta = input(); select(ta, codex); select(ta, twin);
assert.equal(wire(ta, [codex, twin]), '@bot-36 @bot-37');
edit(ta, 0, '@Codex（default） '.length, '');
assert.equal(wire(ta, [codex, twin]), '@bot-37');
ta.handlers.beforeinput({inputType: 'historyUndo'});
ta.value = '@Codex（default） @Codex（default） ';
ta.handlers.input({inputType: 'historyUndo'});
assert.equal(wire(ta, [codex, twin]), '@bot-36 @bot-37');
select(ta, twin, 0, '@Codex（default） '.length);
assert.equal(wire(ta, [codex, twin]), '@bot-37 @bot-37');

// Editing inside a selected token drops the old binding; plain text is untouched.
ta = input(); select(ta, codex);
edit(ta, 1, 6, 'other');
assert.equal(wire(ta), '@other（default）');
ta = input('@bot-36 ordinary email@example.com');
assert.equal(wire(ta), ta.value);
ta = input('/new @co tail');
select(ta, codex, 5, 9);
assert.equal(wire(ta), '/new @bot-36 tail');

// Independent inputs and programmatic clear cannot reuse previous identities.
const other = input('@bot-36');
assert.equal(wire(other), '@bot-36');
ta.value = ''; assert.equal(wire(ta), '');
ta.value = '@Codex（default）'; assert.equal(wire(ta), ta.value);
console.log('PASS: shared mention labels, identity binding, editing, scope, commands and undo');
