"""Static prompt composition, work approval entry and chat message delivery.

Common rules precede mode-specific collaboration rules. Static strings may be
assembled from other static constants; project paths stay in wake-time context.
"""

import os
import ast
from pathlib import Path
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import wake  # noqa: E402


class PromptEntryPointTests(unittest.TestCase):
    def test_permission_denial_and_chat_handoff_are_static(self):
        for kind in ("work", "chat"):
            prompt = wake.system_prompt_for(kind)
            self.assertIn("PERMISSION_DENIED", prompt)
            self.assertIn("立即结束被拒动作", prompt)
            self.assertIn("禁止尝试绕过", prompt)
        chat = wake.system_prompt_for("chat")
        self.assertIn("可讨论方案、创建需求和拆解任务", chat)
        self.assertIn("task_create", chat)
        self.assertIn("task_claim/task_log/task_complete/task_block 在 chat 被拒", chat)
        tree = ast.parse(Path(wake.__file__).read_text(encoding="utf-8"))
        constants = {n.targets[0].id: n.value for n in tree.body
                     if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        def static_text(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return static_text(constants[node.id])
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return static_text(node.left) + static_text(node.right)
            self.fail('Prompt contains dynamic content: ' + ast.dump(node))

        for name in ("_WORK_SYSTEM_PROMPT", "_CHAT_SYSTEM_PROMPT"):
            self.assertEqual(static_text(constants[name]).encode("utf-8"), getattr(wake, name).encode("utf-8"))

    def _rule1(self, kind):
        prompt = wake.system_prompt_for(kind).split('## 协作规则', 1)[1]
        # 第 1 条 = "1. ..." 到 "\n2. " 之间
        start = prompt.index("1. ")
        end = prompt.index("\n2. ", start)
        return prompt[start:end]

    def test_work_rule1_three_entry_points(self):
        rule1 = self._rule1("work")
        self.assertIn("approved 续工", rule1)
        self.assertIn("task_id", rule1)
        self.assertIn("消息已注入", rule1)
        self.assertIn("消息需补读", rule1)
        self.assertIn("group_read_inbox", rule1)
        self.assertIn("读完为止", rule1)

    def test_chat_rule1_injected_or_unread_messages(self):
        rule1 = self._rule1("chat")
        self.assertIn("正文已注入则直接处理", rule1)
        self.assertIn("只有纯信号时调用", rule1)
        self.assertIn("group_read_inbox", rule1)
        self.assertIn("直至本轮可读消息处理完毕", rule1)

    def test_rule1_no_longer_says_must_call_inbox_first(self):
        """旧措辞「必须先调 group_read_inbox 读未读」已移除（不再无条件强制先拉 inbox）。"""
        for kind in ("work", "chat"):
            self.assertNotIn("必须先调 group_read_inbox 读未读", wake.system_prompt_for(kind))

    def test_approved_signal_is_standalone_entry(self):
        """approved 独立入口：带 task_id 读任务，不带通用补读指令。"""
        sig = wake._APPROVED_SIGNAL
        self.assertIn("{task_id}", sig)
        self.assertNotIn("group_read_inbox()", sig)
        self.assertIn("读取该任务", sig)

    def test_approved_formats_task_id(self):
        formatted = wake._APPROVED_SIGNAL.format(task_id=42)
        self.assertIn("42", formatted)
        self.assertNotIn("{task_id}", formatted)

    def test_work_signal_prefix_preserved(self):
        """[系统唤醒信号] 前缀保留。"""
        self.assertTrue(wake._WORK_SIGNAL.startswith("[系统唤醒信号]"))
        self.assertTrue(wake._CHAT_SIGNAL.startswith("[系统唤醒信号]"))

    def test_mark_seen_rule_preserved(self):
        """work 第 14 条 mark_seen 契约保留不动。"""
        self.assertIn("group_mark_seen", wake.system_prompt_for("work"))

    def test_work_rule6_scanner_recovery_guidance(self):
        """req#117 #200：scanner 兜底唤醒拆解 → 先读已有任务/设计依据/来源消息，补齐剩余任务。"""
        prompt = wake.system_prompt_for("work")
        self.assertIn("scanner 兜底唤醒拆解", prompt)
        self.assertIn("task_list/task_get", prompt)
        self.assertIn("设计依据与来源消息", prompt)
        self.assertIn("保留已落库成果", prompt)
        self.assertIn("无 task.create 授权成员", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
