"""req#88 task#152：wake._spawn_in_workspace 按 Bot runtime 分叉。

契约（docs/engineering/ENGINEERING.md：宿主与会话；docs/engineering/REQ100-INLINE-INSTRUCTIONS-IMPLEMENTATION-2026-10-08.md）：
- 分叉点唯一 = _spawn_in_workspace 拼命令段。hermes 走现有 chat -Q -q；codex 走
   codex exec --json --skip-git-repo-check（首建无 resume，
  续 resume <thread_id>）。
- codex MCP 内联 -c mcp_servers.task_queue=...；工具全 approve（第六节：
  codex 与 hermes 同权，chat 内禁任务工具靠
  HERMES_CHAT_MODE 物理注册层，同 hermes 通道）。
- XDG_GROUP_ENGINE_EXECUTION 身份注入两条通道统一（MCP 侧零改动，第七节）。
- wake_runs.runtime_session_id：hermes 行存 session_id；codex 行首建 spawn 前存
  NULL（#148 契约），续行存 thread_id。
- HERMES_EPHEMERAL_SYSTEM_PROMPT 只对 hermes 通道注入（Hermes gateway 机制，
  codex 无此机制）。

mock subprocess.Popen，显式 mock session_adapter.available()=False，隔离库。
"""
import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import tomlkit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, wake  # noqa: E402


def _make_project(conn, project, root):
    conn.execute("INSERT INTO projects(name, root, worktree_root, created_at) VALUES(?,?,'',?)",
                 (project, root, db.now_iso()))
    conn.commit()


