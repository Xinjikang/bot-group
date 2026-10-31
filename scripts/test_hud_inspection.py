"""Read-only evidence, bounded documents, missing configuration and unknown results."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import hud_inspection as probe


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_entry_docs_links_and_coverage_survive_large_tree(self):
        (self.root/'archive').mkdir()
        for i in range(170):
            (self.root/'archive'/f'{i:03}.md').write_text('旧资料'*3000, encoding='utf-8')
        (self.root/'README.md').write_text('[工程说明](custom/guide.md)', encoding='utf-8')
        (self.root/'custom').mkdir()
        (self.root/'custom/guide.md').write_text('Git 远程仓库待配置。', encoding='utf-8')
        (self.root/'.env').write_text('secret=fixture', encoding='utf-8')
        docs = probe.documents(str(self.root))
        self.assertEqual(docs['files'][:2], ['README.md', 'custom/guide.md'])
        self.assertIn('Git 远程仓库', docs['documents']['custom/guide.md']['text'])
        self.assertLessEqual(sum(len(d['text']) for d in docs['documents'].values()), 60000)
        self.assertFalse(docs['coverage']['scan_complete'])
        self.assertTrue(docs['coverage']['unread'])
        self.assertNotIn('.env', docs['structure'])

    def inspect(self, answers, initialized=True, pending=None):
        status = {'initialized': initialized, 'projectPath': str(self.root),
                  'pendingChanges': pending or {}, 'fileCount': 4}
        with patch.object(probe.shutil, 'which', return_value='git-fixture'), \
             patch.object(probe, 'codegraph_command', return_value=['codegraph-fixture']), \
             patch.object(probe, 'run', side_effect=answers+[(0, json.dumps(status))]):
            return probe.inspect({'root': str(self.root)}, probe.documents(str(self.root)))

    def test_remote_missing_codegraph_stale_and_missing_link(self):
        (self.root/'README.md').write_text('[约定](docs/missing.md) [外部](https://example.com/) [锚点](#section)', encoding='utf-8')
        result = self.inspect([(0, str(self.root)), (1, ''), (0, '')], pending={'modified': 2})
        checks = {c['key']: c for c in result['checks']}
        self.assertEqual(checks['git_remote']['status'], 'missing')
        self.assertEqual(checks['codegraph']['status'], 'changed')
        self.assertEqual(checks['document_link']['evidence'], {'source': 'README.md', 'target': 'docs/missing.md'})

    def test_remote_credentials_are_never_in_context(self):
        result = self.inspect([(0, str(self.root)), (0, 'remote.origin.url https://user:secret@example.com/repo'), (0, ' M README.md')])
        self.assertNotIn('secret', json.dumps(result))
        checks = {c['key']: c for c in result['checks']}
        self.assertEqual(checks['git_remote']['evidence']['remotes'], ['origin'])
        self.assertEqual(checks['git_changes']['status'], 'changed')
        self.assertEqual(checks['git_changes']['evidence']['files'], ['README.md'])

    def test_codegraph_uninitialized_and_probe_failure_remain_distinct(self):
        result = self.inspect([(0, str(self.root)), (1, ''), (0, '')], initialized=False)
        self.assertEqual(next(c for c in result['checks'] if c['key']=='codegraph')['status'], 'missing')
        with patch.object(probe.shutil, 'which', return_value='git-fixture'), \
             patch.object(probe, 'codegraph_command', return_value=['codegraph-fixture']), \
             patch.object(probe, 'run', return_value=(None, 'timeout')):
            result = probe.inspect({'root': str(self.root)}, probe.documents(str(self.root)))
        self.assertEqual([c['status'] for c in result['checks']][1:], ['unknown', 'unknown'])

    def test_repository_ownership_mismatch(self):
        result = self.inspect([(0, str(self.root.parent))])
        self.assertEqual(next(c for c in result['checks'] if c['key']=='git')['status'], 'conflict')

    def test_external_link_paths_are_not_read(self):
        self.assertIsNone(probe.local_link(self.root, 'README.md', '../outside.md'))
        self.assertIsNone(probe.local_link(self.root, 'README.md', 'file:///outside.md'))
        self.assertEqual(probe.local_link(self.root, 'README.md', 'docs/a%20b.md#part'), Path('docs/a b.md'))

    def test_role_character_evidence_is_visible_precise_and_read_only(self):
        members = [{'profile': 'default', 'role': 'secretary\u200c', 'role_id': 41, 'active': 1},
                   {'profile': 'regular', 'role': 'manager', 'active': 1},
                   {'profile': 'inactive', 'role': 'hidden\u200b', 'active': 0}]
        with patch.object(probe.shutil, 'which', return_value=None), patch.object(probe, 'codegraph_command', return_value=None):
            result = probe.inspect({'root': str(self.root), 'members': members}, {'documents': {}})
        checks = [c for c in result['checks'] if c['key'] == 'member_role_characters']
        self.assertEqual(len(checks), 1)
        evidence = checks[0]['evidence']
        self.assertEqual(evidence['visible_value'], 'secretary[U+200C]')
        self.assertEqual(evidence['characters'], [{'position': 10, 'codepoint': 'U+200C', 'name': 'ZERO WIDTH NON-JOINER'}])
        self.assertEqual(evidence['profile'], 'default')
        self.assertEqual(evidence['role_id'], 41)
        self.assertIn('task_dispatch_impact_not_checked', evidence['impact'])
        self.assertEqual(members[0]['role'], 'secretary\u200c')


if __name__ == '__main__':
    unittest.main()
