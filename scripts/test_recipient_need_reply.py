"""Recipient-specific requirements across topic and work delivery paths."""
import unittest
from unittest.mock import patch
from scripts import test_need_reply_nullable as fixture
from engine import api, db, queue, scanner, wake
from engine.delivery import effective_need_reply


class RecipientNeedReplyTests(unittest.TestCase):
    setUp = fixture.NeedReplyNullableTests.setUp
    tearDown = fixture.NeedReplyNullableTests.tearDown

    def message(self, value, mentions='["default"]', tid=None, ts=None):
        cur = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
            "VALUES('bot-group',?,'user','user','notice',?,?,?)",
            (ts or db.now_iso(), mentions, tid, value))
        for profile in ('default', 'architect'):
            self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,?,'pending')", (cur.lastrowid, profile))
        self.conn.commit()
        return cur.lastrowid

    def test_work_batch_projects_each_message_for_reader(self):
        for value in (0, 1, None):
            self.message(value)
        for profile, expected in [('default', [0, 1, None]), ('architect', [None]*3)]:
            result = api.get_inbox(profile, 100, 'work', x_group_profile=profile, project='bot-group')
            self.assertEqual([m['need_reply'] for m in result['messages']], expected)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT need_reply FROM messages ORDER BY id')], [0, 1, None])

    def test_topic_full_context_projects_for_mcp_reader(self):
        fixture._mk_thread(self.conn)
        self.message(0, tid=100)
        self.assertEqual(api.get_thread(100, x_group_profile='default')['messages'][0]['need_reply'], 0)
        self.assertIsNone(api.get_thread(100, x_group_profile='architect')['messages'][0]['need_reply'])

    def test_queue_passes_trigger_msg_id_not_need_reply(self):
        """req#82 P1：queue 不再为触发消息计算 need_reply 传 deliver，改传 trigger_msg_id。

        回复要求由注入快照逐条按 effective_need_reply 承载（test_message_injection.py
        覆盖），deliver 只收到 trigger_msg_id 用于触发消息标注。
        """
        fixture._mk_thread(self.conn)
        for kind in ('work', 'chat'):
            for recipient in ('default', 'architect'):
                with self.subTest(kind=kind, recipient=recipient):
                    mid = self.message(1, tid=100 if kind == 'chat' else None)
                    self.conn.execute("UPDATE threads SET turn_profile=? WHERE id=100", (recipient,))
                    self.conn.commit()
                    wake.enqueue(self.conn, recipient, kind, 'thread-open' if kind == 'chat' else 'mention',
                                 trigger_msg_id=mid, thread_id=100 if kind == 'chat' else None, project='bot-group')
                    with patch.object(wake, 'session_busy', return_value=False), \
                            patch.object(wake, 'deliver', return_value={'ok': True}) as deliver:
                        queue.process_one(self.conn)
                    kwargs = deliver.call_args.kwargs
                    self.assertNotIn('need_reply', kwargs)
                    self.assertEqual(kwargs['trigger_msg_id'], mid)

    def test_scanner_only_targets_mentions_including_false(self):
        self.message(0)
        with patch.object(wake, 'enqueue', return_value=True) as enqueue:
            self.assertEqual(scanner.scan_inbox(self.conn), 1)
        self.assertEqual(enqueue.call_args.args[1], 'default')

    def test_recent_unmentioned_message_does_not_revive_old_mention(self):
        """R2 演进：陈旧 @ 消息不因新鲜未提及消息复活，整组陈旧转 failed 终态。

        旧语义（req#52）断言 scan_inbox==0（静默不唤醒）；R2 后该 @ 消息整组陈旧，
        转 failed 终态（不再静默丢），但接收者本人仍不被唤醒。
        """
        self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES('user','主人','user')")
        self.conn.commit()
        self.message(1, ts='2020-01-01T00:00:00')  # 旧 @ 消息（陈旧）
        self.message(1, mentions='[]')              # 新鲜未提及消息（不构成有效 pending）
        with patch.object(wake, 'enqueue', return_value=False) as enqueue:
            self.assertEqual(scanner.scan_inbox(self.conn), 0)
        # 旧 @ 消息整组陈旧 → 转 failed 终态
        row = self.conn.execute(
            "SELECT status FROM inbox WHERE profile='default' AND message_id=1").fetchone()
        self.assertEqual(row["status"], "failed")

    def test_work_seen_gate_uses_recipient_requirement(self):
        false_id = self.message(0)
        true_id = self.message(1)
        with patch.object(api, '_resolve_identity', return_value=('architect', 'bot')):
            result = api.post_inbox_seen({'message_ids': [false_id, true_id]})
            self.assertEqual(result['seen'], 2)
        with patch.object(api, '_resolve_identity', return_value=('default', 'bot')):
            result = api.post_inbox_seen({'message_ids': [false_id, true_id]})
            self.assertEqual(result['seen'], 1)

    def test_helper_handles_empty_multiple_and_legacy_null(self):
        for mentions in ('[]', '["other"]', 'null', 'invalid'):
            self.assertIsNone(effective_need_reply({'mentions': mentions, 'need_reply': 1}, 'default'))
        for value in (0, 1, None):
            self.assertEqual(effective_need_reply({'mentions': ['default', 'architect'], 'need_reply': value}, 'default'), value)


if __name__ == '__main__':
    unittest.main()
