"""Per-run Hermes overlays and shared Codex MCP connection configuration."""

import json
import os
from pathlib import Path
import tempfile
import subprocess
import tomllib
import unittest
from unittest.mock import Mock, patch

from engine import auth, config, mcp_runtime, wake


class RuntimeMcpTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = self.root / "managed"
        self.scope.mkdir()
        self.patch = patch.object(config, "data_dir", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def server(self, name):
        with patch.object(wake, "_engine_url", return_value="http://127.0.0.1:18765"):
            return wake._mcp_server_config(name, context={"profile": name, "project": "alpha",
                "session": name + "-work", "thread_id": None, "wake_started_at": 100})

    def test_independent_overlays_preserve_managed_settings_and_cleanup(self):
        original = {"model": {"default": "managed-model"},
                    "mcp_servers": {"other": {"command": "other.exe"}, "task_queue": {"command": "old.exe"}}}
        source = self.scope / "config.yaml"
        source.write_text(json.dumps(original), encoding="utf-8")
        (self.scope / ".env").write_text("EXAMPLE_SETTING=test-only\n", encoding="utf-8")
        env1 = {"HERMES_MANAGED_DIR": str(self.scope)}
        env2 = dict(env1)
        first = mcp_runtime.hermes_overlay(self.server("one"), env1)
        second = mcp_runtime.hermes_overlay(self.server("two"), env2)
        self.addCleanup(first.cleanup)
        self.addCleanup(second.cleanup)
        self.assertNotEqual(first.name, second.name)
        for temporary, name in ((first, "one"), (second, "two")):
            data = json.loads((Path(temporary.name) / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(data["model"], original["model"])
            self.assertEqual(data["mcp_servers"]["other"], original["mcp_servers"]["other"])
            env = data["mcp_servers"]["task_queue"]["env"]
            self.assertEqual(env["TASK_QUEUE_ROLE"], name)
            self.assertNotIn("GROUP_DB", env)
            self.assertEqual((Path(temporary.name) / ".env").read_bytes(), (self.scope / ".env").read_bytes())
        self.assertEqual(json.loads(source.read_text()), original)
        first.cleanup()
        self.assertFalse(Path(first.name).exists())
        self.assertTrue(Path(second.name).exists())

    def test_invalid_inherited_config_fails_without_leaking_directory(self):
        # A nonempty sequence must not silently replace the administrator's settings.
        (self.scope / "config.yaml").write_text("- invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            mcp_runtime.hermes_overlay(self.server("one"), {"HERMES_MANAGED_DIR": str(self.scope)})
        self.assertEqual(list((self.root / "runtime" / "mcp").iterdir()), [])

    def test_saved_http_entry_fails_before_start(self):
        home = self.root / "profile"
        home.mkdir()
        (home / "config.yaml").write_text('mcp_servers:\n  task_queue:\n    url: https://old.invalid\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "saved HTTP"):
            mcp_runtime.hermes_overlay(self.server("one"), {}, profile_home=home)

    def test_cleanup_failure_is_reported_without_interrupting_finalization(self):
        temporary = Mock(name="temporary")
        temporary.cleanup.side_effect = PermissionError("in use")
        with self.assertLogs("engine.mcp_runtime", level="ERROR") as captured:
            mcp_runtime.cleanup_overlay(temporary)
        self.assertIn("Cannot remove MCP runtime directory", captured.output[0])

    @unittest.skipUnless(os.environ.get("HERMES_TEST_SOURCE"), "Set HERMES_TEST_SOURCE for native config-loader coverage")
    def test_native_hermes_loads_overlay_and_keeps_member_settings(self):
        source = Path(os.environ["HERMES_TEST_SOURCE"])
        interpreter = os.environ["HERMES_TEST_PYTHON"]
        home = self.root / "profile"
        home.mkdir()
        original = {"model": {"default": "test-model"}, "mcp_servers": {
            "task_queue": {"command": "stale.exe", "args": ["stale.py"], "enabled": False,
                           "env": {"GROUP_ENGINE_URL": "http://stale", "TASK_QUEUE_ROLE": "stale"}},
            "other": {"command": "other.exe"}}}
        (home / "config.yaml").write_text(json.dumps(original), encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC"}}
        env.update({"HERMES_HOME": str(home), "HERMES_MANAGED_DIR": str(self.scope),
                    "PYTHONIOENCODING": "utf-8"})
        temporary = mcp_runtime.hermes_overlay(self.server("one"), env, profile_home=home)
        self.addCleanup(temporary.cleanup)
        code = (
            "import json; from hermes_cli.config import load_config; "
            "from tools.mcp_tool_config import _load_mcp_config; "
            "c=load_config(); print(json.dumps({'model':c['model'],'servers':_load_mcp_config()}))"
        )
        result = subprocess.run([interpreter, "-X", "utf8", "-c", code], cwd=source, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(loaded["model"]["default"], "test-model")
        self.assertEqual(loaded["servers"]["task_queue"]["command"], wake._MCP_PYTHON)
        self.assertEqual(loaded["servers"]["task_queue"]["env"]["TASK_QUEUE_ROLE"], "one")
        self.assertEqual(loaded["servers"]["task_queue"]["env"]["GROUP_ENGINE_URL"], "http://127.0.0.1:18765")
        self.assertTrue(loaded["servers"]["task_queue"]["enabled"])
        self.assertIn("other", loaded["servers"])
        self.assertEqual(json.loads((home / "config.yaml").read_text()), original)

    def test_codex_inline_config_has_api_and_context_without_database(self):
        with patch.object(wake, "_engine_url", return_value="http://127.0.0.1:18765"):
            args = wake._codex_mcp_config("one", "alpha", context={
                "profile": "one", "project": "alpha", "session": "test",
                "thread_id": 1, "wake_started_at": 100}, chat=True)
        server = tomllib.loads(args[1])["mcp_servers"]["task_queue"]
        self.assertNotIn("GROUP_DB", server["env"])
        self.assertEqual(server["env"]["GROUP_ENGINE_URL"], "http://127.0.0.1:18765")
        self.assertEqual(server["env"]["HERMES_CHAT_MODE"], "1")
        context = json.loads(server["env"]["XDG_GROUP_ENGINE_EXECUTION"])
        self.assertEqual(context, {"profile": "one", "project": "alpha", "session": "test",
                                   "thread_id": 1, "wake_started_at": 100})
        self.assertEqual(auth.execution_scope("one", server["env"]["GROUP_ENGINE_TOKEN"], auth.signing_keys()[1]),
                         {**context, 'signing_generation': auth.signing_keys()[0]})


if __name__ == "__main__":
    unittest.main()
