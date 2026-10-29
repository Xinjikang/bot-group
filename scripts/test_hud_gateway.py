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
        self.tools = []
        self.receipts = [{'status': 'terminal', 'outcome': 'completed',
                          'result': {'final_response': '{"text":"ready"}'}}]

    def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == 'runtime.describe':
            return {'capabilities': ['durable-admission-v1']}
        if method in ('session.create', 'session.resume'):
            return {'session_id': 'hud-session'}
        if method == 'tools.show':
            return {'sections': [{'tools': self.tools}]}
        if method == 'prompt.submit':
            if self.fail_submit:
                raise GatewayTransportError('lost acknowledgement')
            self.callback({'method': 'event', 'params': {'session_id': 'other-session',
                          'type': 'reasoning.delta', 'payload': {'text': 'unrelated'}}})
            self.callback({'method': 'event', 'params': {'session_id': 'hud-session',
                          'type': 'reasoning.delta', 'payload': {'text': '核对项目目标'}}})
            return {'admission_id': 'admission'}
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
        def save(value):
            self.saved = copy.deepcopy(value)
        def connect(profile, **kwargs):
            self.client.callback = kwargs['on_event']
            self.assertEqual(Path(kwargs['home']), self.root)
            return self.client, self.root, ['hermes']
        for target, replacement in (('_saved', lambda: copy.deepcopy(self.saved)), ('_save', save)):
            p=patch.object(bridge,target,replacement);p.start();self.addCleanup(p.stop)
        p=patch.object(bridge.gateway,'connection',side_effect=connect);p.start();self.addCleanup(p.stop)
        p=patch.object(bridge.time,'sleep');p.start();self.addCleanup(p.stop)

    def run_prompt(self):
        return bridge.respond(self.bot,'synthetic prompt',str(self.root),lambda *args:self.updates.append(args),system_prompt='Dedicated guidance')

    def test_public_session_and_stream(self):
        self.assertEqual(json.loads(self.run_prompt()),{'text':'ready'})
        create=next(p for m,p in self.client.calls if m=='session.create')
        self.assertEqual(create['source'],'acp')
        self.assertEqual(create['toolsets'],['no_mcp'])
        self.assertEqual(create['editor'],{'mcp_servers':[]})
        self.assertTrue(create['ignore_rules'])
        self.assertFalse(create['accept_hooks'])
        self.assertEqual(create['client_env']['HERMES_EPHEMERAL_SYSTEM_PROMPT'],'Dedicated guidance')
        submitted=next(p for m,p in self.client.calls if m=='prompt.submit')
        self.assertEqual(submitted['text'],'synthetic prompt')
        self.assertIn(('reasoning','核对项目目标'),self.updates)
        self.assertNotIn(('reasoning','unrelated'),self.updates)
        self.assertTrue(self.saved['terminal'])

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

    def test_tool_isolation_checked_before_submit(self):
        self.client.tools=[{'name':'terminal'}]
        with self.assertRaisesRegex(RuntimeError,'工具隔离'):self.run_prompt()
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
