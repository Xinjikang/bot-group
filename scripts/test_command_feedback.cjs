const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('frontend/attachments.js', 'utf8');
const start = code.indexOf('const commandReceipts =');
const end = code.indexOf('const displayedImageUrls', start);
const notices = [];
const receiptText = {textContent:''}, dismissReceipt = {};
const receipt = {hidden:true, dataset:{}, querySelector: selector => selector === 'button' ? dismissReceipt : receiptText};
let response;
let dismissNotice;
let holdNotice = false;
const context = vm.createContext({
  S: {currentProject:'p'}, document: {querySelector: () => receipt},
  imageDestinationKey: () => 'p', imageDrafts: new Map([['p', []]]), imageSends: new Set(),
  refreshImageEditors() {}, api: async () => response, showAlert: text => {
    notices.push(text);
    if (holdNotice) return new Promise(resolve => { dismissNotice = resolve; });
  },
  URL: {revokeObjectURL() {}},
});
vm.runInContext(code.slice(start, end), context);
(async () => {
  response = {handled: true, ok: false, detail: '目标忙碌'};
  await assert.rejects(context.sendImageMessage({text:'/new @bot', project:'p'}), /目标忙碌/);
  assert(context.imageDrafts.has('p'), 'failure preserves draft');
  assert.equal(context.imageSends.size, 0);
  assert.equal(receipt.hidden, false);
  assert.equal(receipt.dataset.state, 'error');
  assert.equal(receiptText.textContent, '目标忙碌');
  response = {handled: true, ok: true, detail: '/help details'};
  assert.equal(await context.sendImageMessage({text:'/help', project:'p'}), true);
  assert.deepEqual(notices, ['/help details']);
  holdNotice = true;
  response = {handled: true, ok: true, command: '/new',
    detail: '@bot 的工作会话已切换。下一条发给该会话的消息将使用新会话。'};
  context.imageDrafts.set('p', []);
  let finished = false;
  const sending = context.sendImageMessage({text:'/new @bot', project:'p'})
    .then(result => { finished = true; return result; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(notices[1], response.detail);
  assert.equal(receiptText.textContent, response.detail, 'inline receipt appears before modal acknowledgment');
  assert.equal(receipt.dataset.state, 'success');
  assert.equal(finished, false, 'send waits for the user to dismiss the feedback');
  assert(context.imageDrafts.has('p'), 'draft remains until feedback is acknowledged');
  assert(context.imageSends.has('p'), 'repeat submission is blocked while feedback is open');
  assert.equal(await context.sendImageMessage({text:'/new @bot', project:'p'}), false);
  dismissNotice();
  assert.equal(await sending, true);
  assert.equal(context.imageSends.size, 0);
  assert.equal(context.imageDrafts.has('p'), false);
  assert.equal(receipt.hidden, false, 'receipt survives input cleanup and modal dismissal');
  context.S.currentProject = 'other';
  context.renderCommandReceipt();
  assert.equal(receipt.hidden, true, 'receipt is isolated by project');
  context.S.currentProject = 'p';
  context.renderCommandReceipt();
  assert.equal(receipt.hidden, false, 'receipt is restored on return');
  holdNotice = false;
  response = {message_id: 123};
  assert.equal(await context.sendImageMessage({text:'hello', project:'p'}), true);
  assert.equal(notices.length, 2, 'ordinary messages do not show command dialogs');
  assert.equal(receipt.hidden, false, 'ordinary sends preserve the last command receipt');
  dismissReceipt.onclick();
  assert.equal(receipt.hidden, true, 'receipt closes only on explicit dismissal or replacement');
  context.api = async () => { throw new Error('network failed'); };
  await assert.rejects(context.sendImageMessage({text:'/new @bot', project:'p'}), /network failed/);
  assert.equal(receiptText.textContent, 'network failed');
  assert.equal(receipt.dataset.state, 'error');
  console.log('PASS: slash feedback, rejected command draft retention, ordinary send');
})().catch(error => {console.error(error); process.exitCode = 1;});
