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
from engine import hermes_connections as connections  # noqa: E402
from engine.hermes_gateway_client import GatewayRefusal, GatewayTransportError  # noqa: E402
from engine import process_query  # noqa: E402

PROJECT = 'p'


class SlowFailure:
    """模拟耗时失败：时钟推进 delay 秒后抛 exc（验证退避从失败完成时起算）。"""

    def __init__(self, delay, exc):
        self.delay, self.exc = delay, exc


class FakeHermesClient:
    """按原生 session_id 返回可控 running/pending 状态的假网关租约。

    生产代码经 lease.rpc 查询并在 finally 释放租约（lease.close），
    夹具按同一契约记录调用与释放次数；不镜像底层 client.rpc 直连路径——
    gate 序列化/订阅登记/pool.failed 契约由 NativeObserverPoolContractTests
    （真实 Pool/Lease）覆盖，假夹具保留 client 属性只会掩盖直连回归。
    """

    def __init__(self, states, calls, closes, clock=None):
        self.states = states  # {session_id: dict | Exception | SlowFailure}
        self.calls = calls    # 记录 (method, params, timeout)
        self.closes = closes  # 记录 close() 调用
        self.clock = clock    # [t] 可推进的假时钟（None=真实时间）
        self.closed = False

    def rpc(self, method, *, timeout=None, **params):
        self.calls.append((method, params, timeout))
        if method != 'session.resume':
            raise AssertionError(f'unexpected rpc {method}')
        state = self.states.get(params['session_id'])
        if isinstance(state, SlowFailure):
            if self.clock is not None:
                self.clock[0] += state.delay
            raise state.exc
        if isinstance(state, Exception):
            raise state
        return {'running': state.get('running', False),
                'pending': state.get('pending', False)}

    def close(self):
        self.closed = True
        self.closes.append(True)


class FakeOwnedProc:
    """_native_procs 占位：poll() 返回 None 表示进程存活（内存已知占用路径）。"""

    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


class NativeBusyFixtures:
    """共享夹具（DB/队列/进程确定性），供假网关用例与真实 Pool/Lease 契约用例复用。"""

    def _setup_db(self):
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

    def _setup_queue_and_process_fixtures(self):
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


