"""task#80（req#45 信号显式标来源）：三个信号常量带 [系统唤醒信号] 前缀。

契约：_WORK_SIGNAL/_CHAT_SIGNAL/_APPROVED_SIGNAL 开头统一带 [系统唤醒信号]，
验证 Bot 可通过固定前缀识别系统唤醒信号的来源。
"""
import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# wake.py 依赖 engine 包，直接 import（纯常量模块，无副作用）
from engine import wake  # noqa: E402


class SignalPrefixTests(unittest.TestCase):
    PREFIX = "[系统唤醒信号]"

    def test_work_signal_has_prefix(self):
        self.assertTrue(wake._WORK_SIGNAL.startswith(self.PREFIX))

    def test_chat_signal_has_prefix(self):
        self.assertTrue(wake._CHAT_SIGNAL.startswith(self.PREFIX))

    def test_approved_signal_has_prefix(self):
        self.assertTrue(wake._APPROVED_SIGNAL.startswith(self.PREFIX))

    def test_work_signal_points_to_inbox(self):
        """去 kind 参数后仍指向 work 读取。"""
        self.assertIn("group_read_inbox()", wake._WORK_SIGNAL)
        self.assertNotIn("kind=", wake._WORK_SIGNAL)

    def test_chat_signal_points_to_inbox(self):
        """去 kind 参数后仍指向 chat 读取。"""
        self.assertIn("group_read_inbox()", wake._CHAT_SIGNAL)
        self.assertNotIn("kind=", wake._CHAT_SIGNAL)

    def test_approved_signal_no_kind(self):
        """approved 独立入口（req#82 P2）：不带 group_read_inbox 通用补读，带动态 task_id 读任务与批准结果。"""
        self.assertNotIn("group_read_inbox()", wake._APPROVED_SIGNAL)
        self.assertIn("{task_id}", wake._APPROVED_SIGNAL)
        self.assertIn("读取该任务", wake._APPROVED_SIGNAL)

    # ---- req#82 P1：need_reply 参数已废弃，signal_for 不再附加「必须回复/无需回复」字样 ----
    # 回复要求改由注入正文逐条承载（见 test_message_injection.py），信号本身回归纯静态。

    def test_signal_no_need_reply_suffix(self):
        """signal_for 不再接收 need_reply 参数，不附加任何回复要求字样。"""
        s = wake.signal_for("work")
        self.assertNotIn("必须回复", s)
        self.assertNotIn("无需回复", s)
        self.assertNotIn("静默标已读", s)

    def test_chat_signal_no_need_reply_suffix(self):
        """chat 信号同样不附加回复要求字样。"""
        s = wake.signal_for("chat")
        self.assertNotIn("必须回复", s)
        self.assertNotIn("无需回复", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
