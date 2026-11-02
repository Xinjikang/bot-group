"""req#142 成果 1（task#276）：Hermes 原生忙闲预检查与跨会话调度恢复回归。

覆盖验收：
1. A 原生 busy 且引擎表缺记录时 B 空闲会话获得投递；A 保留原 ID 等待，解除 busy 后恢复
2. 同 Bot 独立 work/chat 会话、不同 Bot 继续推进（FIFO 跳过 busy）
3. 投递返回 busy 按原逻辑退回（原 ID 回 queued），其他就绪会话继续得到调度
4. 原生查询超时/断连/未知只暂缓对应会话；失败退避缓存、绑定代次切换失效
6. Hermes 原生忙时后方 Codex、DSH 就绪任务获得投递；Codex/DSH 忙时跳过与空闲恢复

隔离：临时 SQLite；Hermes 网关查询与物理进程扫描均为确定性夹具，不触达真实网关/进程。

运行：python -B -m unittest scripts.test_native_busy_scheduling -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, queue, wake  # noqa: E402
from engine import hermes_gateway  # noqa: E402
from engine.hermes_gateway_client import GatewayTransportError  # noqa: E402
from engine import process_query  # noqa: E402

PROJECT = 'p'


class FakeHermesClient:
    """按原生 session_id 返回可控 running/pending 状态的假网关客户端。"""

    def __init__(self, states, calls):
        self.states = states  # {session_id: dict | Exception}
        self.calls = calls    # 记录 (method, params, timeout)

    def rpc(self, method, *, timeout=None, **params):
        self.calls.append((method, params, timeout))
        if method != 'session.resume':
            raise AssertionError(f'unexpected rpc {method}')
        state = self.states.get(params['session_id'])
        if isinstance(state, Exception):
            raise state
        return {'running': state.get('running', False),
                'pending': state.get('pending', False)}


class FakeOwnedProc:
    """_native_procs 占位：poll() 返回 None 表示进程存活（内存已知占用路径）。"""

    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


class NativeBusySchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(db, 'db_path', return_value=os.path.join(self.tmp.name, 'group.db'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.conn.execute("INSERT INTO projects(name,root,created_at,readiness_status) VALUES(?,?,?,'ready')",
                          (PROJECT, '.', db.now_iso()))
        for profile in ('hermes-a', 'hermes-b', 'codex-c', 'dsh-d'):
            self.conn.execute('INSERT INTO members(profile,display_name,role) VALUES(?,?,?)',
                              (profile, profile, 'dev'))
            self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES(?,?,1)",
                             (PROJECT, profile))
        # codex/dsh 的 runtime 由 members.bot_id → bots.runtime 派生
        for i, (profile, runtime) in enumerate((('codex-c', 'codex'), ('dsh-d', 'dsh'))):
            bot_id = self.conn.execute(
                "INSERT INTO bots(host_type,config_root,profile,display_name,runtime,runtime_entry,role) "
                "VALUES(?,?,?,?,?,'','')", (f'probe-{i}', f'/probe/{i}', profile, profile, runtime)).lastrowid
            self.conn.execute('UPDATE members SET bot_id=? WHERE profile=?', (bot_id, profile))
        self.conn.commit()
        # 状态夹具
        self.native_states = {}
        self.gateway_calls = []
        self.delivered = []

        def fake_connection(profile, *, home=None, command=None, read_only=False,
                            on_event=None, history=False):
            return FakeHermesClient(self.native_states, self.gateway_calls), home, command or []

        patcher = patch.object(hermes_gateway, 'connection', side_effect=fake_connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 物理进程扫描确定性：默认无匹配进程（空闲）；按用例单独覆盖
        patcher = patch.object(process_query, 'list_processes', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(process_query, 'command_line', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(wake, 'deliver', side_effect=self._fake_deliver)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 状态缓存与内存占用注册表隔离清理
        self.addCleanup(hermes_gateway._native_cache.clear)
        self.addCleanup(wake._native_procs.clear)

    def _fake_deliver(self, profile, kind, **kwargs):
        self.delivered.append((profile, kind, kwargs.get('thread_id')))
        return {'ok': True, 'detail': 'probe'}

    # ---- 夹具 ----

    def _bind(self, profile, kind='work', thread_id=0, session_id=None, generation=0, switching=0):
        """注册 Hermes 托管绑定行（project 固定为 p）。"""
        self.conn.execute(
            "INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,"
            "runtime,generation,switching,title,updated_at) VALUES(?,?,?,?,?,?,'hermes',?,?,?,?)",
            (PROJECT, 0, profile, kind, thread_id, session_id or f'native-{profile}',
             generation, switching, '', db.now_iso()))
        self.conn.commit()

    def _job(self, profile, kind='work', thread_id=None, trigger='probe'):
        self.assertTrue(wake.enqueue(self.conn, profile, kind, trigger,
                                     thread_id=thread_id, project=PROJECT),
                        f'enqueue failed for {profile}/{kind}')
        return self.conn.execute('SELECT * FROM wake_jobs WHERE profile=? AND kind=?',
                                 (profile, kind)).fetchone()

    def _clear_native_cache(self):
        hermes_gateway._native_cache.clear()

    # ---- 验收 1：原生 busy 无引擎记录 → 后方空闲会话投递，A 保留原 ID 恢复 ----

    def test_native_busy_without_engine_record_does_not_starve_ready_session(self):
        self._bind('hermes-a', session_id='native-a')
        self.native_states['native-a'] = {'running': True, 'pending': False}  # 引擎表无 hermes_executions 行
        job_a = self._job('hermes-a', 'work')
        self._job('hermes-b', 'work')
        for _ in range(20):
            queue.process_one(self.conn)
        # B 空闲会话获得投递；A 未被投递且保留原 ID 等待
        self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
        row_a = self.conn.execute('SELECT id,status FROM wake_jobs WHERE id=?', (job_a['id'],)).fetchone()
        self.assertEqual(row_a['status'], 'queued')
        self.assertEqual(row_a['id'], job_a['id'])
        # 解除原生 busy（模拟 TTL 过期后重查）→ A 恢复投递，ID 不变
        self.native_states['native-a'] = {'running': False, 'pending': False}
        self._clear_native_cache()
        self.assertTrue(queue.process_one(self.conn))
        row_a = self.conn.execute('SELECT id,status FROM wake_jobs WHERE id=?', (job_a['id'],)).fetchone()
        self.assertEqual(row_a['status'], 'done')
        self.assertEqual(self.delivered[-1], ('hermes-a', 'work', None))

    def test_terminal_engine_record_with_new_native_execution_still_busy(self):
        # 引擎记录 terminal 已清空（96 行路径）但原生有新执行：须判 busy 而不是空闲
        self._bind('hermes-a', session_id='native-a')
        session = wake.session_name('hermes-a', 'work', None, PROJECT)
        self.conn.execute(
            "INSERT INTO hermes_executions(execution_id,session,started_at,project,profile,kind,"
            "source,python,home,workdir,session_id,profile_id,submission_id,state,"
            "submit_deadline,reconcile_deadline,cleanup_pending) "
            "VALUES('exec-1',?,?,?,?,?,'gateway','py','/','/','native-a','pid-1','sub-1','terminal',1,1,0)",
            (session, 100.0, PROJECT, 'hermes-a', 'work'))
        self.conn.commit()
        self.native_states['native-a'] = {'running': True, 'pending': False}
        job_a = self._job('hermes-a', 'work')
        self._job('hermes-b', 'work')
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?',
                                           (job_a['id'],)).fetchone()['status'], 'queued')

    # ---- 验收 2：同 Bot 独立 work/chat 会话与不同 Bot 继续推进（FIFO 跳过 busy） ----

    def test_independent_work_chat_sessions_advance_while_work_busy(self):
        self._bind('hermes-a', kind='work', session_id='native-a-work')
        self.native_states['native-a-work'] = {'running': True, 'pending': False}
        # hermes-a 的 chat 会话（另一话题）有独立绑定且空闲
        self._bind('hermes-a', kind='chat', thread_id=7, session_id='native-a-chat')
        self.native_states['native-a-chat'] = {'running': False, 'pending': False}
        self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,"
                          "last_active_at,turn_profile) VALUES(7,'p','t','user','[\"hermes-a\"]','open',"
                          "'now','now','hermes-a')")
        self.conn.commit()
        self._job('hermes-a', 'work')          # busy，id 最小
        self._job('hermes-a', 'chat', thread_id=7)  # 空闲，次之
        self._job('hermes-b', 'work')          # 空闲，最后
        self.assertTrue(queue.process_one(self.conn))
        self.assertTrue(queue.process_one(self.conn))
        self.assertFalse(queue.process_one(self.conn))  # 只剩 busy 的 a-work
        self.assertEqual(self.delivered[:2], [
            ('hermes-a', 'chat', 7),
            ('hermes-b', 'work', None),
        ])

    # ---- 验收 3：投递返回 busy 按原逻辑退回，其他就绪会话继续调度 ----

    def test_deliver_busy_returns_to_queued_and_ready_sessions_advance(self):
        # 预检查通过（原生空闲），spawn 阶段 attach 才发现 busy（预检查后变忙的竞态）
        self._bind('hermes-a', session_id='native-a')
        self.native_states['native-a'] = {'running': False, 'pending': False}
        job_a = self._job('hermes-a', 'work')
        self._job('hermes-b', 'work')
        calls = {'n': 0}

        def deliver_with_attach_busy(profile, kind, **kwargs):
            calls['n'] += 1
            self.delivered.append((profile, kind, kwargs.get('thread_id')))
            if calls['n'] == 1:
                return {'ok': False, 'busy': True, 'detail': 'native busy at attach'}
            return {'ok': True, 'detail': 'probe'}

        with patch.object(wake, 'deliver', side_effect=deliver_with_attach_busy):
            # 第一次投递：attach 才发现原生 running → busy 退回
            self.assertTrue(queue.process_one(self.conn))
            # attach 发现的原生忙状态：下一轮预检查即命中（模拟真实时序）
            self.native_states['native-a'] = {'running': True, 'pending': False}
            self._clear_native_cache()
            # a 退回后 busy，B 获得调度
            self.assertTrue(queue.process_one(self.conn))
        row_a = self.conn.execute('SELECT id,status,started_at FROM wake_jobs WHERE id=?',
                                  (job_a['id'],)).fetchone()
        self.assertEqual(row_a['status'], 'queued')     # 原逻辑退回
        self.assertEqual(row_a['id'], job_a['id'])      # 保留原 ID
        self.assertIsNone(row_a['started_at'])
        self.assertEqual(self.delivered, [('hermes-a', 'work', None), ('hermes-b', 'work', None)])

    # ---- 验收 4：查询失败/未知只暂缓对应会话；退避缓存；绑定代次失效 ----

    def test_query_failure_defers_only_target_session_then_recovers(self):
        self._bind('hermes-a', session_id='native-a')
        self.native_states['native-a'] = GatewayTransportError('Hermes connection interrupted')
        self._job('hermes-a', 'work')
        self._job('hermes-b', 'work')
        # 失败期间：B 投递，A 暂缓；退避缓存命中后不再重复查询
        self.assertTrue(queue.process_one(self.conn))
        self.assertFalse(queue.process_one(self.conn))
        self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
        resumed_calls = len(self.gateway_calls)
        self.assertFalse(queue.process_one(self.conn))  # 失败退避缓存命中，不新增查询
        self.assertEqual(len(self.gateway_calls), resumed_calls)
        # 查询恢复（模拟退避过期）→ A 恢复调度
        self.native_states['native-a'] = {'running': False, 'pending': False}
        self._clear_native_cache()
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered[-1][0], 'hermes-a')

    def test_unknown_snapshot_defers_session(self):
        # resume 返回缺 running/pending 键 → 未知 → 暂缓，不按空闲放行
        self._bind('hermes-a', session_id='native-a')

        class UnknownClient(FakeHermesClient):
            def rpc(self, method, *, timeout=None, **params):
                self.calls.append((method, params, timeout))
                return {'revision': 1}

        with patch.object(hermes_gateway, 'connection',
                          return_value=(UnknownClient(self.native_states, self.gateway_calls), None, [])):
            self._job('hermes-a', 'work')
            self._job('hermes-b', 'work')
            self.assertTrue(queue.process_one(self.conn))
            self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.delivered, [('hermes-b', 'work', None)])

    def test_binding_generation_switch_invalidates_idle_cache(self):
        # 空闲结果入缓存后绑定代次提升（/new 或绑定切换）：旧缓存不得掩盖新执行
        self._bind('hermes-a', session_id='native-a', generation=0)
        self.native_states['native-a'] = {'running': False, 'pending': False}
        self._job('hermes-a', 'work')
        self.assertTrue(queue.process_one(self.conn))  # a idle → a 投递，缓存 idle
        self.assertEqual(self.delivered, [('hermes-a', 'work', None)])
        # 绑定切换：generation 0 → 1，原生此时已有新执行
        self.conn.execute("UPDATE session_bindings SET generation=1 WHERE profile='hermes-a'")
        self.conn.commit()
        self.native_states['native-a'] = {'running': True, 'pending': False}
        self._job('hermes-a', 'work')  # 重新入队
        self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES('hermes-c','hermes-c','dev')")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','hermes-c',1)")
        self.conn.commit()
        self._job('hermes-c', 'work')
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered[-1][0], 'hermes-c')  # a 按新代次查到 busy 被跳过
        row_a = self.conn.execute("SELECT status FROM wake_jobs WHERE profile='hermes-a' AND status='queued'"
                                  ).fetchone()
        self.assertIsNotNone(row_a)

    # ---- 验收 6：Hermes 原生忙时 Codex/DSH 就绪任务获得投递；codex/dsh 忙时跳过与恢复 ----

    def test_mixed_runtime_ready_codex_dsh_advance_while_hermes_busy(self):
        self._bind('hermes-a', session_id='native-a')
        self.native_states['native-a'] = {'running': True, 'pending': False}
        self._job('hermes-a', 'work')   # 原生忙
        self._job('codex-c', 'work')    # codex 空闲（无运行中 wake_runs）
        self._job('dsh-d', 'work')      # dsh 空闲
        self.assertTrue(queue.process_one(self.conn))
        self.assertTrue(queue.process_one(self.conn))
        self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.delivered, [
            ('codex-c', 'work', None),
            ('dsh-d', 'work', None),
        ])

    def test_mixed_runtime_codex_dsh_busy_skip_and_resume(self):
        # codex busy：内存已知占用（_native_procs 注册 + wake_runs 行）
        session_c = wake.session_name('codex-c', 'work', None, PROJECT)
        started_c = 200.5
        self.conn.execute("INSERT INTO wake_runs(session,profile,thread_id,runtime,started_at,pid,project) "
                          "VALUES(?,?,?,?,?,?,?)", (session_c, 'codex-c', 0, 'codex', started_c, 777, PROJECT))
        wake._native_procs[session_c] = (FakeOwnedProc(777), started_c)
        # dsh busy：命令行匹配路径（--profile headless --patch <spawn tag>）
        session_d = wake.session_name('dsh-d', 'work', None, PROJECT)
        started_d = 300.5
        self.conn.execute("INSERT INTO wake_runs(session,profile,thread_id,runtime,started_at,pid,project) "
                          "VALUES(?,?,?,?,?,?,?)", (session_d, 'dsh-d', 0, 'dsh', started_d, 888, PROJECT))
        with patch.object(process_query, 'command_line', return_value=[
                'dsh', '--profile', 'headless', '--patch',
                wake._dsh_spawn_tag(session_d, started_d) + '/overlay']):
            self._job('codex-c', 'work')
            self._job('dsh-d', 'work')
            self._job('hermes-b', 'work')  # 空闲对照
            self.assertTrue(queue.process_one(self.conn))
            self.assertFalse(queue.process_one(self.conn))
            self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
        self.conn.commit()
        # 解除 busy：codex/dsh 恢复投递（FIFO 顺序）
        del wake._native_procs[session_c]
        self.conn.execute("UPDATE wake_runs SET ended_at=? WHERE session=?",
                          (db.now_iso(), session_c))
        self.conn.execute("UPDATE wake_runs SET ended_at=? WHERE session=?",
                          (db.now_iso(), session_d))
        self.conn.commit()
        with patch.object(process_query, 'command_line', return_value=None), \
             patch.object(process_query, 'list_processes', return_value=[]):
            self.assertTrue(queue.process_one(self.conn))
            self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered[1:], [
            ('codex-c', 'work', None),
            ('dsh-d', 'work', None),
        ])


if __name__ == '__main__':
    unittest.main(verbosity=2)
