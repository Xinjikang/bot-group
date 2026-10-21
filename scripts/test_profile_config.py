"""Run with .venv/Scripts/python -m unittest discover -s scripts -p test_profile_config.py."""

import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from ruamel.yaml import YAML

from engine import api, auth, db, profile_config as pc


class ProfileConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / "profiles" / "test-bot"
        self.folder.mkdir(parents=True)
        self.env_patch = patch.dict(os.environ, {
            "HERMES_HOME": str(self.root), "GROUP_DB": str(self.root / "group.db"),
            "GROUP_ADMIN_PASSWORD": "test-password", "GROUP_SECRET": "test-secret",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        (self.folder / "config.yaml").write_text(
            '# keep this heading\nmodel:\n  default: "old-model" # model note\n'
            '  provider: old-provider\n  base_url: https://example.test\n'
            '  context_length: 1000\nmcp_servers:\n  demo:\n    command: demo\n'
            '# keep this footer\n', encoding="utf-8")
        (self.folder / "profile.yaml").write_text(
            'description: old\ndescription_auto: true\ndisplay_name: Test\n', encoding="utf-8")
        (self.folder / "SOUL.md").write_bytes(b"# Soul\r\nold\r\n")
        (self.folder / ".env").write_bytes(b"# private\r\nTOKEN=test-only\r\n")
        conn = db.get_connection()
        db.init_schema(conn)
        # req#27：members 无 active 列；停用走 project_members（项目作用域）
        conn.execute("INSERT OR REPLACE INTO members (profile, display_name, role) VALUES ('test-bot', 'Test', 'dev')")
        conn.execute("INSERT OR REPLACE INTO project_members (project, profile, active) VALUES ('xjk', 'test-bot', 0)")
        conn.commit()
        conn.close()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + auth.login("test-password")["token"]}

    def values(self, **changes):
        value = pc.read("test-bot")
        value.update(changes)
        return value

    def test_read_inactive_member_auth_and_no_cache(self):
        url = "/members/test-bot/config"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers={"X-Group-Profile": "test-bot", "X-Group-Key": auth.bot_token("test-bot")}).status_code, 403)
        result = self.client.get(url, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertIn("TOKEN=test-only", result.json()["env"])
        self.assertEqual(self.client.get("/members/unknown/config", headers=self.headers).status_code, 404)

    def test_no_change_preserves_every_byte(self):
        before = pc._snapshot(self.folder)
        values = self.values()
        values["env"] = values["env"].replace("\r\n", "\n")
        pc.save("test-bot", values)
        self.assertEqual(pc._snapshot(self.folder), before)

    def test_all_fields_save_preserves_other_config_and_comments(self):
        values = self.values(description="新描述", soul="# 新人格\n", env="TOKEN=new-test\n",
                             model="new-model", provider="new-provider")
        result = self.client.put("/members/test-bot/config", headers=self.headers, json=values)
        self.assertEqual(result.status_code, 200, result.text)
        config_text = (self.folder / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("# keep this heading", config_text)
        self.assertIn("# keep this footer", config_text)
        self.assertIn("# model note", config_text)
        config = YAML().load(config_text)
        self.assertEqual(config["model"]["base_url"], "https://example.test")
        self.assertEqual(config["mcp_servers"]["demo"]["command"], "demo")
        self.assertEqual(pc.read("test-bot")["description"], "新描述")
        meta = YAML().load((self.folder / "profile.yaml").read_text(encoding="utf-8"))
        self.assertFalse(meta["description_auto"])
        self.assertEqual(meta["display_name"], "Test")
        self.assertEqual((self.folder / ".env").read_bytes(), b"TOKEN=new-test\r\n")
        self.assertFalse(list(self.folder.glob(".group-edit-*")))
        backup = self.folder / result.json()["backup"]
        self.assertEqual((backup / ".env").read_bytes(), b"# private\r\nTOKEN=test-only\r\n")
        self.assertIn(b"old-model", (backup / "config.yaml").read_bytes())
        self.assertEqual((backup / "SOUL.md").read_bytes(), b"# Soul\r\nold\r\n")

    def test_stale_revision_rejected(self):
        values = self.values(description="changed")
        (self.folder / ".env").write_text("external=true", encoding="utf-8")
        before = pc._snapshot(self.folder)
        result = self.client.put("/members/test-bot/config", headers=self.headers, json=values)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(pc._snapshot(self.folder), before)

    def test_reasoning_strength_preserves_other_bytes(self):
        original = (self.folder / 'config.yaml').read_bytes()
        pc.save('test-bot', self.values(reasoning_effort='high'))
        updated = (self.folder / 'config.yaml').read_bytes()
        self.assertTrue(updated.startswith(original))
        self.assertEqual(pc.read('test-bot')['reasoning_effort'], 'high')
        pc.save('test-bot', self.values(reasoning_effort='none'))
        self.assertEqual(YAML().load((self.folder / 'config.yaml').read_text())['agent']['reasoning_effort'], 'none')
        before = pc._snapshot(self.folder)
        with self.assertRaises(HTTPException):
            pc.save('test-bot', self.values(reasoning_effort='unknown'))
        self.assertEqual(pc._snapshot(self.folder), before)

    def test_reasoning_existing_agent_preserves_comments_and_overrides(self):
        path = self.folder / 'config.yaml'
        path.write_bytes(path.read_bytes() + b'agent:\r\n  reasoning_effort: false # note\r\n  reasoning_overrides:\r\n    special: low\r\n')
        self.assertEqual(pc.read('test-bot')['reasoning_effort'], 'none')
        original = path.read_bytes()
        pc.save('test-bot', self.values(reasoning_effort='xhigh'))
        self.assertEqual(path.read_bytes(), original.replace(b'false # note', b'"xhigh" # note'))

    def test_change_during_edit_is_detected(self):
        update = pc._update_model
        def concurrent_change(*args):
            (self.folder / ".env").write_text("external=true", encoding="utf-8")
            return update(*args)
        values = self.values(model="new", provider="new")
        with patch.object(pc, "_update_model", side_effect=concurrent_change):
            with self.assertRaises(HTTPException) as error:
                pc.save("test-bot", values)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("old-model", (self.folder / "config.yaml").read_text())

    def test_partial_write_rolls_back_all_files(self):
        before = pc._snapshot(self.folder)
        replace = os.replace
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if Path(target).parent == self.folder and Path(target).name == "SOUL.md":
                raise PermissionError("simulated write failure")
            return replace(source, target)
        with patch.object(pc.os, "replace", side_effect=fail_second):
            with self.assertRaises(HTTPException):
                pc.save("test-bot", self.values(description="new", soul="new", env="new"))
        self.assertEqual(pc._snapshot(self.folder), before)
        self.assertFalse(list(self.folder.glob(".group-edit-*")))

    def test_backup_failure_blocks_all_writes(self):
        before = pc._snapshot(self.folder)
        with patch.object(pc, "_backup", side_effect=PermissionError("backup denied")):
            with self.assertRaises(HTTPException):
                pc.save("test-bot", self.values(description="new", soul="new"))
        self.assertEqual(pc._snapshot(self.folder), before)

    def test_backup_precedes_first_replace_and_history_is_retained(self):
        before = pc._snapshot(self.folder)
        replace = os.replace
        def verify_backup(source, target):
            if Path(target).parent == self.folder:
                backups = list((self.folder / ".group-engine-backups").iterdir())
                self.assertEqual(len(backups), 1)
                self.assertEqual((backups[0] / "SOUL.md").read_bytes(), before["SOUL.md"])
                self.assertEqual({p.name for p in backups[0].iterdir()}, {"SOUL.md", "manifest.json"})
            return replace(source, target)
        with patch.object(pc.os, "replace", side_effect=verify_backup):
            first = pc.save("test-bot", self.values(soul="first edit"))
        second = pc.save("test-bot", self.values(soul="second edit"))
        self.assertNotEqual(first["backup"], second["backup"])
        self.assertEqual((self.folder / first["backup"] / "SOUL.md").read_bytes(), before["SOUL.md"])
        self.assertEqual((self.folder / second["backup"] / "SOUL.md").read_bytes(), b"first edit")

    def test_backup_contains_only_changed_files(self):
        for changes, expected in (
            ({"description": "new description"}, {"profile.yaml"}),
            ({"env": "FLAG=new"}, {".env"}),
            ({"model": "new-model", "provider": "new-provider"}, {"config.yaml"}),
            ({"soul": "new soul", "env": "FLAG=newer"}, {"SOUL.md", ".env"}),
        ):
            with self.subTest(changes=list(changes)):
                before = pc._snapshot(self.folder)
                result = pc.save("test-bot", self.values(**changes))
                backup = self.folder / result["backup"]
                self.assertEqual({p.name for p in backup.iterdir()}, expected | {"manifest.json"})
                manifest = json.loads((backup / "manifest.json").read_text())
                self.assertEqual(set(manifest["changed_files"]), expected)
                for name in expected:
                    self.assertEqual((backup / name).read_bytes(), before[name])

    def test_unchanged_save_creates_no_backup(self):
        result = pc.save("test-bot", self.values())
        self.assertIsNone(result["backup"])
        self.assertFalse((self.folder / ".group-engine-backups").exists())

    def test_invalid_model_layout_does_not_write_other_tabs(self):
        (self.folder / "config.yaml").write_text('model: old-model\n', encoding="utf-8")
        before = pc._snapshot(self.folder)
        with self.assertRaises(HTTPException):
            pc.save("test-bot", self.values(description="new", model="new"))
        self.assertEqual(pc._snapshot(self.folder), before)

    def test_only_two_value_spans_change_with_bom_crlf_and_comments(self):
        original = (b'\xef\xbb\xbf# header\r\nmodel:\r\n  default: old-model  # keep\r\n'
                    b'  provider: old-provider\r\n  base_url: "https://example.test"\r\n'
                    b'other: {indent: unusual}\r\n# footer without newline')
        updated = pc._update_model(original, {"model": 'new:"#model', "provider": "custom"})
        expected = original.replace(b'old-model', b'"new:\\"#model"').replace(b'old-provider', b'"custom"')
        self.assertEqual(updated, expected)

    def test_only_changed_field_is_touched(self):
        original = (self.folder / "config.yaml").read_bytes()
        updated = pc._update_model(original, {"model": "old-model", "provider": "new-provider"})
        self.assertEqual(updated, original.replace(b'old-provider', b'"new-provider"'))

    def test_missing_model_fields_are_inserted_without_rewriting_other_bytes(self):
        block = b'model:\n  default: "new-model"\n  provider: "new-provider"\n'
        cases = (
            (None, block),
            (b'# empty config', b'# empty config\n' + block),
            (b'mcp_servers:\n  demo: {command: demo}\n# footer',
             b'mcp_servers:\n  demo: {command: demo}\n# footer\n' + block),
            (b'\xef\xbb\xbf---\r\nother: true # keep\r\n...\r\n',
             b'\xef\xbb\xbf---\r\nother: true # keep\r\n' + block.replace(b'\n', b'\r\n') + b'...\r\n'),
            (b'model: # heading\n    default: new-model # keep\nother: yes\n',
             b'model: # heading\n    provider: "new-provider"\n    default: new-model # keep\nother: yes\n'),
            (b'model:\n  provider: new-provider\n  base_url: https://example.test\n',
             b'model:\n  default: "new-model"\n  provider: new-provider\n  base_url: https://example.test\n'),
            (b'model:\n  base_url: https://example.test\n',
             block + b'  base_url: https://example.test\n'),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(pc._update_model(original, {'model': 'new-model', 'provider': 'new-provider'}), expected)

    def test_first_model_save_backs_up_missing_config(self):
        (self.folder / 'config.yaml').unlink()
        values = self.values(model='new-model', provider='new-provider', reasoning_effort='high')
        result = pc.save('test-bot', values)
        current = pc.read('test-bot')
        self.assertEqual((current['model'], current['provider'], current['reasoning_effort']),
                         ('new-model', 'new-provider', 'high'))
        manifest = json.loads((self.folder / result['backup'] / 'manifest.json').read_text())
        self.assertEqual(manifest['changed_files'], ['config.yaml'])
        self.assertEqual(manifest['missing_files'], ['config.yaml'])

    def test_shared_alias_and_multiline_are_refused(self):
        for text in (
            'other: &shared\n  default: old\n  provider: old\nmodel: *shared\n',
            'model:\n  default: &shared old\n  provider: old\nother: *shared\n',
            'model:\n  default: |\n    old\n  provider: old\n',
        ):
            with self.assertRaises(HTTPException):
                pc._update_model(text.encode(), {"model": "new", "provider": "new"})

    def test_default_and_missing_files(self):
        current = pc.read("default")
        current.update(description="default desc", soul="new soul", env="A=B")
        pc.save("default", current)
        self.assertEqual(pc.read("default")["description"], "default desc")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.folder)}):
            self.assertEqual(pc._directory("default"), self.root)
            self.assertEqual(pc._directory("test-bot"), self.folder)

    def test_path_and_yaml_validation(self):
        for name in ("../test-bot", "user", "test/bot", "C:\\test", ""):
            with self.assertRaises(HTTPException):
                pc.read(name)
        (self.folder / "config.yaml").write_text("model: [unclosed", encoding="utf-8")
        with self.assertRaises(HTTPException) as error:
            pc.read("test-bot")
        self.assertEqual(error.exception.status_code, 400)

    def test_strict_request_and_bot_write_rejected(self):
        values = self.values()
        url = "/members/test-bot/config"
        self.assertEqual(self.client.put(url, headers={"X-Group-Profile": "test-bot", "X-Group-Key": auth.bot_token("test-bot")}, json=values).status_code, 403)
        values["env"] = {"bad": "object"}
        self.assertEqual(self.client.put(url, headers=self.headers, json=values).status_code, 422)


if __name__ == "__main__":
    unittest.main()
