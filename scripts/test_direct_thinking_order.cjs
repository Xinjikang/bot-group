const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const bubble = {key:'wake', thread_id:null, status:'thinking', spawned_at:'2026-09-15T12:52:00'};
const trigger = {id:1, thread_id:null, ts:'2026-09-15T12:52:00'};
const ctx = {S:{msgs:[trigger],threads:[],thinkBubbles:new Map([['wake',bubble]]),collapsedThreads:new Set()},
  document:{createElement:()=>({children:[],appendChild(n){this.children.push(n);}})},
  renderThinkBubble:b=>`thinking:${b.key}`,
  renderMsg:(m, bubbles)=>({id:m.id,bubbles:bubbles.map(b=>b.key)})};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('function bubbleInCurrentProject('),html.indexOf('async function loadOlderMessages(')),ctx);
vm.runInContext(html.slice(html.indexOf('function renderGroupedMessages('),html.indexOf('function renderThreadGroup(')),ctx);
const render = () => JSON.parse(JSON.stringify(ctx.renderGroupedMessages().children));
assert.deepEqual(render(),[{id:1,bubbles:[]},'thinking:wake'],'same-second wake follows user message');
ctx.S.msgs.push({id:2,thread_id:null,ts:'2026-09-15T12:52:01'});
assert.deepEqual(render(),[{id:1,bubbles:[]},{id:2,bubbles:[]},'thinking:wake'],'active bubble follows newer user messages');
bubble.status='inactive';
assert.deepEqual(render(),[{id:1,bubbles:[]},'thinking:wake',{id:2,bubbles:[]}],'ended bubble retains chronological position after same-second trigger');
bubble.status='replied';
bubble.reply_message_id=3;
ctx.S.msgs.push({id:3,thread_id:null,ts:'2026-09-15T12:53:00'});
assert.deepEqual(render(),[{id:1,bubbles:[]},{id:2,bubbles:[]},{id:3,bubbles:['wake']}],'reply binds thinking to reply instead of separate bubble');
assert.deepEqual(render(),[{id:1,bubbles:[]},{id:2,bubbles:[]},{id:3,bubbles:['wake']}],'redraw preserves reply binding');
const continued = {key:'continued', thread_id:null, status:'thinking', parent_id:3,
  reply_message_id:null, spawned_at:bubble.spawned_at};
ctx.S.thinkBubbles.set(continued.key, continued);
ctx.S.msgs.push({id:4,thread_id:null,ts:'2026-09-15T12:54:00'});
assert.deepEqual(render(),[{id:1,bubbles:[]},{id:2,bubbles:[]},{id:3,bubbles:['wake']},
  {id:4,bubbles:[]},'thinking:continued'], 'post-reply work stays visible after the latest messages');
console.log('PASS: same-second direct wake, later user message, ended anchor, reply binding and redraw');
