"""req#142 成果 2（task#277）：/thinking 原生执行观察回归。

覆盖验收 5：原生忙且引擎无活动派发记录的 Hermes 绑定显示观察条目，正确绑定
成员/项目/work/话题；与引擎派发记录区分（observed=true、无任务/消息关联）；
原生空闲即撤下，/reasoning 在批次结束后按 process_ended_at 传递结束时刻
（保留期内），超期清除；引擎活动派发接手/切换中/绑定消失结束批次；
批次起止发布 thinking.changed。

隔离：临时 SQLite + _native_observation_state 打桩（不触达真实网关）。
运行：python -B -m unittest scripts.test_native_observation -v
"""
import unittest
from unittest.mock import patch

from engine import api, db, events, heartbeat, hermes_gateway, native_observation, reasoning, wake
from scripts import test_abnormal_exit as fixtures


class NativeObservationTests(unittest.TestCase):
    def setUp(self):
        fixtures.AbnormalExitTests.setUp(self)
        self.states = {}  # engine_session -> 'native_running' | 'idle' | None（打桩）
        patcher = patch.object(hermes_gateway, '_native_observation_state',
                               side_effect=lambda conn, profile, session: self.states.get(session))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(native_observation._batches.clear)

    def bind(self, profile, kind='work', thread_id=0, project='p', session_id=None,
             generation=0, switching=0):
        self.conn.execute(
            "INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,"
            "runtime,generation,switching,title,updated_at) VALUES(?,?,?,?,?,?,'hermes',?,?,?,?)",
            (project, 0, profile, kind, thread_id,
             session_id or f'nat-{profile}-{kind}-{thread_id}', generation, switching, '',
             db.now_iso()))
        self.conn.commit()

    def thinking(self, entries=None):
        with patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=entries or []), \
                patch.object(wake, '_running_wakes', {}):
            return api.get_thinking()

    def test_observation_entry_shows_for_native_running_without_engine_record(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        result = self.thinking()
        self.assertEqual(result['thinking'], [])
        self.assertEqual(len(result['runs']), 1)
        entry = result['runs'][0]
        self.assertEqual(entry['profile'], 'dev-1')
        self.assertEqual(entry['session'], 'dev-1-p-work')
        self.assertIsNone(entry['thread_id'])
        self.assertEqual(entry['project'], 'p')
        self.assertEqual(entry['kind'], 'work')
        self.assertEqual(entry['state'], 'native_running')
        self.assertTrue(entry['observed'])
        # 观察不关联任务/消息（无证据）
        self.assertNotIn('task_id', entry)
        self.assertNotIn('message_id', entry)
        self.assertNotIn('parent_id', entry)

    def test_chat_binding_keeps_thread_ownership(self):
        self.bind('dev-2', kind='chat', thread_id=42, session_id='nat-c')
        self.states['dev-2-p-chat-42'] = 'native_running'
        entry = self.thinking()['runs'][0]
        self.assertEqual(entry['thread_id'], 42)
        self.assertEqual(entry['kind'], 'chat')
        self.assertEqual(entry['session'], 'dev-2-p-chat-42')

    def test_epoch_wake_started_at_stable_across_refreshes(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        first = self.thinking()['runs'][0]
        second = self.thinking()['runs'][0]
        self.assertEqual(first['wake_started_at'], second['wake_started_at'])
        self.assertEqual(len(second), len(first))

    def test_engine_active_run_not_double_observed(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        # 引擎活动派发（wake_runs 未结束行）→ 不观察
        self.conn.execute("INSERT INTO wake_runs(session,started_at,pid) VALUES('dev-1-p-work',100,10)")
        self.conn.commit()
        self.assertEqual(self.thinking()['runs'], [])
        # 内存活跃运行同样不观察
        self.conn.execute('UPDATE wake_runs SET ended_at=101')
        self.conn.commit()
        with patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {'dev-1-p-work': {
                    'session': 'dev-1-p-work', 'wake_started_at': 100}}):
            result = api.get_thinking()
        # 引擎自身运行照常展示，但不追加观察条目（observed 标记）
        self.assertEqual(len(result['runs']), 1)
        self.assertTrue(all(r.get('observed') is not True for r in result['runs']))

    def test_terminal_engine_record_with_native_running_observed(self):
        # 自发续跑核心场景：引擎记录已终态，原生仍 running → 观察条目
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        self.conn.execute("INSERT INTO wake_runs(session,started_at,pid,ended_at) "
                          "VALUES('dev-1-p-work',100,10,110)")
        self.conn.commit()
        entry = self.thinking()['runs'][0]
        self.assertTrue(entry['observed'])
        self.assertEqual(entry['state'], 'native_running')

    def test_engine_dispatch_takes_over_ends_observation_batch(self):
        # 观察批次存续期间引擎开始派发同一会话 → 批次结束（引擎记录接手展示）
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        started = self.thinking()['runs'][0]['wake_started_at']
        self.conn.execute("INSERT INTO wake_runs(session,started_at,pid) VALUES('dev-1-p-work',200,20)")
        self.conn.commit()
        self.assertEqual(self.thinking()['runs'], [])
        self.assertIsNotNone(native_observation.ended_for('dev-1-p-work', started))

    def test_observation_cleared_when_native_idle_and_reasoning_reports_end(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        started = self.thinking()['runs'][0]['wake_started_at']
        self.states['dev-1-p-work'] = 'idle'
        self.assertEqual(self.thinking()['runs'], [])  # 空闲即撤下
        ended = native_observation.ended_for('dev-1-p-work', started)
        self.assertIsNotNone(ended)
        # /reasoning 传递 process_ended_at（无 wake_runs 行的观察批次折叠前端气泡）
        with patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(reasoning, 'read_reasoning',
                             return_value={'entries': [], 'latest_id': 0}):
            result = api.get_reasoning('dev-1', 'dev-1-p-work', started)
        self.assertEqual(result['process_ended_at'], ended)
        self.assertEqual(result['entries'], [])

    def test_ended_batch_expires_after_grace(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        started = self.thinking()['runs'][0]['wake_started_at']
        self.states['dev-1-p-work'] = 'idle'
        self.thinking()
        with patch.object(native_observation.time, 'time',
                          return_value=native_observation.time.time() + native_observation._END_TTL + 1):
            self.thinking()
        self.assertIsNone(native_observation.ended_for('dev-1-p-work', started))

    def test_reobserve_after_end_starts_new_batch_and_keeps_old_terminal(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        first = self.thinking()['runs'][0]['wake_started_at']
        self.states['dev-1-p-work'] = 'idle'
        self.thinking()
        self.states['dev-1-p-work'] = 'native_running'
        second = self.thinking()['runs'][0]['wake_started_at']
        self.assertGreater(second, first)
        # 旧批次终态保留（node1715）：新批次启动后旧 /reasoning 仍可折叠
        self.assertIsNotNone(native_observation.ended_for('dev-1-p-work', first))

    def test_previous_batch_terminal_survives_new_batch(self):
        """node1715 反例：t1000 busy→t1005 idle→t1006 busy——旧批 /reasoning 终态保留 1005。"""
        self.bind('dev-1', session_id='nat-a')
        now = [1000.0]
        with patch.object(native_observation.time, 'time', side_effect=lambda: now[0]), \
                patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {}), \
                patch.object(reasoning, 'read_reasoning',
                             side_effect=lambda *a, **kw: {'entries': [], 'latest_id': 0}):
            self.states['dev-1-p-work'] = 'native_running'
            first = api.get_thinking()['runs'][0]
            self.assertEqual(first['wake_started_at'], 1000.0)
            now[0] = 1005.0
            self.states['dev-1-p-work'] = 'idle'
            api.get_thinking()
            now[0] = 1006.0
            self.states['dev-1-p-work'] = 'native_running'
            second = api.get_thinking()['runs'][0]
            self.assertEqual(second['wake_started_at'], 1006.0)
            # 新批启动后，旧批 /reasoning 仍按自己的终态折叠（迟到查询不丢 1005）
            old = api.get_reasoning('dev-1', 'dev-1-p-work', 1000.0)
            self.assertEqual(old['process_ended_at'], 1005.0)
            new = api.get_reasoning('dev-1', 'dev-1-p-work', 1006.0)
            self.assertIsNone(new['process_ended_at'])  # 新批仍活跃
            # 第二批结束 → 两批终态并存，各自可折叠
            now[0] = 1010.0
            self.states['dev-1-p-work'] = 'idle'
            api.get_thinking()
            self.assertEqual(api.get_reasoning('dev-1', 'dev-1-p-work', 1000.0)['process_ended_at'], 1005.0)
            self.assertEqual(api.get_reasoning('dev-1', 'dev-1-p-work', 1006.0)['process_ended_at'], 1010.0)

    def test_two_ended_batches_expire_independently(self):
        """两批终态各自保留期：第一批到期清除，第二批仍在期；再各自到期。"""
        self.bind('dev-1', session_id='nat-a')
        now = [1000.0]
        with patch.object(native_observation.time, 'time', side_effect=lambda: now[0]), \
                patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {}):
            self.states['dev-1-p-work'] = 'native_running'
            api.get_thinking()
            now[0] = 1005.0
            self.states['dev-1-p-work'] = 'idle'
            api.get_thinking()
            now[0] = 1006.0
            self.states['dev-1-p-work'] = 'native_running'
            api.get_thinking()
            now[0] = 1010.0
            self.states['dev-1-p-work'] = 'idle'
            api.get_thinking()
            # 第一批到期（1005+120=1125 < 1130），第二批仍在期（1010+120=1130 ≥ 1130）
            now[0] = 1130.0
            api.get_thinking()
            self.assertIsNone(native_observation.ended_for('dev-1-p-work', 1000.0))
            self.assertEqual(native_observation.ended_for('dev-1-p-work', 1006.0), 1010.0)
            # 第二批到期（1140-1010 > 120）
            now[0] = 1140.0
            api.get_thinking()
            self.assertIsNone(native_observation.ended_for('dev-1-p-work', 1006.0))

    def test_failed_or_unknown_query_not_displayed(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = None  # 查询失败/未知：无证据不显示
        self.assertEqual(self.thinking()['runs'], [])
        self.assertIsNone(native_observation.ended_for('dev-1-p-work', 0))

    def test_switching_binding_ends_batch_and_not_observed(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        started = self.thinking()['runs'][0]['wake_started_at']
        self.conn.execute("UPDATE session_bindings SET switching=1 WHERE session_id='nat-a'")
        self.conn.commit()
        self.assertEqual(self.thinking()['runs'], [])
        self.assertIsNotNone(native_observation.ended_for('dev-1-p-work', started))

    def test_binding_removed_ends_batch(self):
        self.bind('dev-1', session_id='nat-a')
        self.states['dev-1-p-work'] = 'native_running'
        started = self.thinking()['runs'][0]['wake_started_at']
        self.conn.execute("DELETE FROM session_bindings WHERE session_id='nat-a'")
        self.conn.commit()
        self.thinking()
        self.assertIsNotNone(native_observation.ended_for('dev-1-p-work', started))

    def test_thinking_changed_published_on_transitions(self):
        self.bind('dev-1', session_id='nat-a')
        published = []
        with patch.object(events.bus, 'publish',
                          side_effect=lambda name, payload: published.append((name, payload))):
            self.states['dev-1-p-work'] = 'native_running'
            self.thinking()
            self.states['dev-1-p-work'] = 'idle'
            self.thinking()
        profiles = [p['profile'] for n, p in published if n == 'thinking.changed']
        self.assertIn('dev-1', profiles)

    def test_engine_dispatched_runs_unchanged(self):
        # 既有契约回归：引擎派发运行仍原样返回（观察不改变 runs 合并语义）
        run = dict(profile='dev-1', session='dev-1-p-work', project='p',
                   thread_id=None, wake_started_at=100, spawned_at='2026-11-03T10:00:00')
        with patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {run['session']: run}):
            result = api.get_thinking()
        self.assertEqual(result['thinking'], [])
        self.assertEqual(result['runs'], [run])


if __name__ == '__main__':
    unittest.main(verbosity=2)
