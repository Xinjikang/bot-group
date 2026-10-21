// Cache retention and quota recovery, using production functions and isolated storage.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const code = html.slice(html.indexOf('const THINKING_STORAGE_KEY'), html.indexOf('function restoreThinkingState()'));
const now = Date.now(), day = 86400000;
const storage = new Map([['other-preference', 'keep']]);
let quota = Infinity, blocked = false, warnings = 0;
const ctx = {S: {thinkRuns: new Map(), thinkBubbles: new Map()}, Date, Map, JSON, Number,
  console: {warn() { warnings++; }},
  localStorage: {setItem(key, value) {
    if (blocked || value.length > quota) {
      const error = new Error('storage rejected');
      error.name = blocked ? 'SecurityError' : 'QuotaExceededError'; throw error;
    }
    storage.set(key, value);
  }}
};
vm.createContext(ctx); vm.runInContext(code, ctx);
const run = (id, age = 0, active = false) => ({key: `r${id}`, wake_started_at: id,
  cacheUpdatedAt: now - age, finished: !active, after_id: 42, currentKey: `b${id}`, tailStart: 1});
const bubble = (id, size = 20) => ({key: `b${id}`, sourceKey: `r${id}`,
  entries: [{id: 42, reasoning: 'x'.repeat(size)}], status: 'thinking'});
let snapshot = ctx.prepareThinkingCache([run(1, 8*day), run(2, 6*day), run(3, 8*day, true)],
  [bubble(1), bubble(2), bubble(3)], 524288, now);
assert.deepEqual(Array.from(snapshot.runs, r => r.key), ['r3', 'r2']);
assert.deepEqual(Array.from(snapshot.bubbles, b => b.sourceKey), ['r3', 'r2']);
const many = Array.from({length: 150}, (_, i) => run(i));
snapshot = ctx.prepareThinkingCache(many, [], 524288, now);
assert.equal(snapshot.runs.length, 100);
assert.equal(snapshot.runs[0].key, 'r149');
assert.equal(snapshot.runs.at(-1).key, 'r50');
const huge = bubble(5, 1500000), original = JSON.stringify(huge);
snapshot = ctx.prepareThinkingCache([run(5, 0, true)], [huge], 8192, now);
assert.ok(JSON.stringify(snapshot).length <= 8192);
assert.equal(snapshot.runs[0].after_id, 42);
assert.equal(snapshot.bubbles[0].cacheTruncated, true);
assert.equal(JSON.stringify(huge), original, 'live content remains complete');
snapshot = ctx.prepareThinkingCache([run(5)], [huge], 8192, now);
assert.equal(snapshot.bubbles[0].cacheTruncated, true, 'large recent completed run retains a tail');
const legacy = {...run(6)}; delete legacy.cacheUpdatedAt;
snapshot = ctx.prepareThinkingCache([legacy], [bubble(6)], 524288, now);
assert.equal(snapshot.runs[0].cacheUpdatedAt, now, 'legacy data gets a bounded grace period');
snapshot = ctx.prepareThinkingCache(snapshot.runs, snapshot.bubbles, 524288, now + 8*day);
assert.equal(snapshot.runs.length, 0, 'saving again does not extend completed retention');
quota = 8192;
ctx.S.thinkRuns.set('r5', run(5, 0, true)); ctx.S.thinkBubbles.set('b5', huge);
ctx.S.thinkRuns.set('r7', run(7)); ctx.S.thinkBubbles.set('b7', bubble(7, 15000));
ctx.saveThinkingState();
const saved = storage.get('group-engine.thinking.v2');
assert.ok(saved.length <= quota);
assert.ok(JSON.parse(saved).runs.some(r => r.key === 'r5'));
assert.equal(storage.get('other-preference'), 'keep');
assert.equal(ctx.S.thinkBubbles.get('b5').entries[0].reasoning.length, 1500000);
blocked = true; ctx.saveThinkingState(); ctx.saveThinkingState();
assert.equal(warnings, 1, 'unavailable storage does not flood the console');
assert.equal(storage.get('group-engine.thinking.v2'), saved, 'failed writes preserve prior snapshot');
blocked = false; ctx.saveThinkingState(); blocked = true; ctx.saveThinkingState();
assert.equal(warnings, 2, 'a later failure can report again after recovery');
console.log('PASS: TTL, run cap, active priority, oversized tails, migration, quota retry, data preservation');
