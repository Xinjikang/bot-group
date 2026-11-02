"""DSH member field removal and automatic approval package detection."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, dsh_auto


class MemberSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        folder = self.home / 'profiles' / 'desktop'
        folder.mkdir(parents=True)
        (folder / 'cordis.patch.yml').write_text('[]\n', encoding='utf-8')
        self.files = [self.home / '.env', self.home / 'AGENTS.md', folder / '.group-engine-auto-review']
        for file in self.files:
            file.write_text('existing\r\n', encoding='utf-8')
        self.bot = dict(config_root=str(self.home), profile='desktop', runtime='dsh')
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        handle = patch('engine.api._profile_editor_access', return_value=None)
        handle.start()
        self.addCleanup(handle.stop)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', None)
        for target, value in (('engine.codex_profile_config.resolve', None),
                              ('engine.dsh_profile_config.resolve', self.bot)):
            handle = patch(target, return_value=value)
            handle.start()
            self.addCleanup(handle.stop)

    def test_api_removes_fields_preserves_files_and_ignores_their_revision(self):
        before = {file: file.read_bytes() for file in self.files}
        data = self.client.get('/members/dsh/config').json()
        removed = {'instructions', 'env', 'auto_review'}
        self.assertFalse(removed & data.keys())
        self.assertFalse(removed & data['plugins'].keys())
        self.files[0].write_text('externally updated', encoding='utf-8')
        before[self.files[0]] = self.files[0].read_bytes()
        response = self.client.put('/members/dsh/config', json={
            'revision': data['revision'], 'description': 'updated', 'persona': 'persona',
            'model': 'deepseek-flash', 'provider': 'deepseek-account', 'reasoning_effort': 'max'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({file: file.read_bytes() for file in self.files}, before)
        current = self.client.get('/members/dsh/config').json()
        self.assertEqual(current['persona'], 'persona')
        self.assertEqual(current['description'], 'updated')
        for field in removed:
            response = self.client.put('/members/dsh/config', json={
                'revision': current['revision'], field: ''})
            self.assertIn(response.status_code, (400, 422), (field, response.text))
        self.assertEqual({file: file.read_bytes() for file in self.files}, before)

    def test_package_absent_and_local_package_present_ignore_old_switch(self):
        self.assertFalse(dsh_auto.plugin_available(self.bot))
        pkg = self.home / 'node_modules' / dsh_auto.AUTO_PLUGIN_PACKAGE / 'package.json'
        pkg.parent.mkdir(parents=True)
        pkg.write_text('{}', encoding='utf-8')
        for flag in ('off', 'on', 'garbage'):
            self.files[2].write_text(flag, encoding='utf-8')
            self.assertTrue(dsh_auto.plugin_available(self.bot))
        (self.home / 'profiles/desktop/cordis.patch.yml').write_text(
            '- id: auto-review\n  disabled: true\n', encoding='utf-8')
        self.assertTrue(dsh_auto.plugin_available(self.bot))
        entries = dsh_auto.overlay_inserts('adapter.mjs')[0]['insert']
        self.assertFalse(entries[0]['disabled'])

    def test_bound_archive_detection_and_corruption_is_error(self):
        entry = self.root / 'resources/runtime/cli/bin/dsh.cmd'
        entry.parent.mkdir(parents=True)
        entry.write_text('@echo off', encoding='utf-8')
        self.bot['runtime_entry'] = str(entry)
        archive = self.root / 'resources/app.asar'
        self.assertFalse(dsh_auto.plugin_available(self.bot))
        for present in (True, False):
            node = {'size': 2, 'offset': '0'}
            for name in reversed(('dsh', 'node_modules', '@deepseek-ai',
                                  'dsh-experimental-auto-review' if present else 'other', 'package.json')):
                node = {'files': {name: node}}
            content = json.dumps(node).encode()
            archive.write_bytes(struct.pack('<IIII', 4, 0, 0, len(content)) + content)
            self.assertEqual(dsh_auto.plugin_available(self.bot), present)
        archive.write_bytes(b'broken')
        with self.assertRaises(ValueError):
            dsh_auto.plugin_available(self.bot)


if __name__ == '__main__':
    unittest.main()
