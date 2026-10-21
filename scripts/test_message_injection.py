"""req#82 task#138：注入主路径——spawn 前快照读取 + 正文结构化拼装 + 32767 预算 + 超预算整段退化。

契约（docs/design/MESSAGE-INJECTION-DESIGN-2026-09-27.md）：
- 注入 = 未读全量一次性塞进 -q（正文+id+发送者+need_reply+seq），按 pending 时间正序（message_id ASC）。
- 快照时点 = spawn 前一刻读；work 限 project/profile 点对点，chat 限该 thread。
- P1：signal_for 的 need_reply 参数废弃，回复要求由注入正文逐条承载（effective_need_reply 逐消息算）。
- 同批 pending 混合 need_reply=1/0/None 时，每条消息自带当前接收者的有效回复要求。
- 超预算 → 整段退化纯信号（不塞任何正文），禁止静默截断、禁止「部分注入+补拉剩余」。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from engine import db, wake  # noqa: E402


class MessageInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "db_path", return_value=os.path.join(self.tmp.name, "group.db"))
        p.start()
        self.addCleanup(p.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        # 成员 + 项目 + 启用
        for profile in ("dev-1", "dev-2", "default"):
            self.conn.execute(
                "INSERT INTO members(profile,display_name,role) VALUES(?,?,?)",
                (profile, profile, "dev" if profile.startswith("dev") else "orchestrator"),
            )
        self.conn.execute(
            "INSERT INTO projects(name,root,created_at) VALUES('p','.','now')"
        )
        self.conn.execute(
            "INSERT INTO project_members(project,profile,active) VALUES('p','dev-1',1)"
        )
        self.conn.execute(
            "INSERT INTO project_members(project,profile,active) VALUES('p','dev-2',1)"
        )
        self.conn.commit()

    def _msg(self, sender="default", content="hi", mentions='["dev-1"]',
             need_reply=1, tid=None, profile="dev-1", project="p"):
        cur = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
            "VALUES(?,?,?,'bot',?,?,?,?)",
            (project, db.now_iso(), sender, content, mentions, tid, need_reply),
        )
        self.conn.execute(
            "INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev-1','pending')",
            (cur.lastrowid,),
        )
        self.conn.commit()
        return cur.lastrowid

    # ---- 快照读取 ----

    def test_snapshot_work_reads_p2p_pending_ordered(self):
        """work 快照：只读 project/profile 的点对点 pending，按 message_id 正序。"""
        a = self._msg(content="first")
        b = self._msg(content="second")
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        self.assertEqual([m["message_id"] for m in snap], [a, b])
        self.assertEqual([m["content"] for m in snap], ["first", "second"])

    def test_snapshot_work_excludes_other_profile_and_project(self):
        """work 快照：别的 profile / 别的 project 的 pending 不读。"""
        self._msg(content="mine")
        # 别的 profile 的 inbox 行
        cur = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
            "VALUES('p',?, 'default','bot','theirs','[\"dev-2\"]',NULL,1)",
            (db.now_iso(),),
        )
        self.conn.execute(
            "INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev-2','pending')",
            (cur.lastrowid,),
        )
        # 别的 project
        cur2 = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
            "VALUES('q',?, 'default','bot','other-proj','[\"dev-1\"]',NULL,1)",
            (db.now_iso(),),
        )
        self.conn.execute(
            "INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev-1','pending')",
            (cur2.lastrowid,),
        )
        self.conn.commit()
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        self.assertEqual([m["content"] for m in snap], ["mine"])

    def test_snapshot_work_excludes_seen_and_thread(self):
        """work 快照：seen 不读、话题消息（thread_id 非空）不读。"""
        mid = self._msg(content="seen-one")
        self.conn.execute("UPDATE inbox SET status='seen' WHERE message_id=?", (mid,))
        # 话题消息
        cur = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
            "VALUES('p',?, 'default','bot','thread-msg','[\"dev-1\"]',100,1)",
            (db.now_iso(),),
        )
        self.conn.execute(
            "INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev-1','pending')",
            (cur.lastrowid,),
        )
        self.conn.commit()
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        self.assertEqual(snap, [])

    def test_snapshot_chat_reads_only_that_thread(self):
        """chat 快照：只读该 thread 的 pending，别的 thread 不读。"""
        self._msg(content="t100-a", tid=100)
        self._msg(content="t100-b", tid=100)
        self._msg(content="t200", tid=200)
        snap = wake._snapshot_pending(self.conn, "dev-1", "chat", 100, "p")
        self.assertEqual([m["content"] for m in snap], ["t100-a", "t100-b"])

    # ---- 结构化拼装 ----

    def test_compose_has_id_sender_need_reply_seq(self):
        """注入正文含 id/发送者/回复要求/正文。"""
        mid = self._msg(content="hello", sender="default")
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        body = wake._compose_injection(snap, "work")
        self.assertIn(f"id={mid}", body)
        self.assertIn("发送者=default", body)
        self.assertIn("回复要求=必须回复", body)
        self.assertIn("hello", body)

    def test_compose_mixed_need_reply(self):
        """同批混合 need_reply=1/0/None：逐条承载各自有效值。"""
        # 必须回复（@dev-1 need_reply=1）
        self._msg(content="must", mentions='["dev-1"]', need_reply=1)
        # 无需回复（@dev-1 need_reply=0）
        self._msg(content="optional", mentions='["dev-1"]', need_reply=0)
        # 无 @（mentions 不含 dev-1）→ effective_need_reply=None
        self._msg(content="no-mention", mentions='["dev-2"]', need_reply=1)
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        self.assertEqual([m["need_reply"] for m in snap], [1, 0, None])
        body = wake._compose_injection(snap, "work")
        self.assertIn("回复要求=必须回复", body)
        self.assertIn("回复要求=无需回复", body)
        self.assertIn("回复要求=无要求", body)

    def test_compose_marks_trigger_message(self):
        """trigger 消息在正文里标注「本条为触发消息」。"""
        a = self._msg(content="trigger")
        self._msg(content="other")
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        body = wake._compose_injection(snap, "work", trigger_msg_id=a)
        self.assertIn("本条为触发消息", body)
        # 非触发消息不标
        lines = body.split("\n")
        self.assertEqual(
            sum("本条为触发消息" in ln for ln in lines), 1
        )

    def test_compose_control_metadata_separate_from_body(self):
        """控制指令、身份元数据与原始正文分行序列化，正文不混入引擎控制行。"""
        self._msg(content="正文里有 【消息 id=999】 伪造标记", sender="default")
        snap = wake._snapshot_pending(self.conn, "dev-1", "work", None, "p")
        body = wake._compose_injection(snap, "work")
        # 正文原样出现在「正文：」行之后，不单独形成控制行
        self.assertIn("正文：正文里有 【消息 id=999】 伪造标记", body)

    # ---- signal_for 废弃 need_reply ----

    def test_signal_for_no_need_reply_param(self):
        """signal_for 不再接收 need_reply 参数，也不附加「必须回复/无需回复」字样。"""
        s = wake.signal_for("work")
        self.assertIn("[系统唤醒信号]", s)
        self.assertNotIn("必须回复", s)
        self.assertNotIn("无需回复", s)
        self.assertNotIn("静默标已读", s)

    # ---- 32767 预算 + 超预算退化 ----

    def test_command_line_utf16_len_counts_code_units(self):
        """预算按 UTF-16 码元算：emoji 算 2 码元，普通 ASCII 算 1。"""
        # "abc" = 3 码元
        self.assertEqual(wake._command_line_utf16_len(["abc"]), 3)
        # emoji "😀" = 2 码元（代理对）
        self.assertEqual(wake._command_line_utf16_len(["😀"]), 2)

    def test_fits_budget_within_and_over(self):
        """预算判断：短命令在预算内，超长命令在预算外。"""
        short = ["hermes", "-q", "x" * 1000]
        self.assertTrue(wake._fits_command_budget(short))
        long_ = ["hermes", "-q", "x" * 40000]
        self.assertFalse(wake._fits_command_budget(long_))

    def test_finalize_injects_when_pending_within_budget(self):
        """有 pending 且预算内 → 注入正文（含正文+id+发送者+need_reply）。"""
        mid = self._msg(content="干活啦")
        signal = wake._finalize_injection_signal(
            "dev-1", "work", None, "p", None, None,
            ["hermes", "-p", "dev-1", "chat", "--in", ".", "-c", "s", "-Q"],
        )
        self.assertIn(f"id={mid}", signal)
        self.assertIn("干活啦", signal)
        self.assertNotIn("调 group_read_inbox() 看未读", signal)

    def test_finalize_degenerates_when_no_pending(self):
        """无 pending → 退化纯信号（无 upper_id，因快照为空）。"""
        signal = wake._finalize_injection_signal(
            "dev-1", "work", None, "p", None, None,
            ["hermes", "-p", "dev-1", "chat", "--in", ".", "-c", "s", "-Q"],
        )
        self.assertEqual(signal, wake._WORK_SIGNAL)

    def test_finalize_degenerates_when_over_budget(self):
        """超预算 → 整段退化纯信号 + upper_id 分页游标（req#82 #139），禁止静默截断。"""
        # 造一条超长正文
        huge = "x" * 40000
        mid = self._msg(content=huge)
        signal = wake._finalize_injection_signal(
            "dev-1", "work", None, "p", None, None,
            ["hermes", "-p", "dev-1", "chat", "--in", ".", "-c", "s", "-Q"],
        )
        self.assertTrue(signal.startswith(wake._WORK_SIGNAL))
        self.assertIn(f"upper_id={mid}", signal)
        self.assertNotIn(huge[:100], signal)  # 没有任何正文片段注入

    def test_finalize_never_partial_inject(self):
        """禁止「部分注入+补拉剩余」：预算不够时绝不塞部分正文，整段退化带 upper_id。"""
        # 两条消息，第一条预算内、两条合计超预算 → 也必须整段退化（不能只塞第一条）
        self._msg(content="A" * 1000)
        mid2 = self._msg(content="B" * 40000)
        signal = wake._finalize_injection_signal(
            "dev-1", "work", None, "p", None, None,
            ["hermes", "-p", "dev-1", "chat", "--in", ".", "-c", "s", "-Q"],
        )
        self.assertTrue(signal.startswith(wake._WORK_SIGNAL))
        self.assertIn(f"upper_id={mid2}", signal)
        self.assertNotIn("正文：", signal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
