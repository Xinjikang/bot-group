// Run: node scripts/test_composer_limit.cjs
// req#82 task#142：composer-text 单条 2000 UTF-16 码元上限——maxlength 属性 + 发送双保险
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');

// ① textarea 有 maxlength="2000" 属性（HTML5 maxlength 按 UTF-16 码元计数，与引擎口径一致）
const ta = html.match(/<textarea id="composer-text"([^>]*)>/);
assert.ok(ta, 'composer-text textarea exists');
assert.ok(/maxlength="2000"/.test(ta[1]), 'maxlength=2000 on composer-text');

// ② 发送前双保险：程序化赋值可绕 maxlength，submit 里必须再拦一次
const slice = (a, b) => {
  const start = html.indexOf(a), end = html.indexOf(b, start);
  assert.ok(start >= 0 && end > start, `Missing script boundary: ${a} / ${b}`);
  return html.slice(start, end);
};
const section = slice('$("#composer").addEventListener("submit"', '$("#composer-text").addEventListener("keydown"');
assert.ok(section.includes('text.length > 2000'), 'send path re-checks UTF-16 code-unit limit');
assert.ok(section.includes('showAlert'), 'over-limit surfaces via alert, never silently drops');

// ③ 口径验证：JS string .length = UTF-16 码元（含 surrogate pair 场景）
// "😀" = 1 码点 = 2 UTF-16 码元；2000 个 emoji 码点 = 4000 码元 → 必须按 4000 拦
const emojiText = '😀'.repeat(1001); // 1001 码点 = 2002 UTF-16 码元
assert.equal(emojiText.length, 2002, 'JS length counts UTF-16 code units, not code points');
assert.ok(emojiText.length > 2000, 'emoji-heavy message over 1001 code points is blocked');

// ④ vm 跑发送分支：2001 字符 → alert 且不发请求；2000 字符 → 正常发
const elements = new Map();
const element = selector => {
  if (!elements.has(selector)) elements.set(selector, {value: '', dataset: {}, hidden: false,
    addEventListener(type, handler) { this[type] = handler; },
    appendChild(n) { return n; }, querySelector() { return null; }});
  return elements.get(selector);
};
let alerts = [], posted = [];
const ctx = {S: {currentThread: null, currentProject: null},
  $: element, esc: s => s, NAME_OF: p => p, showAlert: m => alerts.push(m),
  requestSubmit() {}, composerImages: {refresh() {}},
  sendImageMessage: async (body) => { posted.push(body); return true; },
  api(url, options) { posted.push({url, options}); return Promise.resolve({}); },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('frontend/member_identity.js', 'utf8'), ctx);
ctx.mentionCandidates = () => [];
vm.runInContext(section, ctx);
(async () => {
const form = element('#composer');
const composerTa = element('#composer-text');

// 2001 字符：拦截 + 提示，不发包
composerTa.value = '字'.repeat(2001);
form.submit({preventDefault() {}});
await new Promise(r => setImmediate(r));
assert.equal(alerts.length, 1, '2001 chars alerts');
assert.ok(alerts[0].includes('2001/2000'), 'alert shows over-limit count');
assert.equal(posted.length, 0, 'over-limit message is not sent');

// 2000 字符（含 emoji 混排）：正常发送
alerts.length = 0; posted.length = 0;
composerTa.value = '字'.repeat(1999) + '😀'; // 2000 码点 = 2001 UTF-16 码元 → 拦（口径统一的关键用例）
form.submit({preventDefault() {}});
await new Promise(r => setImmediate(r));
assert.equal(alerts.length, 1, '2001 code units (1999 chars + 1 emoji) is blocked');
// 纯 2000 码元：放行
alerts.length = 0; posted.length = 0;
composerTa.value = '字'.repeat(2000); // 2000 UTF-16 码元
form.submit({preventDefault() {}});
await new Promise(r => setImmediate(r));
assert.equal(alerts.length, 0, 'exactly 2000 code units passes');
assert.equal(posted.length, 1, 'message posted');

console.log('PASS: composer-text maxlength=2000 (UTF-16 code units) + send-path double guard');
})().catch(e=>{console.error(e);process.exitCode=1;});
