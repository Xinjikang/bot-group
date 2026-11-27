"""User steering uses one history message and preserves each recipient's delivery path."""
import io
import json
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from PIL import Image
from engine import api, attachments, commands, db, hermes_gateway, hermes_gateway_steer, router, wake
from scripts.test_hermes_gateway_steer import _running_row
from scripts.unified_test_support import isolated_database


class SteerMessageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_database())
        self.conn = self.enterContext(closing(db.get_connection()))
        self.remote = Mock()
        self.remote.rpc.return_value = {'status': 'queued'}
        self.enterContext(patch.object(hermes_gateway, 'connection', return_value=(self.remote, None, [])))

    def send(self, text, tid=None):
        return commands.dispatch(self.conn, 'alpha', 'user', 'user', '/steer ' + text, tid)

    def running(self, tid=None):
        _running_row(self.conn, profile='default', kind='chat' if tid else 'work', thread_id=tid)
        self.conn.execute("UPDATE hermes_executions SET project='alpha'")
        self.conn.commit()

    def observe(self, wire):
        self.remote.rpc.return_value = {'session_id': 'gw-exec0', 'messages': [
            {'role': 'user', 'timestamp': 12346, 'content': '[OUT-OF-BAND USER MESSAGE]\n' + wire}]}
        return hermes_gateway_steer.observe_session(self.conn, self.remote, 'gw-exec0', 12345.)

    def receipts(self, mid):
        return dict(self.conn.execute('SELECT profile,status FROM inbox WHERE message_id=?', (mid,)))

    def test_work_steer_is_read_only_after_current_context_insertion(self):
        self.running()
        sent = self.send('@default first line\n  second line')
        mid = sent['message_id']
        wire = self.remote.rpc.call_args.kwargs['text']
        self.assertIn('first line\n  second line', wire)
        self.assertEqual(self.remote.rpc.call_args.args, ('session.steer',))
        self.assertEqual(self.remote.rpc.call_args.kwargs['session_id'], 'gw-exec0')
        self.assertEqual(self.remote.rpc.call_args.kwargs['execution_generation'], 1)
        self.assertEqual(self.receipts(mid), {'default': 'steering'})
        self.assertFalse(wake._snapshot_pending(self.conn, 'default', 'work', None, 'alpha'))
        self.assertEqual(self.observe(wire), 1)
        self.assertEqual(self.observe(wire), 0)
        message = api.get_messages(project='alpha', limit=10)['messages'][-1]
        self.assertEqual((message['id'], message['sender_type'], message['sender']), (mid, 'user', 'user'))
        self.assertEqual(message['steer_receipts'], {'default': 'seen'})
        self.assertTrue(self.conn.execute('SELECT seen_at FROM inbox WHERE message_id=?', (mid,)).fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wake_jobs').fetchone()[0], 0)

    def test_topic_one_message_for_all_targets_other_members_receive_normal_unread(self):
        self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,"
                          "turn_profile,round) VALUES(10,'alpha','topic','user',?,'open',?,?,'default',3)",
                          (json.dumps(['user','default','architect','dev']), db.now_iso(), db.now_iso()))
        self.conn.commit()
        self.running(10)
        first = self.send('@default @architect review', 10)
        mid = first['message_id']
        wire = self.remote.rpc.call_args.kwargs['text']
        self.assertEqual(self.observe(wire), 1)
        self.assertEqual(self.receipts(mid), {'default': 'seen', 'architect': 'failed', 'dev': 'pending'})
        self.assertEqual([r['message_id'] for r in wake._snapshot_pending(self.conn, 'dev', 'chat', 10, 'alpha')], [mid])
        self.assertEqual([m['id'] for m in api.get_thread(10, profile='dev', x_group_profile=None)['messages']], [mid])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM topic_dispatch WHERE thread_id=10').fetchone()[0], 1)
        self.assertIsNone(router._dfs_subtree(self.conn, 10, mid, 3, 0, set(), mid))
        self.assertEqual(tuple(self.conn.execute('SELECT turn_profile,round FROM threads WHERE id=10').fetchone()), ('default',3))
        with self.assertRaises(commands.CommandError):
            self.send('@reviewer outside', 10)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM messages WHERE thread_id=10').fetchone()[0], 1)

    def test_lost_ack_requires_evidence_for_this_message(self):
        self.running()
        self.remote.rpc.side_effect = hermes_gateway.GatewayTransportError('timeout')
        first = self.send('@default identical')
        first_wire = self.remote.rpc.call_args.kwargs['text']
        self.remote.rpc.side_effect = None
        second = self.send('@default identical')
        second_wire = self.remote.rpc.call_args.kwargs['text']
        self.assertEqual(self.observe(first_wire), 1)
        self.assertEqual(self.receipts(first['message_id']), {'default': 'seen'})
        self.assertEqual(self.receipts(second['message_id']), {'default': 'steering'})
        self.assertEqual(self.observe(first_wire + '\n' + second_wire), 1)
        self.assertEqual(self.receipts(second['message_id']), {'default': 'seen'})

    def test_ordinary_and_steered_messages_share_routing_and_attachment_contract(self):
        self.running()
        text = '请让 @default 核对这张图\n  保留原文'
        image = io.BytesIO()
        Image.new('RGB', (1, 1)).save(image, format='PNG')
        items = [attachments.store(self.conn, image.getvalue(), 'check.png', 'alpha', 'user') for _ in range(2)]
        with patch.object(wake, 'enqueue', return_value=False):
            normal = router.route_message(self.conn, 'user', text, 'user', None, 'alpha',
                                          attachment_ids=[items[0]['id']], need_reply=1)
        sent = commands.dispatch(self.conn, 'alpha', 'user', 'user', '/steer ' + text, None,
                                 attachment_ids=[items[1]['id']], need_reply=1)
        self.assertIn(items[1]['id'], self.remote.rpc.call_args.kwargs['text'])
        self.assertIn(text, self.remote.rpc.call_args.kwargs['text'])
        history = api.get_messages(project='alpha', since_id=normal['message_id'] - 1, limit=10)['messages']
        self.assertEqual([m['id'] for m in history], [normal['message_id'], sent['message_id']])
        for field in ('sender', 'sender_type', 'content', 'project', 'mentions', 'thread_id', 'need_reply'):
            self.assertEqual(history[0][field], history[1][field], field)
        self.assertEqual(history[1]['content'], text)
        for message, item in zip(history, items):
            self.assertEqual(message['attachments'], [item])