class NativeBusySchedulingTests(NativeBusyFixtures, unittest.TestCase):
    def setUp(self):
        self._setup_db()
        # 状态夹具
        self.native_states = {}
        self.gateway_calls = []
        self.lease_closes = []
        self.clock = None
        self.delivered = []

        def fake_connection(profile, *, home=None, command=None, read_only=False,
                            on_event=None, history=False):
            return FakeHermesClient(self.native_states, self.gateway_calls,
                                    self.lease_closes, clock=self.clock), home, command or []

        patcher = patch.object(hermes_gateway, 'connection', side_effect=fake_connection)
        patcher.start()
        self.connection_patcher = patcher
        self.addCleanup(patcher.stop)
        self._setup_queue_and_process_fixtures()

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
            # 生产更新路径：spawn attach 确认原生忙后调用 _native_busy_feedback 回写
            # 观察缓存（deliver 在本测试为记录器，反馈函数与 spawn 调用的是同一实现）；
            # 不做任何手工清缓存/翻转状态
            hermes_gateway._native_busy_feedback(self.conn, 'native-a')
            # 退回后同一 TTL 窗口内：预检查命中 busy 缓存 → A 跳过，B 获得调度
            self.assertTrue(queue.process_one(self.conn))
        row_a = self.conn.execute('SELECT id,status,started_at FROM wake_jobs WHERE id=?',
                                  (job_a['id'],)).fetchone()
        self.assertEqual(row_a['status'], 'queued')     # 原逻辑退回
        self.assertEqual(row_a['id'], job_a['id'])      # 保留原 ID
        self.assertIsNone(row_a['started_at'])
        self.assertEqual(self.delivered, [('hermes-a', 'work', None), ('hermes-b', 'work', None)])
        # 原生仍空闲（缓存 busy 驱动跳过）：窗口内不再重投 A（20 轮 A 零重投、B 零重复）
        for _ in range(20):
            self.assertFalse(queue.process_one(self.conn))
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

    def test_observation_lease_released_on_success_and_failure(self):
        # manager ①：观察租约成功/异常路径均须 finally 释放（Pool.sweep 依赖 leases 空集）
        self._bind('hermes-a', session_id='native-a')
        session = wake.session_name('hermes-a', 'work', None, PROJECT)
        self.native_states['native-a'] = {'running': False, 'pending': False}
        self.assertIs(hermes_gateway.process_state(session, profile='hermes-a'), False)
        self.assertEqual(self.lease_closes, [True])
        hermes_gateway._native_cache.clear()
        self.native_states['native-a'] = GatewayTransportError('Hermes connection interrupted')
        self.assertIsNone(hermes_gateway.process_state(session, profile='hermes-a'))
        self.assertEqual(self.lease_closes, [True, True])

    def test_failure_backoff_starts_at_failure_completion(self):
        # manager ③：退避从失败完成时起算——5 秒超时失败完成于 t105，首档 3s 退避过期须 108
        self._bind('hermes-a', session_id='native-a')
        self.clock = [100.0]
        self.native_states['native-a'] = SlowFailure(5.0, GatewayTransportError('slow timeout'))
        session = wake.session_name('hermes-a', 'work', None, PROJECT)
        with patch.object(hermes_gateway.time, 'monotonic', side_effect=lambda: self.clock[0]):
            self.assertIsNone(hermes_gateway.process_state(session, profile='hermes-a'))
            state, expiry, fails, reason, first = hermes_gateway._native_cache[('native-a', 0)]
            self.assertEqual(state, 'error')
            self.assertEqual(reason, 'transport_error')
            self.assertEqual(first, 105.0)
            self.assertEqual(expiry, 108.0)  # 105 完成 + 3 退避，而非 103（从查询开始算）
            self.assertEqual(fails, 1)
            # 失败完成后的退避窗口内重复检查：不新增 RPC（查询/连接耗时与退避分开计）
            calls_before = len(self.gateway_calls)
            self.clock[0] = 106.0
            self.assertIsNone(hermes_gateway.process_state(session, profile='hermes-a'))
            self.assertEqual(len(self.gateway_calls), calls_before)
            # 退避过期后重新查询，失败计数递增（6s 档）
            self.clock[0] = 109.0
            self.assertIsNone(hermes_gateway.process_state(session, profile='hermes-a'))
            self.assertEqual(len(self.gateway_calls), calls_before + 1)
            _, expiry2, fails2, _, _ = hermes_gateway._native_cache[('native-a', 0)]
            self.assertEqual(expiry2, 120.0)  # 109 开始 + 5 完成于 114 + 6 退避
            self.assertEqual(fails2, 2)

    def test_unknown_snapshot_defers_session(self):
        # resume 返回缺 running/pending 键 → 未知 → 暂缓，不按空闲放行
        self._bind('hermes-a', session_id='native-a')

        class UnknownClient(FakeHermesClient):
            def rpc(self, method, *, timeout=None, **params):
                self.calls.append((method, params, timeout))
                return {'revision': 1}

        with patch.object(hermes_gateway, 'connection',
                          return_value=(UnknownClient(self.native_states, self.gateway_calls,
                                                      self.lease_closes, clock=self.clock), None, [])):
            self._job('hermes-a', 'work')
            self._job('hermes-b', 'work')
            self.assertTrue(queue.process_one(self.conn))
            self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
        self.assertEqual(self.lease_closes, [True])  # 未知分支同样释放租约

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


class _ObserverRemote:
    """真实传输契约夹具：timeout 是传输层显式参数（不进入 wire 载荷），params 才是 wire。"""

    def __init__(self, calls, fail):
        self.calls = calls
        self.fail = fail
        self.closed = False

    def rpc(self, method, *, timeout=30, **params):
        if self.closed:
            raise GatewayTransportError('fixture transport closed')
        self.calls.append({'method': method, 'params': params, 'timeout': timeout})
        if self.fail[0] is not None:
            raise self.fail[0]
        if method == 'session.resume':
            return {'session_id': params.get('session_id'), 'subscription_id': str(len(self.calls)),
                    'running': False, 'pending': False}
        return {'pong': True}

    def close(self):
        self.closed = True


