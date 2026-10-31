"""req#137 task#264：dsh 成员配置读写与 persona 挂载测试。

覆盖（对应验收标准）：
- 7 字段回读一致（描述/人格/指令/.env/模型/提供商/推理强度）
- 仅写变更字段 + 未知条目/字段保留
- revision 冲突检测、备份、清空字段继承原生 profile
- 动态表达式（!!js）只读容忍、保存拒绝
- 凭据脱敏、profile_config 分派

运行：python -B -m unittest scripts.test_dsh_profile_config -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from engine import db, discovery, dsh_profile_config  # noqa: E402


PATCH = """\
# 用户 patch 层
- id: system-prompt
  config:
    personaPrefix: "你是测试人格"
- id: agent-default-model
  config:
    provider: deepseek-account
    model: deepseek-flash
    reasoningEffort: max
- id: unknown-plugin
  config:
    keep: "保留我"
"""


class DshProfileConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.droot = Path(self.tmp.name) / "dsh"
        (self.droot / "profiles" / "desktop").mkdir(parents=True)
        self.patch = self.droot / "profiles" / "desktop" / "cordis.patch.yml"
        self.patch.write_text(PATCH, encoding="utf-8")
        self.envfile = self.droot / ".env"
        self.envfile.write_text("DEEPSEEK_API_KEY=sk-test123456\n", encoding="utf-8")
        self.agentsfile = self.droot / "AGENTS.md"
        self.agentsfile.write_text("先思考再回答\n", encoding="utf-8")
        self.descfile = self.droot / "profiles" / "desktop" / ".group-engine-description"
        self.descfile.write_text("", encoding="utf-8")
        self.dentry = Path(self.tmp.name) / "dsh.cmd"
        self.dentry.write_text("@echo off\n")
        self._envs = {}
        for k in ("DSH_HOME", "DSH_BIN", "GROUP_DB"):
            self._envs[k] = os.environ.get(k)
        os.environ["DSH_HOME"] = str(self.droot)
        os.environ["DSH_BIN"] = str(self.dentry)
        os.environ["GROUP_DB"] = str(Path(self.tmp.name) / "group.db")
        conn = db.get_connection()
        db.init_schema(conn)
        reg = discovery.register_bot(conn, "dsh", "desktop", str(self.droot), "dsh-bot")
        self.bot_id = reg["bot_id"]
        conn.execute(
            "INSERT INTO members(profile,display_name,created_at,source,bot_id) "
            "VALUES(?,?,?, 'api',?)", ("bot-1", "dsh-bot", db.now_iso(), self.bot_id))
        conn.commit()
        conn.close()
        self.bot = {"id": self.bot_id, "runtime": "dsh",
                    "config_root": str(self.droot), "profile": "desktop"}

    def tearDown(self):
        for k, v in self._envs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _read(self):
        return dsh_profile_config.read(self.bot)

    def _save(self, overrides):
        current = self._read()
        values = {k: current[k] for k in ("revision", "description", "persona",
                                          "instructions", "model", "provider",
                                          "reasoning_effort", "env")}
        values.update(overrides)
        return dsh_profile_config.save(self.bot, values)

    def test_read_round_trip(self):
        view = self._read()
        self.assertEqual(view["persona"], "你是测试人格")
        self.assertEqual(view["model"], "deepseek-flash")
        self.assertEqual(view["provider"], "deepseek-account")
        self.assertEqual(view["reasoning_effort"], "max")
        self.assertIn("先思考再回答", view["instructions"])
        self.assertIn("DEEPSEEK_API_KEY=sk-test123456", view["env"])
        self.assertEqual(view["description"], "")
        self.assertEqual(view["runtime"], "dsh")

    def test_plugins_status(self):
        view = self._read()
        plugins = view["plugins"]
        self.assertEqual(plugins["persona"]["state"], "ok")
        self.assertEqual(plugins["persona"]["plugin"], "system-prompt")
        self.assertEqual(plugins["model"]["state"], "ok")
        self.assertEqual(plugins["instructions"]["state"], "missing")  # 条目未配置
        self.assertEqual(plugins["instructions"]["plugin"], "dsh-agent-instructions")
        self.assertEqual(plugins["description"]["state"], "ok")
        self.assertIsNone(plugins["description"]["plugin"])
        self.assertEqual(plugins["env"]["state"], "ok")

    def test_plugins_status_disabled(self):
        self.patch.write_text(
            "- id: system-prompt\n  disabled: true\n  config:\n    personaPrefix: x\n"
            "- id: agent-default-model\n  config:\n    provider: deepseek-account\n",
            encoding="utf-8")
        self.assertEqual(self._read()["plugins"]["persona"]["state"], "disabled")

    def test_plugins_status_load_failed(self):
        self.patch.write_text(
            "- id: agent-default-model\n"
            "  name: '@deepseek-ai/dsh-agent-default-model'\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
            encoding="utf-8")
        self.assertEqual(self._read()["plugins"]["model"]["state"], "load_failed")

    def test_save_only_changes_target_and_preserves_unknown(self):
        self._save({"model": "deepseek-reasoner", "persona": "新人格"})
        text = self.patch.read_text(encoding="utf-8")
        self.assertIn("deepseek-reasoner", text)
        self.assertIn("新人格", text)
        self.assertIn("unknown-plugin", text)
        self.assertIn("保留我", text)      # 未知字段保留
        self.assertIn("deepseek-account", text)  # 未改 provider 保留
        # 指令在 AGENTS.md 侧边文件，未随 patch 变更
        self.assertIn("先思考再回答", self.agentsfile.read_text(encoding="utf-8"))
        view = self._read()
        self.assertEqual(view["model"], "deepseek-reasoner")
        self.assertEqual(view["persona"], "新人格")
        self.assertEqual(view["provider"], "deepseek-account")

    def test_save_instructions_and_description_to_files(self):
        self._save({"instructions": "新指令", "description": "成员描述"})
        # 指令写 AGENTS.md、描述写侧边文件，均不入 cordis.patch.yml
        self.assertIn("新指令", self.agentsfile.read_text(encoding="utf-8"))
        self.assertIn("成员描述", self.descfile.read_text(encoding="utf-8"))
        self.assertNotIn("成员描述", self.patch.read_text(encoding="utf-8"))
        view = self._read()
        self.assertIn("新指令", view["instructions"])
        self.assertEqual(view["description"], "成员描述")

    def test_revision_conflict(self):
        current = self._read()
        # 修改文件后再用旧 revision 保存，应触发 409 并发冲突
        self.patch.write_text(self.patch.read_text(encoding="utf-8") + "- id: added\n",
                              encoding="utf-8")
        values = {k: current[k] for k in ("revision", "description", "persona",
                                          "instructions", "model", "provider",
                                          "reasoning_effort", "env")}
        values["model"] = "changed"
        with self.assertRaises(HTTPException) as cm:
            dsh_profile_config.save(self.bot, values)
        self.assertEqual(cm.exception.status_code, 409)

    def test_dynamic_expression_refused(self):
        self.patch.write_text(
            "- id: system-prompt\n  config:\n    personaPrefix: !!js 'expr()'\n",
            encoding="utf-8")
        self.assertEqual(self._read()["persona"], "[expression]")  # 只读容忍
        with self.assertRaises(HTTPException) as cm:
            self._save({"persona": "新人格"})
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("动态表达式", cm.exception.detail)

    def test_backup_created(self):
        self._save({"description": "带备份"})
        backups = self.droot / "profiles" / "desktop" / ".group-engine-backups"
        self.assertTrue(backups.is_dir())
        self.assertTrue(any(backups.iterdir()))

    def test_clear_field_inherits_native(self):
        self._save({"model": ""})
        self.assertEqual(self._read()["model"], "")
        self.assertNotIn("deepseek-flash", self.patch.read_text(encoding="utf-8"))

    def test_sanitize_redacts_each_credential_branch(self):
        # sk- 分支
        msg = dsh_profile_config._sanitize("auth failed sk-abcdef123456")
        self.assertNotIn("sk-abcdef123456", msg)
        self.assertIn("[REDACTED]", msg)
        # bearer 分支
        msg = dsh_profile_config._sanitize("token bearer eyJhbGciOi")
        self.assertNotIn("bearer eyJhbGciOi", msg)
        self.assertIn("[REDACTED]", msg)
        # api_key 分支
        msg = dsh_profile_config._sanitize("missing api_key=secret-value")
        self.assertNotIn("api_key=secret-value", msg)
        self.assertIn("[REDACTED]", msg)

    def test_amp_and_star_text_not_refused(self):
        # 合法普通文本含 & / *（如 A&B、*重点*）不得被 _AMBIGUOUS 误拒
        self._save({"persona": "你是 A&B 客服，回答用 *重点* 标注"})
        view = self._read()
        self.assertIn("A&B", view["persona"])
        self.assertIn("*重点*", view["persona"])

    def test_profile_config_dispatch(self):
        from engine import profile_config
        view = profile_config.read("bot-1")
        self.assertEqual(view["runtime"], "dsh")
        self.assertEqual(view["model"], "deepseek-flash")
        values = {k: view[k] for k in ("revision", "description", "persona",
                                       "instructions", "model", "provider",
                                       "reasoning_effort", "env")}
        values["persona"] = "分派人格"
        result = profile_config.save("bot-1", values)
        self.assertTrue(result["saved"])
        self.assertEqual(dsh_profile_config.read(self.bot)["persona"], "分派人格")


if __name__ == "__main__":
    unittest.main()
