"""Remote checkout safety and reproducibility using temporary Git repositories."""
import importlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.test_plugin_listen_pairing import _plugin_modules
from scripts.relay_dispatch_fixture import isolated_git_origin


class RemoteWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = _plugin_modules('workspace_fixture')['config']
        cls.ws = importlib.import_module(config.__package__ + '.workspaces')

    def git(self, path, *args):
        return subprocess.check_output(['git', '-c', 'user.name=fixture', '-c', 'user.email=t@example.invalid',
                                        *args], cwd=path, text=True, stderr=subprocess.PIPE, env=os.environ.copy()).strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.repo = self.base / 'office'
        self.repo.mkdir()
        self.git(self.repo, 'init', '-b', 'master')
        self.git(self.repo, 'commit', '--allow-empty', '-m', 'initial')
        self.origin = 'https://fixture.invalid/project.git'
        self.bare = self.base / 'origin.git'
        self.git(self.base, 'clone', '--bare', str(self.repo), str(self.bare))
        env = patch.dict(os.environ, isolated_git_origin(self.origin, self.bare))
        env.start()
        self.addCleanup(env.stop)
        self.spec = dict(action='prepare', mode='shared', member='remote-luna', project='alpha',
                         origin=self.origin, baseline=self.git(self.repo, 'rev-parse', 'HEAD'),
                         root=str(self.base / 'ecs'))

    def prepare(self, **changes):
        return self.ws.prepare({**self.spec, **changes})

    def advance(self, publish):
        (self.repo / 'code').write_text('change', encoding='utf-8')
        self.git(self.repo, 'add', 'code')
        self.git(self.repo, 'commit', '-m', 'advance')
        if publish:
            self.git(self.repo, 'push', str(self.bare), 'master')
        return self.git(self.repo, 'rev-parse', 'HEAD')

    def test_bootstrap_exact_baseline_then_advance_both_modes(self):
        latest = self.advance(True)
        shared = self.prepare()
        self.assertEqual(shared['head'], self.spec['baseline'])
        independent = self.prepare(mode='worktree', baseline=latest)
        self.assertEqual(independent['main'], shared['path'])
        self.assertNotEqual(independent['path'], shared['path'])
        self.assertEqual(independent['branch'], 'bot-group/remote-luna')
        self.assertEqual(independent, self.prepare(mode='worktree', baseline=latest))
        self.assertEqual(self.git(shared['main'], 'rev-parse', 'HEAD'), latest)

    def test_dirty_divergent_and_wrong_repository_preserved(self):
        main = Path(self.prepare()['path'])
        draft = main / 'draft'
        draft.write_text('mine', encoding='utf-8')
        self.assertTrue(self.prepare()['dirty'])
        latest = self.advance(True)
        with self.assertRaisesRegex(self.ws.WorkspaceError, '未提交'):
            self.prepare(baseline=latest)
        self.git(main, 'add', 'draft')
        self.git(main, 'commit', '-m', 'local work')
        local = self.git(main, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(self.ws.WorkspaceError, '分叉'):
            self.prepare(baseline=latest)
        with self.assertRaisesRegex(self.ws.WorkspaceError, 'origin'):
            self.prepare(origin='https://other.invalid/alpha.git')
        self.assertEqual(self.git(main, 'rev-parse', 'HEAD'), local)
        self.assertEqual(draft.read_text(encoding='utf-8'), 'mine')

    def test_missing_baseline_and_invalid_inputs(self):
        with self.assertRaisesRegex(self.ws.WorkspaceError, '缺少办公机基线'):
            self.prepare(baseline=self.advance(False))
        self.assertFalse((Path(self.spec['root']) / 'alpha').exists())
        for changes in ({'root': 'relative'}, {'project': '../alpha'}, {'member': 'x/y'},
                        {'origin': self.bare.as_uri()}, {'origin': 'https://secret@fixture.invalid/repo'}):
            with self.subTest(fields=list(changes)), self.assertRaises(self.ws.WorkspaceError):
                self.prepare(**changes)

    def test_task_branch_can_be_pushed_and_merged_in_office(self):
        work = Path(self.prepare(mode='worktree')['path'])
        (work / 'result.txt').write_text('remote change', encoding='utf-8')
        self.git(work, 'add', 'result.txt')
        self.git(work, 'commit', '-m', 'collaboration')
        self.git(work, 'push', 'origin', 'HEAD:refs/heads/bot-group/remote-luna')
        self.git(self.repo, 'fetch', str(self.bare), 'refs/heads/bot-group/remote-luna')
        self.git(self.repo, 'merge', '--ff-only', 'FETCH_HEAD')
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.git(work, 'rev-parse', 'HEAD'))