class RuntimeForkTests(unittest.TestCase):
    """_bot_runtime 定位链 + spawn 分叉命令 + XDG 注入 + wake_runs 回填语义。"""

    def setUp(self):
        gateway = patch('engine.hermes_gateway.enabled', return_value=False)
        gateway.start()
        self.addCleanup(gateway.stop)
        self.tmp = tempfile.mkdtemp(prefix="t152_")
        environment = patch.dict(os.environ, {'GROUP_DATA_DIR': self.tmp,
            'HERMES_MANAGED_DIR': os.path.join(self.tmp, 'managed'),
            'HERMES_HOME': os.path.join(self.tmp, 'hermes'),
            'CODEX_HOME': os.path.join(self.tmp, 'codex')})
        environment.start()
        self.addCleanup(environment.stop)
        auth = patch('engine.codex_auth.sync', return_value=False)
        auth.start()
        self.addCleanup(auth.stop)
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self._adapter = patch('engine.session_adapter.available', return_value=False)
        self._adapter.start()
        self.addCleanup(self._adapter.stop)
        self.addCleanup(wake._native_procs.clear)
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        _make_project(self.conn, "bot-group", _ROOT)
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ("bot-group",), ("default",))
        self.conn.commit()
        # spawn 状态复位
        wake._running_sessions.clear()
        wake._intentional_stops.clear()
        self.addCleanup(wake._running_sessions.clear)
        self.addCleanup(wake._intentional_stops.clear)

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _register_bot(self, host_type="codex", profile="default", runtime=None):
        """直接写 bots 表注册 codex Bot（#150 register_bot 的库层等价操作）。"""
        cur = self.conn.execute(
            "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
            "VALUES(?,?,?,?,?,?)",
            (host_type, profile, "C:/codex-root", runtime or host_type, profile, ""),
        )
        bot_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO project_members(project, profile, bot_id, active) "
            "VALUES(?,?,?,1)",
            ("bot-group", profile, bot_id),
        )
        self.conn.commit()
        return bot_id

    def _spawn_capture(self, profile="default", kind="work", signal="sig",
                       thread_id=None, project="bot-group"):
        """跑 _spawn 并捕获 Popen 的 cmd/env，返回 (result, cmd, env)。"""
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            proc = MagicMock()
            proc.pid = 4321
            proc.returncode = 0
            proc.wait.return_value = 0
            return proc

        fake_events = MagicMock()
        fake_hb = MagicMock()
        import types as _t
        fe = _t.ModuleType("engine.events"); fe.bus = MagicMock()
        fh = _t.ModuleType("engine.heartbeat"); fh.on_exit = MagicMock()

        with patch.object(wake.subprocess, "Popen", side_effect=_fake_popen), \
             patch.object(wake, "session_busy", return_value=False), \
             patch.object(wake, "_hermes_bin", return_value="hermes"), \
             patch.object(wake, "_codex_bin", return_value="codex"), \
             patch.dict(sys.modules, {"engine.events": fe, "engine.heartbeat": fh}), \
             patch("threading.Thread"):
            result = wake._spawn(profile, kind, signal, thread_id, project)
        return result, captured

    # ---------- _bot_runtime 定位链 ----------

    def test_runtime_pm_bot_id_wins(self):
        """第 1 层：pm.bot_id → bots.runtime（Bot 身份层正路）。"""
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        self.assertEqual(wake._bot_runtime(self.conn, "bot-group", "default"), "codex")

    def test_runtime_fallback_unique_profile_row(self):
        """第 2 层：pm 无 bot_id 行时按 profile 反推唯一 bots 行。"""
        self.conn.execute(
            "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
            "VALUES('codex','default','C:/codex-root','codex','d','')",
        )
        self.conn.execute(
            "INSERT INTO project_members(project, profile, bot_id, active) "
            "VALUES('bot-group','default',NULL,1)",
        )
        self.conn.commit()
        self.assertEqual(wake._bot_runtime(self.conn, "bot-group", "default"), "codex")

    def test_runtime_fallback_ambiguous_is_rejected(self):
        """同名 profile 跨宿主歧义必须明确拒绝。"""
        for host in ("hermes", "codex"):
            self.conn.execute(
                "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
                "VALUES(?,'default',? ,?,'d','')", (host, f"C:/{host}-root", host),
            )
        self.conn.commit()
        with self.assertRaises(ValueError):
            wake._bot_runtime(self.conn, 'bot-group', 'default')

    def test_runtime_no_bot_defaults_hermes(self):
        """第 3 层：无 bots 行默认 hermes（存量行为不变）。"""
        self.assertEqual(wake._bot_runtime(self.conn, "bot-group", "default"), "hermes")

    # ---------- spawn 分叉命令 ----------

    def test_hermes_spawn_command_unchanged(self):
        """hermes Bot：现有命令形态不变（-p chat --in -c -Q + -q signal）。"""
        result, cap = self._spawn_capture()
        self.assertTrue(result["ok"], result)
        cmd = cap["cmd"]
        self.assertEqual(cmd[0], "hermes")
        self.assertIn("chat", cmd)
        self.assertIn("-Q", cmd)
        # signal 仍走 -q 包装
        self.assertEqual(cmd[-2:], ["-q", "sig"])
        # 无 codex 参数
        self.assertNotIn("exec", cmd)
        self.assertNotIn("--ignore-user-config", cmd)

    def test_codex_first_spawn_no_resume(self):
        """codex Bot 首建：codex exec 基础参数，无 resume，prompt 是末位裸参数。"""
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        result, cap = self._spawn_capture()
        self.assertTrue(result["ok"], result)
        cmd = cap["cmd"]
        self.assertEqual(cmd[:4], ["codex", "exec", "--json",
                                   "--skip-git-repo-check"])
        self.assertNotIn("resume", cmd)
        # prompt 末位裸参数（无 -q）
        self.assertEqual(cmd[-1], 'sig')
        self._assert_developer_context(cmd, 'work')
        self.assertNotIn("-q", cmd)
        for key in ('approval_policy=', 'approvals_reviewer=', 'default_permissions='):
            self.assertFalse(any(arg.startswith(key) for arg in cmd))
        self.assertNotIn('Route approval requests through auto_review first.', cmd[-1])

    def test_codex_resume_uses_bound_thread_id(self):
        """codex Bot 续：绑定行存在 → resume <thread_id>，prompt 在 resume 后。"""
        bot_id = self._register_bot(host_type="codex", profile="default", runtime="codex")
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, "
            "session_id, runtime, generation, switching, title, updated_at) "
            "VALUES('bot-group', ?, 'default', 'work', 0, 'th-abc-123', 'codex', 0, 0, '', ?)",
            (bot_id, db.now_iso()),
        )
        self.conn.commit()
        result, cap = self._spawn_capture()
        self.assertTrue(result["ok"], result)
        cmd = cap["cmd"]
        self.assertIn("resume", cmd)
        i = cmd.index("resume")
        self.assertEqual(cmd[i + 1], "th-abc-123")
        for key in ('approval_policy=', 'approvals_reviewer=', 'default_permissions='):
            self.assertFalse(any(arg.startswith(key) for arg in cmd))
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)
        # resume thread_id 后才是 prompt
        self.assertEqual(cmd[-1], 'sig')
        self._assert_developer_context(cmd, 'work')

    def _assert_developer_context(self, cmd, kind):
        values = [arg for arg in cmd if arg.startswith('developer_instructions=')]
        self.assertEqual(len(values), 1)
        index = cmd.index(values[0])
        self.assertEqual(cmd[index - 1], '-c')
        if 'resume' in cmd:
            self.assertLess(index, cmd.index('resume'))
        self.assertEqual(json.loads(values[0].split('=', 1)[1]),
                         wake.wake_system_prompt(kind, 'bot-group', _ROOT))

    def test_codex_chat_context_and_quoted_signal(self):
        self._register_bot()
        self.conn.execute(
            "INSERT INTO threads(id, project, title, status, turn_profile, creator, members, created_at, last_active_at) "
            "VALUES(123, 'bot-group', 't', 'open', 'default', 'default', '[\"default\"]', ?, ?)",
            (db.now_iso(), db.now_iso()))
        self.conn.commit()
        signal = 'event "quoted"\nnext line \\ path'
        result, cap = self._spawn_capture(kind='chat', thread_id=123, signal=signal)
        self.assertTrue(result['ok'], result)
        self.assertEqual(cap['cmd'][-1], signal)
        self._assert_developer_context(cap['cmd'], 'chat')

    def test_codex_mcp_inline_config(self):
        """codex 命令含内联 MCP 配置与准确身份；工具 approve。"""
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        result, cap = self._spawn_capture()
        self.assertTrue(result["ok"], result)
        joined = " ".join(cap["cmd"])
        self.assertIn("mcp_servers.task_queue=", joined)
        # toml 内联含身份 env 与工具授权
        self.assertIn("TASK_QUEUE_ROLE", joined)
        self.assertIn("approval_mode", joined)
        # mcp/server.py 绝对路径在 args 里（json 转义斜杠，宽松匹配）
        self.assertIn("mcp", joined)
        self.assertIn("server.py", joined)

    def test_codex_mcp_allowlist_tracks_registered_tools(self):
        with open(os.path.join(_ROOT, 'mcp', 'server.py'), encoding='utf-8') as source:
            module = ast.parse(source.read())
        registered = set()
        for node in module.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if ((isinstance(func, ast.Attribute) and func.attr == 'tool')
                        or (isinstance(func, ast.Name) and func.id == '_cond_tool')):
                    registered.add(node.name)
        self.assertIn('req_update', registered)
        self.assertIn('operation_get', registered)
        for chat in (False, True):
            with self.subTest(chat=chat):
                config = tomlkit.parse(wake._codex_mcp_config('default', 'bot-group', chat=chat)[1])
                server = config['mcp_servers']['task_queue']
                self.assertEqual(set(server['enabled_tools']), registered)
                self.assertEqual(set(server['tools']), registered)
                self.assertEqual(server['env']['HERMES_CHAT_MODE'], '1' if chat else '0')

    def test_consecutive_codex_wakes_keep_rules_with_distinct_signals(self):
        bot_id = self._register_bot()
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, "
            "session_id, runtime, generation, switching, title, updated_at) "
            "VALUES('bot-group', ?, 'default', 'work', 0, 'stable-thread', 'codex', 0, 0, '', ?)",
            (bot_id, db.now_iso()))
        self.conn.commit()
        captures = []
        for signal in ('wake-one', 'wake-two'):
            result, cap = self._spawn_capture(signal=signal)
            self.assertTrue(result['ok'], result)
            self._assert_developer_context(cap['cmd'], 'work')
            self.assertEqual(cap['cmd'][-1], signal)
            captures.append(cap)
        values = [[arg for arg in cap['cmd'] if arg.startswith('developer_instructions=')]
                  for cap in captures]
        self.assertEqual(values[0], values[1])

    def test_hermes_overlay_includes_operation_get_in_both_modes(self):
        import importlib
        mcp_runtime = importlib.import_module('engine.mcp_runtime')
        self.conn.execute(
            "INSERT INTO threads(id, project, title, status, turn_profile, creator, members, created_at, last_active_at) "
            "VALUES(123, 'bot-group', 't', 'open', 'default', 'default', '[\"default\"]', ?, ?)",
            (db.now_iso(), db.now_iso()))
        self.conn.commit()
        for kind in ('work', 'chat'):
            with self.subTest(kind=kind), patch.object(mcp_runtime, 'hermes_overlay',
                                                      wraps=mcp_runtime.hermes_overlay) as overlay:
                result, _ = self._spawn_capture(kind=kind, thread_id=123 if kind == 'chat' else None)
                self.assertTrue(result['ok'], result)
                server = overlay.call_args.args[0]
                self.assertIn('operation_get', server['tools']['include'])
                self.assertEqual(server['tools']['include'], list(wake._MCP_TOOLS))

    # ---------- env 注入 ----------

    def test_xdg_injection_both_runtimes(self):
        """第七节：XDG_GROUP_ENGINE_EXECUTION 身份注入两通道统一，字段含
        profile/project/thread_id/session/wake_started_at（MCP 侧零改动）。"""
        # hermes 通道
        _, cap_h = self._spawn_capture(profile="default")
        xdg_h = cap_h["env"].get("XDG_GROUP_ENGINE_EXECUTION")
        self.assertTrue(xdg_h, "hermes 通道缺 XDG 注入")
        data_h = json.loads(xdg_h)
        self.assertEqual(data_h["profile"], "default")
        self.assertEqual(data_h["project"], "bot-group")
        self.assertEqual(data_h["session"], "default-bot-group-work")
        self.assertIn("wake_started_at", data_h)
        # codex 通道
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        _, cap_c = self._spawn_capture(profile="default")
        xdg_c = cap_c["env"].get("XDG_GROUP_ENGINE_EXECUTION")
        self.assertTrue(xdg_c, "codex 通道缺 XDG 注入")
        data_c = json.loads(xdg_c)
        # 字段结构与 hermes 通道一致（MCP _execution_context 消费方零改动）
        self.assertEqual(set(data_c.keys()), set(data_h.keys()))
        self.assertEqual(data_c["profile"], "default")

    def test_hermes_ephemeral_prompt_not_set_for_codex(self):
        """HERMES_EPHEMERAL_SYSTEM_PROMPT 只对 hermes 通道注入（gateway 机制）。"""
        _, cap_h = self._spawn_capture()
        self.assertIn("HERMES_EPHEMERAL_SYSTEM_PROMPT", cap_h["env"])
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        _, cap_c = self._spawn_capture()
        self.assertNotIn("HERMES_EPHEMERAL_SYSTEM_PROMPT", cap_c["env"])

    def test_codex_chat_sets_hermes_chat_mode(self):
        """第六节：codex chat 话题唤醒设 HERMES_CHAT_MODE=1（物理注册层把关
        任务工具禁用，同 hermes 通道）。"""
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        # 造一个 open 话题 turn 让 chat spawn 通过 turn 校验
        self.conn.execute(
            "INSERT INTO threads(id, project, title, status, turn_profile, creator, members, created_at, last_active_at) "
            "VALUES(7, 'bot-group', 't', 'open', 'default', 'default', '[\"default\"]', ?, ?)", (db.now_iso(), db.now_iso()),
        )
        self.conn.commit()
        with patch.object(wake, "topic_busy", return_value=False):
            _, cap = self._spawn_capture(kind="chat", thread_id=7)
        self.assertEqual(cap["env"].get("HERMES_CHAT_MODE"), "1")

    # ---------- wake_runs 回填语义 ----------

    def test_wake_runs_runtime_session_id_semantics(self):
        """#148 契约：hermes 行 runtime_session_id=会话名（降级路径）；codex 首建
        spawn 前存 NULL、续行存 thread_id。"""
        # hermes：available()=False 降级路径 sid=会话名
        self._spawn_capture()
        row = self.conn.execute(
            "SELECT runtime_session_id FROM wake_runs WHERE session='default-bot-group-work'"
        ).fetchone()
        self.assertEqual(row["runtime_session_id"], "default-bot-group-work")
        # codex 首建：NULL
        codex_bot = self._register_bot(host_type="codex", profile="default", runtime="codex")
        self._spawn_capture()
        row = self.conn.execute(
            "SELECT runtime_session_id FROM wake_runs WHERE session='default-bot-group-work' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        self.assertIsNone(row["runtime_session_id"])
        # codex 续：thread_id
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, "
            "session_id, runtime, generation, switching, title, updated_at) "
            "VALUES('bot-group', ?, 'default', 'work', 0, 'th-xyz', 'codex', 0, 0, '', ?)",
            (codex_bot, db.now_iso()),
        )
        self.conn.commit()
        self._spawn_capture()
        row = self.conn.execute(
            "SELECT runtime_session_id FROM wake_runs WHERE session='default-bot-group-work' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["runtime_session_id"], "th-xyz")

    def test_codex_spawn_skips_hermes_session_binding(self):
        """codex 跳过 Hermes 会话绑定（第四节第 5 条：无会话创建动作）。"""
        self._register_bot(host_type="codex", profile="default", runtime="codex")
        with patch.object(wake, "_codex_bin", return_value="codex"), \
             patch("engine.session_adapter.available", side_effect=AssertionError('Codex must skip Hermes probing')), \
             patch("engine.session_adapter.resolve_or_create") as mock_resolve:
            mock_resolve.side_effect = AssertionError(
                "codex spawn 不得触发 Hermes 会话绑定 resolve_or_create")
            result, _ = self._spawn_capture()
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
