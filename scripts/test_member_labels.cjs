const fs = require('fs'), vm = require('vm'), assert = require('assert');
const path = require('path');
const c = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/member_identity.js'), 'utf8'), c);
for (const profile of ['default', 'bot-36']) {
  const member = {profile, host_profile: 'default', display_name: '知夏'};
  assert.equal(c.memberLabel(member), '知夏（default）');
  assert.equal(member.profile, profile);
}
assert.equal(c.memberLabel({profile: 'legacy', display_name: '旧成员'}), '旧成员（legacy）');
assert.equal(c.memberLabel({profile: 'bot-36', host_profile: 'default', display_name: ''}), 'default');
console.log('member label tests passed');
