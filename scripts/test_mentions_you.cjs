// Run: node scripts/test_mentions_you.cjs
// bot-group req#178 task#334：@点名加底色 + 点到当前用户的「提到我」标记。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const start = html.indexOf('function NAME_OF(p) {');
const end = html.indexOf('// req#48 task#83：撤回一条消息');
assert(start > 0 && end > start, 'renderMsg slice not found');
const source = html.slice(start, end);

function makeContext(dict) {
  const t = (key, args) => {
    const entry = dict[key] || key;
    return String(entry).replace(/\{(\d+)\}/g, (whole, i) => args && args[i] !== undefined ? String(args[i]) : whole);
  };
  const ctx = {
    S: { userDisplayName: '主人', members: [{ profile: 'a', display_name: '甲' }],
      recoveryNotices: {}, isUserIdentity: false },
    safeJSON: (text, fallback) => { try { return JSON.parse(text); } catch { return fallback; } },
    esc: String, t,
    renderThinkBubble: () => ({ querySelector: () => null }),
    document: {
      createElement: tag => {
        const node = {
          tagName: String(tag).toUpperCase(), dataset: {}, style: {}, children: [],
          addEventListener() {}, setAttribute() {},
          querySelector: () => ({ after() {}, messageContentSignature: null, appendChild() {}, insertBefore() {}, addEventListener() {} }),
        };
        Object.defineProperty(node, 'innerHTML', {
          set(value) { node._html = value; }, get() { return node._html || ''; },
        });
        return node;
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx;
}

const zh = { mentions_you: '提到我', user: '用户', retracted: '已撤回' };
const en = { mentions_you: 'Mentions you', user: 'user', retracted: 'retracted' };

function render(ctx, m) {
  const div = ctx.renderMsg(m, []);
  return div._html;
}

// 1) 点到当前用户（profile==="user"）→ 中文标记
let ctx = makeContext(zh);
let html1 = render(ctx, { id: 1, ts: '2026-11-25T10:00:00', sender: 'a', sender_type: 'bot',
  content: '@user 看一下', mentions: JSON.stringify(['user']), thread_id: 7 });
assert(html1.includes('tag-mentions-you'), 'mentions-you tag rendered for zh');
assert(html1.includes('提到我'), 'zh label is 提到我');
assert(html1.includes('<span class="mention">@主人</span>'), 'mention span contains the display name');

// 2) 同上下文英文词条 → 英文标记
ctx = makeContext(en);
html1 = render(ctx, { id: 1, ts: '2026-11-25T10:00:00', sender: 'a', sender_type: 'bot',
  content: '@user 看一下', mentions: JSON.stringify(['user']), thread_id: 7 });
assert(html1.includes('Mentions you'), 'en label is Mentions you');

// 3) 点别人 → 无标记
html1 = render(ctx, { id: 2, ts: '2026-11-25T10:01:00', sender: 'user', sender_type: 'user',
  content: '@a 看下', mentions: JSON.stringify(['a']), thread_id: 7 });
assert(!html1.includes('tag-mentions-you'), 'no badge when current user not mentioned');

// 4) 无 mentions / 无 thread_id 的散聊 → 无标记
html1 = render(ctx, { id: 3, ts: '2026-11-25T10:02:00', sender: 'a', sender_type: 'bot',
  content: 'hi', mentions: JSON.stringify([]) });
assert(!html1.includes('tag-mentions-you'), 'no badge without mentions');
html1 = render(ctx, { id: 4, ts: '2026-11-25T10:03:00', sender: 'a', sender_type: 'bot',
  content: '@user', mentions: JSON.stringify(['user']), thread_id: null });
assert(html1.includes('tag-mentions-you'), 'p2p mention still badges');

// 5) 撤回态消息不渲染标记、断链消息整体隐藏
html1 = render(ctx, { id: 5, ts: '2026-11-25T10:04:00', sender: 'a', sender_type: 'bot',
  content: '@user', mentions: JSON.stringify(['user']), retract_by: 'user', retract_at: '2026-11-25T10:05:00' });
assert(!html1.includes('tag-mentions-you'), 'retracted message shows no badge');
const hidden = ctx.renderMsg({ id: 6, ts: '2026-11-25T10:06:00', sender: 'a', sender_type: 'engine',
  content: '[互@断链]', mentions: JSON.stringify(['user']) }, []);
assert.equal(hidden.style.display, 'none', 'engine link-break message stays hidden');

console.log('PASS: mentions-you badge zh/en, non-self/no-mention absence, p2p badge, retracted/hidden');
