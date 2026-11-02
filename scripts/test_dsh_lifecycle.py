"""req#137 task#267：dsh 命令与生命周期（后端）单元回归。

覆盖：
1. /stop 进程定位：_find_codex_processes 扩展覆盖 runtime='dsh'（_native_procs 活进程 +
   重启后命令行身份匹配 _matches_dsh_execution 靠 overlay 目录标签 _dsh_spawn_tag）。
2. /steer 能力拦截：dsh 目标在进入 Hermes 执行查询/账本投递前返回能力不支持，
   命令留痕（commands 表）不落 steer 账本；hermes 目标仍走原投递。
3. /stop 全流程：进程树 kill + inbox 追回 + job 取消 + 任务释放 + dsh 跳过软删。
4. /help 菜单与后端能力一致（dsh/Codex 不支持运行中纠偏标注）。

mock 隔离库（不进原生），系统 python 可跑。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, wake, commands, stop, session_adapter  # noqa: E402


def _seed(conn, project="bot-group", bot_profile="dsh-bot", runtime="dsh"):
    conn.execute("INSERT INTO members(profile,display_name,role) VALUES('user','user','user')")
    conn.execute("INSERT INTO members(profile,display_name,role) VALUES(?,?,?)",
                 (bot_profile, bot_profile, bot_profile))
    conn.execute("INSERT INTO projects(name,root,worktree_root,created_at) VALUES(?,?, '', ?)",
                 (project, _ROOT, db.now_iso()))
    conn.execute("INSERT INTO project_members(project,profile,active) VALUES(?,?,1)", (project, "user"))
    cur = conn.execute(
        "INSERT INTO bots(host_type,profile,config_root,runtime,runtime_entry,display_name,role) "
        "VALUES('dsh', ?, 'C:/dsh-home', ?, 'C:/dsh/dsh.cmd', ?, '')",
        (bot_profile, runtime, bot_profile))
    bot_id = cur.lastrowid
    conn.execute("INSERT INTO project_members(project,profile,bot_id,active) VALUES(?,?,?,1)",
                 (project, bot_profile, bot_id))
    return bot_id


class DshStopLocateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t267_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        _seed(self.conn)
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_spawn_tag_and_matcher(self):
        tag = wake._dsh_spawn_tag("dsh-bot-bot-group-work", 1764.25)
        argv = ["C:/dsh/dsh.cmd", "--profile", "headless", "--patch",
                f"C:/tmp/{tag}-abc/overlay.json", "--json", "-"]
        run = {"session": "dsh-bot-bot-group-work", "started_at": 1764.25}
        self.assertTrue(wake._matches_dsh_execution(argv, run))
        # 会话/启动时间不符 → 拒绝信任该 pid
        run2 = {"session": "other-session", "started_at": 1764.25}
        self.assertFalse(wake._matches_dsh_execution(argv, run2))
        # 非 headless 或无 --patch → 拒绝
        self.assertFalse(wake._matches_dsh_execution(["dsh.cmd", "--help"], run))
        self.assertFalse(wake._matches_dsh_execution(
            ["dsh.cmd", "--profile", "headless", "--json"], run))

    def test_live_dsh_proc_found_via_native_procs(self):
        self.conn.execute(
            "INSERT INTO wake_runs(session,started_at,pid,project,profile,runtime) "
            "VALUES('dsh-bot-bot-group-work', 1764.25, 4242, 'bot-group', 'dsh-bot', 'dsh')")
        self.conn.commit()
        class FakeProc:
            pid = 4242
            def poll(self):
                return None
        with patch.dict(wake._native_procs,
                        {"dsh-bot-bot-group-work": (FakeProc(), 1764.25)}):
            pids = wake._find_codex_processes("dsh-bot", "dsh-bot-bot-group-work")
        self.assertEqual(pids, [4242])

    def test_restart_fallback_matches_command_line(self):
        self.conn.execute(
            "INSERT INTO wake_runs(session,started_at,pid,project,profile,runtime) "
            "VALUES('dsh-bot-bot-group-work', 1764.25, 4242, 'bot-group', 'dsh-bot', 'dsh')")
        self.conn.commit()
        tag = wake._dsh_spawn_tag("dsh-bot-bot-group-work", 1764.25)
        argv = ["D:/deepseek/DeepSeek Harness.exe", "--expose-internals", "cli.js",
                "--profile", "headless", "--patch", f"C:/tmp/{tag}-xyz/overlay.json",
                "--json", "-"]
        with patch.object(wake.process_query, "command_line", return_value=argv):
            pids = wake._find_codex_processes("dsh-bot", "dsh-bot-bot-group-work")
        self.assertEqual(pids, [4242])
        # 命令行为无关进程（无标签）→ 不信任 pid
        with patch.object(wake.process_query, "command_line",
                          return_value=["dsh.cmd", "--profile", "headless",
                                        "--patch", "C:/tmp/other/overlay.json", "--json"]):
            self.assertEqual(wake._find_codex_processes("dsh-bot", "dsh-bot-bot-group-work"), [])


class DshSteerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t267_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        _seed(self.conn)
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_steer_dsh_target_returns_unsupported_without_ledger(self):
        with patch("engine.hermes_gateway_steer.deliver_steer") as deliver:
            result = commands.dispatch(self.conn, "bot-group", "user", "user",
                                       "/steer @dsh-bot 请改用方案B", None)
        self.assertFalse(result["ok"])
        self.assertIn("不支持", result["detail"])
        self.assertEqual(result["statuses"], ["unsupported"])
        deliver.assert_not_called()
        # 命令留痕：commands 表有记录，steer_operations 无账本
        row = self.conn.execute(
            "SELECT status FROM commands WHERE command='/steer'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM steer_operations").fetchone()[0], 0)

    def test_help_marks_dsh_steer_boundary(self):
        text = commands.help_text()
        self.assertIn("/steer", text)
        self.assertIn("dsh/Codex 子进程后端不支持", text)


class DshStopFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t267_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        _seed(self.conn)
        self.conn.execute(
            "INSERT INTO wake_jobs(project,profile,kind,thread_id,status,trigger,trigger_msg_id,enqueued_at) "
            "VALUES('bot-group','dsh-bot','work',NULL,'running','@msg',77,?)", (db.now_iso(),))
        self.conn.execute(
            "INSERT INTO inbox(message_id,profile,status) VALUES(77,'dsh-bot','pending')")
        self.conn.execute(
            "INSERT INTO requirements(id,project,title,status,created_at,updated_at) "
            "VALUES(1,'bot-group','r267','open',?,?)", (db.now_iso(), db.now_iso()))
        self.conn.execute(
            "INSERT INTO tasks(project,requirement_id,title,status,claimed_by,created_at) "
            "VALUES('bot-group',1,'t267','in_progress','dsh-bot',?)", (db.now_iso(),))
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_stop_dsh_kills_tree_and_cleans_state(self):
        class FakeProc:
            pid = 4242
            def poll(self):
                return 1  # 已退出
        with patch.dict(wake._native_procs,
                        {"dsh-bot-bot-group-work": (FakeProc(), 1764.25)}), \
             patch.object(wake, "find_bot_processes", return_value=[4242]), \
             patch.object(stop, "_kill_process_tree", return_value=True) as kill, \
             patch.object(stop, "_wait_dead", return_value=True), \
             patch("engine.hermes_gateway.cancel", return_value=None):
            result = stop.stop_profile("dsh-bot", "bot-group")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["killed"], 1)
        kill.assert_called_once_with(4242)
        # inbox 追回 + job 取消 + 任务释放
        self.assertEqual(self.conn.execute(
            "SELECT status FROM inbox WHERE message_id=77").fetchone()[0], "seen")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM wake_jobs WHERE profile='dsh-bot'").fetchone()[0], "cancelled")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tasks WHERE title='t267'").fetchone()[0], "pending")
        # dsh 无 hermes 原生 state.db 软删——跳过（同会话保留，绑定不动）
        self.assertEqual(result.get("soft_deleted", 0), 0)
        binding = self.conn.execute(
            "SELECT COUNT(*) FROM session_bindings WHERE profile='dsh-bot'").fetchone()[0]
        self.assertEqual(binding, 0)

    def test_stop_keeps_pending_when_not_dead(self):
        with patch.object(wake, "find_bot_processes", return_value=[4242]), \
             patch.object(stop, "_kill_process_tree", return_value=True), \
             patch.object(stop, "_wait_dead", return_value=False), \
             patch("engine.hermes_gateway.cancel", return_value=None):
            result = stop.stop_profile("dsh-bot", "bot-group")
        self.assertFalse(result["stopped"])
        # 未死透：保留队列与任务占用（下轮叫停再清）
        self.assertEqual(result["soft_deleted"], -1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM wake_jobs WHERE profile='dsh-bot'").fetchone()[0], "running")


if __name__ == "__main__":
    unittest.main()
