"""Public gateway dispatch, admission recovery and HUD isolation."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import hud_gateway as bridge
from engine.hermes_gateway_client import GatewayTransportError


class Client:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.fail_submit = False
        self.fail_receipt = False
        self.tools = [{'name': name} for name in ('read_file', 'write_file', 'patch', 'search_files')]
        self.created = 0
        self.busy = False
        self.receipts = [{'status': 'terminal', 'outcome': 'completed',
                          'result': {'final_response': '{"text":"ready"}'}}]

    def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == 'runtime.describe':
            return {'capabilities': ['durable-admission-v1']}
        if method in ('session.create', 'session.resume'):
            if method == 'session.create':
                self.created += 1
            sid = params.get('session_id') or ('hud-session' if self.created == 1 else f'hud-session-{self.created}')
            return {'session_id': sid, 'running': self.busy, 'pending': [], 'revision': 1,
                    'execution_generation': 0, 'info': {'edit_approval_policy': 'workspace_session'}}
        if method == 'tools.show':
            return {'sections': [{'tools': self.tools}]}
        if method == 'prompt.submit':
            if self.fail_submit:
                raise GatewayTransportError('lost acknowledgement')
            self.callback({'method': 'event', 'params': {'session_id': 'other-session',
                          'type': 'reasoning.delta', 'payload': {'text': 'unrelated'}}})
            self.callback({'method': 'event', 'params': {'session_id': params['session_id'],
                          'type': 'reasoning.delta', 'payload': {'text': '核对项目目标'}}})
            return {'admission_id': params['submission_id']}
        if method == 'prompt.receipt':
            if self.fail_receipt:
                raise GatewayTransportError('disconnected')
            return self.receipts.pop(0) if len(self.receipts)>1 else self.receipts[0]
        return {}

    def close(self):
        self.calls.append(('close', {}))


class HudGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bot = {'runtime': 'hermes', 'config_root': str(self.root), 'profile': 'default'}
        self.saved = {}
        self.client = Client()
        self.updates = []
        self.connections = []
        def save(value):
            self.saved = copy.deepcopy(value)
        def connect(profile, **kwargs):
            self.client.callback = kwargs['on_event']
            self.connections.append((profile, Path(kwargs['home'])))
            return self.client, self.root, ['hermes']
        for target, replacement in (('_saved', lambda: copy.deepcopy(self.saved)), ('_save', save)):
            p=patch.object(bridge,target,replacement);p.start();self.addCleanup(p.stop)
        p=patch.object(bridge.gateway,'connection',side_effect=connect);p.start();self.addCleanup(p.stop)
        p=patch.object(bridge.time,'sleep');p.start();self.addCleanup(p.stop)

    def run_prompt(self, prompt='synthetic prompt', system='Dedicated guidance', workdir=None):
        return bridge.respond(self.bot, prompt, str(workdir or self.root),
                              lambda *args: self.updates.append(args), system_prompt=system)

    def test_completed_turns_reuse_session_with_fresh_admissions(self):
        self.run_prompt()
        first = copy.deepcopy(self.saved)
        self.saved = json.loads(json.dumps(self.saved))
        self.run_prompt('updated project snapshot')
        self.assertEqual(self.client.created, 1)
        self.assertEqual(self.saved['session_id'], first['session_id'])
        self.assertNotEqual(self.saved['admission_id'], first['admission_id'])
        submits = [p for m, p in self.client.calls if m == 'prompt.submit']
        self.assertEqual([p['text'] for p in submits], ['synthetic prompt', 'updated project snapshot'])
        self.assertEqual(sum(m == 'tools.show' for m, _ in self.client.calls), 4)
        self.assertFalse(any(m in ('session.detach', 'session.mutate') for m, _ in self.client.calls))

    def test_private_config_tool_uses_native_execution_scoped_definitions(self):
        server = {'command': 'python', 'args': ['hud_server.py'], 'env': {'HUD_TOOL_TOKEN': 'first'}}
        for token in ('first', 'next'):
            server['env']['HUD_TOOL_TOKEN'] = token
            bridge.respond(self.bot, 'save requested configuration ' + token, str(self.root),
                           lambda *args: None, system_prompt='Dedicated guidance', server=server)
        self.assertEqual(self.client.created, 1)
        create = next(p for m, p in self.client.calls if m == 'session.create')
        self.assertEqual(create['toolsets'], ['file', 'no_mcp'])
        self.assertTrue(create['verbose'])
        self.assertFalse(create['ignore_rules'])
        self.assertEqual([s['name'] for s in create['editor']['mcp_servers']], ['project_config'])
        self.assertFalse(any(m == 'tools.show' for m, _ in self.client.calls))
        bindings = [p['payload'] for m, p in self.client.calls
                    if m == 'session.mutate' and p['operation'] == 'editor_mcp']
        self.assertEqual(len(bindings), 2)
        self.assertEqual(bindings[-1]['mcp_servers'][0]['env'], [{'name': 'HUD_TOOL_TOKEN', 'value': 'next'}])
        self.assertEqual(sum(m == 'prompt.submit' for m, _ in self.client.calls), 2)

    def test_binding_changes_start_separate_sessions(self):
        self.run_prompt()
        self.bot['id'] = 2
        self.run_prompt()
        self.assertEqual(self.client.created, 2)
        self.run_prompt(system='Updated guidance')
        self.assertEqual(self.client.created, 3)
        self.run_prompt(system='Updated guidance', workdir=self.root / 'other')
        self.assertEqual(self.client.created, 4)
        self.bot['profile'] = 'other'
        self.run_prompt(system='Updated guidance', workdir=self.root / 'other')
        self.assertEqual(self.client.created, 5)
        self.assertEqual(self.connections[-1], ('other', self.root / 'profiles' / 'other'))

    def test_hud_generation_reset_starts_new_session(self):
        with bridge.owner('project-a', 1):
            self.run_prompt()
            self.run_prompt('next question')
        self.assertEqual(self.client.created, 1)
        with bridge.owner('project-a', 2):
            self.run_prompt()
        with bridge.owner('project-b', 2):
            self.run_prompt()
        self.assertEqual(self.client.created, 3)

    def test_resume_checks_tool_isolation_and_busy_state(self):
        self.run_prompt()
        self.client.tools = [{'name': 'terminal'}]
        with self.assertRaisesRegex(RuntimeError, '工具清单'):
            self.run_prompt('next question')
        self.client.tools = [{'name': name} for name in ('read_file', 'write_file', 'patch', 'search_files')]
        self.client.busy = True
        with self.assertRaisesRegex(RuntimeError, '仍有执行'):
            self.run_prompt('next question')
        self.assertEqual(sum(m == 'prompt.submit' for m, _ in self.client.calls), 1)
        self.client.busy = False
        self.run_prompt('next question')
        self.assertEqual(self.client.created, 1)

    def test_changed_input_reconciles_previous_then_reuses_session(self):
        self.client.fail_receipt = True
        with self.assertRaises(RuntimeError):
            self.run_prompt()
        self.client.fail_receipt = False
        self.run_prompt('new question')
        self.assertEqual(self.client.created, 1)
        self.assertEqual(sum(m == 'prompt.submit' for m, _ in self.client.calls), 2)

    def test_profile_switch_reconciles_on_original_connection(self):
        self.client.fail_receipt = True
        with self.assertRaises(RuntimeError):
            self.run_prompt()
        self.bot['profile'] = 'other'
        self.client.fail_receipt = False
        self.run_prompt()
        self.assertEqual(self.connections[-2:], [('default', self.root), ('other', self.root / 'profiles' / 'other')])
        self.assertEqual(self.client.created, 2)

    def test_legacy_record_recovers_before_first_session_binding(self):
        self.client.fail_receipt = True
        with self.assertRaises(RuntimeError):
            self.run_prompt()
        self.saved.pop('binding')
        self.saved.pop('profile')
        self.client.fail_receipt = False
        self.run_prompt()
        self.assertEqual(sum(m == 'prompt.submit' for m, _ in self.client.calls), 1)
        self.run_prompt('next question')
        self.run_prompt('another question')
        self.assertEqual(self.client.created, 2)

    def test_public_session_and_stream(self):
        self.assertEqual(json.loads(self.run_prompt()),{'text':'ready'})
        create=next(p for m,p in self.client.calls if m=='session.create')
        self.assertEqual(create['source'],'acp')
        self.assertEqual(create['toolsets'],['file','no_mcp'])
        self.assertEqual(create['editor'],{'mcp_servers':[]})
        self.assertFalse(create['ignore_rules'])
        self.assertFalse(create['accept_hooks'])
        self.assertEqual(create['client_env']['HERMES_EPHEMERAL_SYSTEM_PROMPT'],'Dedicated guidance')
        submitted=next(p for m,p in self.client.calls if m=='prompt.submit')
        self.assertEqual(submitted['text'],'synthetic prompt')
        self.assertIn(('reasoning','核对项目目标'),self.updates)
        self.assertNotIn(('reasoning','unrelated'),self.updates)
        self.assertTrue(self.saved['terminal'])

    def test_cancelled_turn_gets_fresh_deadline_and_submission(self):
        self.client.receipts = [{'status': 'terminal', 'outcome': 'cancelled'}]
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_prompt()
        first = copy.deepcopy(self.saved)
        self.saved.update(deadline=0, cancel_deadline=0)
        self.client.receipts = [{'status': 'terminal', 'outcome': 'completed',
                                 'result': {'final_response': '{"text":"ready"}'}}]
        self.run_prompt('next question')
        self.assertEqual(self.client.created, 1)
        self.assertNotEqual(self.saved['submission_id'], first['submission_id'])
        self.assertGreater(self.saved['deadline'], 0)
        self.assertNotIn('cancel_deadline', self.saved)

    def test_reconnect_resumes_original_admission(self):
        self.client.fail_receipt=True
        with self.assertRaisesRegex(RuntimeError,'连接中断'):
            self.run_prompt()
        admission=self.saved['admission_id']
        self.client.fail_receipt=False
        self.run_prompt()
        self.assertEqual(sum(m=='prompt.submit' for m,_ in self.client.calls),1)
        self.assertEqual(sum(m=='session.create' for m,_ in self.client.calls),1)
        self.assertEqual(self.saved['admission_id'],admission)

    def test_unknown_submission_is_never_repeated(self):
        self.client.fail_submit=True
        with self.assertRaises(RuntimeError):self.run_prompt()
        self.client.fail_submit=False
        with self.assertRaisesRegex(RuntimeError,'回执未知'):self.run_prompt()
        self.assertEqual(sum(m=='prompt.submit' for m,_ in self.client.calls),1)

    def test_reused_session_unknown_submit_does_not_recover_previous_turn(self):
        self.run_prompt()
        first = self.saved['admission_id']
        self.client.fail_submit = True
        with self.assertRaisesRegex(RuntimeError, '连接中断'):
            self.run_prompt()
        self.assertNotIn('admission_id', self.saved)
        self.assertNotEqual(self.saved['submission_id'], first)
        self.client.fail_submit = False
        with self.assertRaisesRegex(RuntimeError, '回执未知'):
            self.run_prompt('changed input')
        self.assertEqual(self.client.created, 1)
        self.assertEqual(sum(m == 'prompt.submit' for m, _ in self.client.calls), 2)

    def test_tool_isolation_checked_before_submit(self):
        self.client.tools=[{'name':'terminal'}]
        with self.assertRaisesRegex(RuntimeError,'工具清单'):self.run_prompt()
        self.assertFalse(any(m=='prompt.submit' for m,_ in self.client.calls))

    def test_timeout_interrupts_exact_execution_and_waits_for_terminal(self):
        self.client.fail_receipt=True
        with self.assertRaises(RuntimeError):self.run_prompt()
        self.saved['deadline']=0
        self.client.fail_receipt=False
        self.client.receipts=[{'status':'started','execution_generation':7},
                              {'status':'terminal','outcome':'cancelled'}]
        with self.assertRaisesRegex(RuntimeError,'cancelled'):self.run_prompt()
        interrupt=next(p for m,p in self.client.calls if m=='session.interrupt')
        self.assertEqual(interrupt,{'session_id':'hud-session','execution_generation':7})
        self.assertTrue(self.saved['terminal'])


if __name__=='__main__':unittest.main()
