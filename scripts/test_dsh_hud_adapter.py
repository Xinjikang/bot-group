"""Execute the HUD adapter with delayed and unavailable tool registrations."""
import json
import shutil
import subprocess
import unittest

from engine import dsh_hud


HARNESS = r'''
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    const adapter = await import('data:text/javascript;base64,' + Buffer.from(input.source).toString('base64'));
    const required = input.required;
    let registered = required.filter(name => !name.startsWith('mcp__'));
    let allowed;
    let guard;
    let created;
    const sections = [{name:'persona', text:'EXISTING_SUFFIX'}];
    const agent = {ctx:{tools:{restrict(filter) { allowed = filter.allow; }}}};
    const ctx = {
        systemPrompt:{section(section) { sections.push(section); }, getSectionOrder() { return 100; }},
        tools:{schemas(scope) {
            assert.equal(scope, agent);
            return [...registered, 'pwsh'].filter(name => !allowed || allowed.includes(name)).map(name => ({name}));
        }, guard(callback) { guard = callback; }},
        on(event, callback) { assert.equal(event, 'agent/created'); created = callback; }
    };
    adapter.apply(ctx, {system:'HUD_SYSTEM'});
    assert.deepEqual(sections.map(s => s.text), ['EXISTING_SUFFIX', 'HUD_SYSTEM']);
    assert.equal(guard({name:'read'}), undefined);
    assert.equal(typeof guard({name:'pwsh'}), 'string');
    if (input.mode === 'late') {
        setTimeout(() => { registered = required; }, 80);
        await created({agent});
        assert.deepEqual(new Set(allowed), new Set(required));
    } else if (input.mode === 'missing') {
        let clock = 0;
        Date.now = () => clock += 5000;
        await assert.rejects(created({agent}), /missing required tools:.*project_config/);
        assert.equal(allowed, undefined);
    } else {
        const controller = new AbortController();
        controller.abort();
        await assert.rejects(created({agent, signal:controller.signal}), /abort/i);
        assert.equal(allowed, undefined);
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


class AdapterTests(unittest.TestCase):
    def test_late_missing_and_cancelled_tool_registration(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js is required to validate the runtime adapter')
        for mode in ('late', 'missing', 'cancelled'):
            with self.subTest(mode=mode):
                result = subprocess.run([node, '-e', HARNESS], input=json.dumps({
                    'source': dsh_hud.TOOL_ADAPTER, 'required': sorted(dsh_hud.REQUIRED_TOOLS), 'mode': mode}),
                    text=True, encoding='utf-8', capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(dsh_hud.TOOL_MARKER in result.stderr, mode == 'late')


if __name__ == '__main__':
    unittest.main()
