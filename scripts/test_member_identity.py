"""Host-neutral identity display and global rename, using isolated databases."""
from unittest.mock import patch
import unittest

from scripts import test_project_roles
from engine import auth, discovery


class MemberIdentityTests(unittest.TestCase):
    def setUp(self):
        gateway = patch('engine.hermes_gateway.enabled', return_value=False)
        gateway.start()
        self.addCleanup(gateway.stop)
        test_project_roles.ProjectRoleTests.setUp(self)

    join = test_project_roles.ProjectRoleTests.join
    roster = test_project_roles.ProjectRoleTests.roster
    set_role = test_project_roles.ProjectRoleTests.set_role

    def test_shared_projection_and_global_rename_for_both_hosts(self):
        for host in ('hermes', 'codex'):
            with self.subTest(host=host):
                member = self.join(host=host)
                self.join('q', host=host)
                before = next(m for m in self.roster() if m['profile'] == member)
                self.set_role(member, 'developer')
                result = self.client.patch(f'/members/{member}/display-name', json={'display_name': '  New name  '})
                self.assertEqual(result.status_code, 200, result.text)
                for project in ('p', 'q'):
                    roster = next(m for m in self.roster(project) if m['profile'] == member)
                    view = self.client.get(f'/projects/{project}/workspaces').json()
                    card = next(m for m in view['members'] if m['profile'] == member)
                    for field in ('profile', 'bot_id', 'host_profile', 'display_name'):
                        self.assertEqual(roster[field], card[field])
                    self.assertEqual(card['display_name'], 'New name')
                    self.assertEqual(card['host_profile'], 'default')
                    self.assertEqual(card['bot_id'], before['bot_id'])
                self.assertEqual(next(m for m in self.roster() if m['profile'] == member)['role'], 'developer')
                self.assertEqual(self.conn.execute('SELECT display_name FROM members WHERE profile=?', (member,)).fetchone()[0], 'New name')
                candidates = [{'host_type': host, 'profile': 'default', 'config_root': str(self.root / host)}]
                with patch.object(discovery, '_scan_hermes', return_value=[]), patch.object(discovery, '_scan_codex', return_value=candidates):
                    self.assertEqual(discovery.scan_candidates(self.conn)[0]['display_name'], 'New name')

    def test_invalid_unauthorized_and_missing_rename_preserve_name(self):
        member = self.join()
        old = self.roster()[0]['display_name']
        for value in ('', '  ', 'x\ny', 'x\0y', 'x' * 201, 42, None):
            response = self.client.patch(f'/members/{member}/display-name', json={'display_name': value})
            self.assertEqual(response.status_code, 400)
        response = self.client.patch(f'/members/{member}/display-name', json={'display_name': 'forbidden'}, headers={'X-Group-Profile': member, 'X-Group-Key': auth.bot_token(member)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.patch('/members/missing/display-name', json={'display_name': 'Name'}).status_code, 404)
        self.assertEqual(self.roster()[0]['display_name'], old)

    def test_audit_failure_rolls_back_both_names(self):
        member = self.join()
        old = self.roster()[0]['display_name']
        with patch('engine.project_workspaces.audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.client.patch(f'/members/{member}/display-name', json={'display_name': 'New'})
        self.assertEqual(self.roster()[0]['display_name'], old)
        self.assertEqual(self.conn.execute('SELECT display_name FROM members WHERE profile=?', (member,)).fetchone()[0], old)

    def test_discovery_marks_current_project_members_including_disabled(self):
        member = self.join()
        self.join('q', host='hermes')
        candidates = [{'host_type': host, 'profile': 'default', 'config_root': str(self.root / host)}
                      for host in ('codex', 'hermes')]
        candidates.append({'host_type': 'codex', 'profile': 'new', 'config_root': str(self.root / 'new')})
        self.conn.execute('UPDATE project_members SET active=0 WHERE project=? AND profile=?', ('p', member))
        self.conn.commit()
        with patch.object(discovery, '_scan_hermes', return_value=[]), patch.object(discovery, '_scan_codex', return_value=candidates):
            rows = self.client.get('/discovery/scan?project=p').json()['candidates']
            self.assertEqual([r['already_project_member'] for r in rows], [True, False, False])
            self.assertEqual([r['already_member'] for r in rows], [True, True, False])
            rows = self.client.get('/discovery/scan?project=q').json()['candidates']
            self.assertEqual([r['already_project_member'] for r in rows], [False, True, False])
            rows = self.client.get('/discovery/scan').json()['candidates']
            self.assertFalse(any(r['already_project_member'] for r in rows))


if __name__ == '__main__':
    import unittest
    unittest.main()
