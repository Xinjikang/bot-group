"""Enrollment grants use temporary profiles and SQLite; no production writes."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
import tomlkit

from engine import bot_identity, codex_project_access as access, discovery
from scripts import test_project_roles


class CodexProjectAccessTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

    def config(self, text=None, name='config.toml'):
        path = self.root / 'codex' / name
        if text is not None:
            path.write_bytes(text.encode('utf-8'))
        return path

    def policy(self):
        config = tomlkit.parse(self.config().read_text(encoding='utf-8-sig'))
        return config, config['permissions'][config['default_permissions']]

    def test_custom_policy_adds_only_project_root_and_retains_other_files(self):
        config = self.config('''# User-owned settings
model="test-model"
approval_policy="never"
approvals_reviewer="user"
default_permissions="read-only-with-rtk"
[permissions.read-only-with-rtk]
extends=":read-only"
[permissions.read-only-with-rtk.filesystem]
"C:/synthetic-rtk"="write"
[permissions.read-only-with-rtk.network]
enabled=false
''')
        before = config.read_bytes()
        for name in ('auth.json', 'AGENTS.md', 'other.config.toml'):
            self.config('synthetic unchanged', name)
        original = tomlkit.parse(before.decode()).unwrap()
        self.join()
        after, policy = self.policy()
        expected = deepcopy(original)
        expected['permissions']['read-only-with-rtk']['filesystem'][self.root.as_posix()] = 'write'
        self.assertEqual(after.unwrap(), expected)
        self.assertIn('# User-owned settings', config.read_text())
        for name in ('auth.json', 'AGENTS.md', 'other.config.toml'):
            self.assertEqual(self.config(name=name).read_bytes(), b'synthetic unchanged')
        backups = list(config.parent.glob('.group-engine-backups/*/config.toml'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        timestamp = config.stat().st_mtime_ns
        self.join()
        self.assertEqual(config.stat().st_mtime_ns, timestamp)
        self.assertEqual(list(config.parent.glob('.group-engine-backups/*/config.toml')), backups)

    def test_second_project_accumulates_only_its_own_root(self):
        qroot = self.root / 'second-project'
        self.conn.execute('UPDATE projects SET root=? WHERE name=?', (str(qroot), 'q'))
        self.conn.commit()
        self.join()
        self.join('q')
        _, policy = self.policy()
        self.assertEqual(policy['filesystem'], {self.root.as_posix(): 'write', qroot.as_posix(): 'write'})

    def test_named_profile_does_not_change_base_or_sibling(self):
        base = self.config('default_permissions="restricted"\n[permissions.restricted]\nextends=":read-only"\n[permissions.restricted.network]\nenabled=false\n')
        named = self.config('model="member-model"\n', 'member.config.toml')
        sibling = self.config('model="sibling"\n', 'sibling.config.toml')
        originals = (base.read_bytes(), sibling.read_bytes())
        self.join(profile='member')
        self.assertEqual((base.read_bytes(), sibling.read_bytes()), originals)
        self.assertEqual(tomlkit.parse(named.read_text()).unwrap(), {
            'model': 'member-model', 'permissions': {'restricted': {'filesystem': {self.root.as_posix(): 'write'}}}})

    def test_inline_profile_overrides_are_preserved(self):
        base = self.config('default_permissions=":workspace"\n[profiles.member]\nmodel="chosen-model"\napproval_policy="on-request"\n')
        before = base.read_bytes()
        self.join(profile='member')
        selected = tomlkit.parse(self.config(name='member.config.toml').read_text())
        self.assertEqual(selected['model'], 'chosen-model')
        self.assertEqual(selected['approval_policy'], 'on-request')
        self.assertEqual(base.read_bytes(), before)

    def test_worktree_grants_assigned_directory_and_shared_test_reports(self):
        trees = self.root / 'trees'
        self.conn.execute("UPDATE projects SET directory_mode='worktree',worktree_root=? WHERE name='p'", (str(trees),))
        self.conn.commit()
        member = self.join()
        _, policy = self.policy()
        self.assertEqual(policy['filesystem'], {(trees / member).as_posix(): 'write',
                                                (self.root / 'reports').as_posix(): 'write'})

    def test_denied_reports_directory_preserves_config_and_membership(self):
        trees = self.root / 'trees'
        self.conn.execute("UPDATE projects SET directory_mode='worktree',worktree_root=? WHERE name='p'", (str(trees),))
        self.conn.commit()
        original = tomlkit.dumps({'default_permissions': 'restricted', 'permissions': {
            'restricted': {'extends': ':read-only', 'filesystem': {
                (self.root / 'reports').as_posix(): 'deny'}}}})
        path = self.config(original)
        response = self.client.post('/bots/register', json={'project': 'p', 'host_type': 'codex',
            'profile': 'default', 'config_root': str(path.parent)})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(path.read_bytes(), original.encode('utf-8'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)

    def test_hermes_and_registration_without_project_do_not_write_config(self):
        path = self.config('invalid TOML deliberately untouched')
        self.join(host='hermes')
        response = self.client.post('/bots/register', json={'host_type': 'codex', 'profile': 'default',
                                                          'config_root': str(path.parent)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_text(), 'invalid TOML deliberately untouched')
        self.assertFalse((path.parent / '.group-engine-backups').exists())

    def test_invalid_config_rejects_enrollment_without_partial_membership(self):
        path = self.config('synthetic-secret = unquoted-value')
        response = self.client.post('/bots/register', json={'project': 'p', 'host_type': 'codex',
            'profile': 'default', 'config_root': str(path.parent)})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn('synthetic-secret', response.text)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)
        self.assertEqual(path.read_text(), 'synthetic-secret = unquoted-value')

    def test_legacy_workspace_grant_preserves_policy_and_roots(self):
        config = tomlkit.parse('sandbox_mode="workspace-write"\napproval_policy="on-request"\n[sandbox_workspace_write]\nnetwork_access=false\nwritable_roots=["C:/existing"]\n')
        original = deepcopy(config.unwrap())
        access.grant(config, config, self.root)
        original['sandbox_workspace_write']['writable_roots'].append(self.root.as_posix())
        self.assertEqual(config.unwrap(), original)
        access.grant(config, config, self.root)
        self.assertEqual(config.unwrap(), original)

    def test_explicit_legacy_readonly_and_denied_root_require_policy_decision(self):
        configs = [tomlkit.parse('sandbox_mode="read-only"\n'),
                   {'default_permissions': 'restricted', 'permissions': {'restricted': {
                       'extends': ':read-only', 'filesystem': {self.root.as_posix(): 'deny'}}}}]
        for config in configs:
            with self.subTest(config=config):
                before = deepcopy(config)
                with self.assertRaises(HTTPException) as caught:
                    access.grant(config, config, self.root)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(config, before)

    def test_named_legacy_profile_keeps_inherited_writable_roots(self):
        inherited = {'sandbox_mode': 'workspace-write', 'sandbox_workspace_write': {
            'network_access': False, 'writable_roots': ['C:/existing']}}
        config = {'sandbox_workspace_write': {'exclude_tmpdir_env_var': True}}
        access.grant(config, inherited, self.root)
        self.assertEqual(config['sandbox_workspace_write'], {
            'exclude_tmpdir_env_var': True, 'writable_roots': ['C:/existing', self.root.as_posix()]})

    def test_scoped_denies_survive_directory_grant(self):
        config = {'default_permissions': 'restricted', 'permissions': {'restricted': {
            'extends': ':read-only', 'filesystem': {self.root.as_posix(): {'.': 'read', '.env': 'deny'}}}}}
        access.grant(config, config, self.root)
        self.assertEqual(config['permissions']['restricted']['filesystem'][self.root.as_posix()],
                         {'.': 'write', '.env': 'deny'})

    def test_bom_crlf_and_idempotence(self):
        path = self.config('\ufeff# Kept comment\r\ndefault_permissions=":workspace"\r\n')
        self.join()
        content = path.read_bytes()
        self.assertTrue(content.startswith(b'\xef\xbb\xbf# Kept comment\r\n'))
        self.assertNotIn(b'\n', content.replace(b'\r\n', b''))
        self.join()
        self.assertEqual(content, path.read_bytes())

    def test_second_profile_failure_restores_first_profile_and_database(self):
        before = self.config().read_bytes()
        other = self.root / 'other-home'
        other.mkdir()
        (other / 'config.toml').write_text('bad config', encoding='utf-8')
        members = []
        for home in (self.config().parent, other):
            reg = discovery.register_bot(self.conn, 'codex', 'default', str(home), 'Test')
            members.append(bot_identity.enroll(self.conn, reg['bot_id'], 'p'))
        with self.assertRaises(HTTPException):
            access.commit_enrollment(self.conn, 'p', members)
        self.assertEqual(self.config().read_bytes(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)

    def test_database_commit_failure_restores_config(self):
        before = self.config().read_bytes()
        reg = discovery.register_bot(self.conn, 'codex', 'default', str(self.config().parent), 'Test')
        member = bot_identity.enroll(self.conn, reg['bot_id'], 'p')
        failing = Mock(wraps=self.conn)
        failing.commit.side_effect = RuntimeError('isolated commit failure')
        with self.assertRaisesRegex(RuntimeError, 'isolated commit failure'):
            access.commit_enrollment(failing, 'p', [member])
        self.assertEqual(self.config().read_bytes(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)

    def test_config_write_failure_rolls_back_enrollment(self):
        before = self.config().read_bytes()
        with patch.object(access.files, '_stage', side_effect=PermissionError('isolated write denial')):
            with self.assertRaises(PermissionError):
                self.join()
        self.assertEqual(self.config().read_bytes(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
