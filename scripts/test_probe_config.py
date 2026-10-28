"""Portable probe configuration: relocate scripts and reject ambiguous resources."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.probe_config import absolute_path, hermes_source, loopback_url, port_number

ROOT = Path(__file__).resolve().parents[1]


class ProbeConfigTests(unittest.TestCase):
    def test_required_absolute_paths_and_source(self):
        with tempfile.TemporaryDirectory(prefix="probe config 空格 ") as temp:
            root = Path(temp).resolve()
            self.assertEqual(absolute_path(temp, "TEST_PATH"), root)
            for invalid in ("", " ", "relative/path"):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    absolute_path(invalid, "TEST_PATH")
            with patch.dict(os.environ, {"HERMES_TEST_SOURCE": temp}):
                with self.assertRaises(ValueError):
                    hermes_source()
                (root / "hermes_cli").mkdir()
                self.assertEqual(hermes_source(), str(root))

    def test_loopback_origins_and_port_bounds(self):
        self.assertEqual(loopback_url("http://127.0.0.1:32123/"), "http://127.0.0.1:32123")
        for value in ("http://example.com:32123", "http://127.0.0.1", "http://127.0.0.1:0",
                      "http://127.0.0.1:65536", "http://user:password@127.0.0.1:32123",
                      "http://127.0.0.1:32123/app/", "http://127.0.0.1:32123?x=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loopback_url(value)
        for value in (-1, 0, 65536):
            with self.assertRaises(ValueError):
                port_number(value)

    def test_relocated_gateway_scripts_use_configured_resources(self):
        with tempfile.TemporaryDirectory(prefix="portable 探针 ") as temp:
            root = Path(temp)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("probe_config.py", "gw_probe.py", "gw_stop_probe.py", "gw_mcp_probe.py",
                         "gw_ownership_probe.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            data = root / "assigned data"
            env = {k: v for k, v in os.environ.items() if not k.startswith("GW_")}
            env.update(GW_PROBE_DATA=str(data), GW_EVIDENCE_DIR=str(root / "evidence"), PYTHONUTF8="1")
            code = (
                "import json, gw_probe as g, gw_mcp_probe as m, gw_ownership_probe as o; "
                "print(json.dumps([str(g.PROBE_DATA), str(g.HERMES_HOME), str(m.TASK_DIR), "
                "str(o.ROOT), str(o.BASE), str(o.SCRATCH), str(o.PY), o.heartbeat(), "
                "str(g.EVIDENCE_DIR), str(o.EVIDENCE)]))"
            )
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=scripts, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(Path(actual[0]), data)
            self.assertEqual(Path(actual[1]), data / "task-250/gw-probe-home")
            self.assertEqual(Path(actual[2]), data / "task-253")
            self.assertEqual(Path(actual[3]), root)
            self.assertTrue(Path(actual[5]).is_relative_to(data))
            self.assertEqual(Path(actual[6]), Path(sys.executable))
            self.assertTrue(actual[7]["skipped"])
            self.assertEqual(actual[8:], [str(root / "evidence")] * 2)
            self.assertFalse(data.exists(), "Import/config inspection must not create resource directories")
            env.pop("GW_PROBE_DATA")
            missing = subprocess.run([sys.executable, "-B", "-c", "import gw_probe"],
                                     cwd=scripts, env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("Set GW_PROBE_DATA", missing.stderr)
            self.assertFalse(data.exists())

    def test_legacy_source_entry_points_fail_before_creating_home(self):
        from scripts import test_hermes_embed_entry, test_hermes_session_restore
        from scripts import test_hermes_steer_delivery, test_session_binding
        with patch.dict(os.environ, {"HERMES_TEST_SOURCE": ""}), \
                patch("tempfile.mkdtemp", side_effect=AssertionError("unexpected filesystem mutation")):
            for module in (test_hermes_embed_entry, test_hermes_session_restore,
                           test_hermes_steer_delivery, test_session_binding):
                with self.subTest(module=module.__name__), self.assertRaisesRegex(ValueError, "HERMES_TEST_SOURCE"):
                    module.main()

    def test_cli_validation_without_browser_or_service_start(self):
        cases = (
            ("verify_release_install.py", []),
            ("verify_release_browser.py", ["--url", "http://127.0.0.1:34567", "--profile", "unused",
                                           "--debug-port", "34567", "--report-dir", "unused"]),
            ("verify_hide_chain.py", []),
            ("verify_zindex.py", ["--url", "http://example.com:34567", "--project", "fixture"]),
            ("verify_directory_picker_live.py", []),
            ("verify_public_install.py", ["--port", "0", "--evidence", "unused", "--shell", "cmd"]),
        )
        with tempfile.TemporaryDirectory() as temp:
            for script, args in cases:
                with self.subTest(script=script):
                    command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts" / script)]
                    result = subprocess.run(command + args, cwd=temp, capture_output=True,
                                            text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    help_result = subprocess.run(command + ["--help"], cwd=temp, capture_output=True,
                                                 text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertEqual(list(Path(temp).iterdir()), [], "Invalid CLI must not create test resources")


if __name__ == "__main__":
    unittest.main()