class NativeObserverPoolContractTests(NativeBusyFixtures, unittest.TestCase):
    """观察者经真实 Pool/Lease 的集成契约（node1705）。

    不使用 FakeHermesClient（其 client 即自身，掩盖 Lease.rpc 的 gate 序列化、订阅登记
    与传输失败语义），改用真实 connections.Pool/Lease + 传输夹具
    （与 scripts/test_hermes_connections.py 的 PoolTests 同一模式）。
    """

    def setUp(self):
        self._setup_db()
        self.delivered = []
        self._setup_queue_and_process_fixtures()
        self.observer_calls = []
        self.observer_fail = [None]
        self.observer_opens = []
        self.observer_clock = [0.0]
        self.observer_home = Path(self.tmp.name) / 'observer-home'
        self.observer_pool = connections.Pool(
            lambda home, command, callback, **kw: self._open(home, command, callback),
            lambda: ('fixture', 'sha'), lambda home: False, lambda: self.observer_clock[0])
        patcher = patch.object(hermes_gateway, 'connection', side_effect=self._acquire)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.observer_pool.stop)
        self.addCleanup(self.observer_pool.join)

    def _open(self, home, command, callback):
        remote = _ObserverRemote(self.observer_calls, self.observer_fail)
        self.observer_opens.append(remote)
        self.observer_remote = remote
        return remote

    def _acquire(self, profile, **kwargs):
        lease = self.observer_pool.acquire(self.observer_home, [], business=False)
        return lease, self.observer_home, []

    def _observe(self):
        session = wake.session_name('hermes-a', 'work', None, PROJECT)
        self._bind('hermes-a', session_id='native-observer')
        return hermes_gateway._native_session_state(self.conn, 'hermes-a', session)

    def _entry(self):
        return self.observer_pool.entries[os.path.normcase(str(self.observer_home.resolve()))]

    def test_observer_lease_rpc_registers_subscription_passes_timeout_and_releases(self):
        self.assertIs(self._observe(), False)
        entry = self._entry()
        self.assertEqual(len(entry.leases), 0)  # finally 释放，Pool.sweep 可做空闲回收
        self.assertFalse(self.observer_remote.closed)  # 共享物理连接保持池契约
        call = self.observer_calls[-1]
        self.assertEqual(call['method'], 'session.resume')
        self.assertTrue(call['params']['omit_messages'])
        self.assertEqual(call['timeout'], hermes_gateway._NATIVE_QUERY_TIMEOUT)  # 传输层显式超时
        self.assertNotIn('timeout', call['params'])  # timeout 不进 wire 载荷
        self.assertTrue(any(sid == 'native-observer' for _, sid in entry.subscriptions))  # 订阅登记

    def test_large_history_precheck_keeps_shared_control_connection_usable(self):
        import json
        import threading
        from websockets.sync.server import serve
        from websockets.exceptions import ConnectionClosed
        from engine import hermes_gateway_client as transport

        history = 'x' * (9 * 1024 * 1024)
        requests, sockets = [], []

        def handler(socket):
            sockets.append(socket)
            try:
                for raw in socket:
                    call = json.loads(raw)
                    requests.append(call)
                    if call['method'] == 'session.resume':
                        result = dict(running=False, pending=[], message_count=1,
                                      messages=[] if call['params'].get('omit_messages') else [history])
                    else:
                        result = {'status': 'started'}
                    socket.send(json.dumps(dict(jsonrpc='2.0', id=call['id'], result=result)))
            except ConnectionClosed:
                pass

        transport.resume_connections()
        with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = {'api_origin': 'http://127.0.0.1:' + str(server.socket.getsockname()[1])}
            pool = connections.Pool(
                lambda home, command, callback, **kw: transport.GatewayClient(endpoint, on_event=callback),
                lambda: ('fixture', 'sha'), lambda home: False)
            try:
                with patch.object(transport, 'ticket', return_value='synthetic'), \
                     patch.object(hermes_gateway, 'connection', side_effect=lambda *a, **kw: (
                         pool.acquire(self.observer_home, [], business=False), self.observer_home, [])):
                    with pool.acquire(self.observer_home, [], business=True) as control:
                        self.assertEqual(control.rpc('prompt.receipt', session_id='other', timeout=2),
                                         {'status': 'started'})
                        self.assertIs(self._observe(), False)
                        self.assertEqual(control.rpc('prompt.receipt', session_id='other', timeout=2),
                                         {'status': 'started'})
                        self.assertFalse(control.client.closed)
                    self.assertEqual(len(sockets), 1)
                    resume = [c for c in requests if c['method'] == 'session.resume']
                    self.assertEqual(len(resume), 1)
                    self.assertTrue(resume[0]['params']['omit_messages'])
            finally:
                pool.stop()
                pool.join()
                server.shutdown()
                thread.join(2)

    def test_observer_transport_failure_releases_and_marks_pool(self):
        self.observer_fail[0] = GatewayTransportError('injected transport failure')
        self.assertIsNone(self._observe())  # 超时/断连只暂缓本会话
        entry = self._entry()
        self.assertEqual(len(entry.leases), 0)  # 异常路径同样 finally 释放
        self.assertEqual(entry.state, 'error')  # Lease.rpc 经 pool.failed 标记死传输
        self.assertTrue(self.observer_remote.closed)
        self.observer_clock[0] = 2.0  # 越过 retry_at 后重连
        lease = self.observer_pool.acquire(self.observer_home, [], business=False)
        lease.close()
        self.assertEqual(len(self.observer_opens), 2)  # 池重建连接
        self.assertEqual(entry.state, 'connected')

    def test_observer_refusal_keeps_shared_connection_and_pool_connected(self):
        self.observer_fail[0] = GatewayRefusal('session_not_found')
        self.assertIsNone(self._observe())  # 会话级拒答只暂缓本会话
        entry = self._entry()
        self.assertEqual(len(entry.leases), 0)  # finally 释放
        self.assertFalse(self.observer_remote.closed)  # 拒答不毁共享连接
        self.assertEqual(entry.state, 'connected')  # 池仍连接


if __name__ == '__main__':
    unittest.main(verbosity=2)
