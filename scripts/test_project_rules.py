"""Real temporary worktrees; no production files, Hermes sessions or bots."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine import db, project_rules, wake


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'main'
        self.root.mkdir()
        self.work = self.base / 'dev'
        self.source = self.root / 'AGENTS.md'
        self.source.write_text('Main rules v1', encoding='utf-8')
        self.git('init')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.test')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')
        self.git('worktree', 'add', '--detach', str(self.work))
        patched = patch.dict(os.environ, {'GROUP_DB': str(self.base / 'db.sqlite')})
        patched.start()
        self.addCleanup(patched.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)',
                          ('p', str(self.root), db.now_iso()))
        self.conn.commit()
        self.target = self.work / 'AGENTS.override.md'
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('p',), ('dev',))
        self.conn.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT)

    def test_manual_edit_sync_idempotence_and_session_continuity(self):
        original = (self.work / 'AGENTS.md').read_bytes()
        session = wake.session_name('dev', 'work', project='p')
        project_rules.sync('p', self.work)
        stamp = self.target.stat().st_mtime_ns
        project_rules.sync('p', self.work)
        self.assertEqual(stamp, self.target.stat().st_mtime_ns)
        self.source.write_text('Manually edited main v2', encoding='utf-8')
        project_rules.sync('p', self.work)
        self.assertTrue(self.target.read_bytes().endswith(b'Manually edited main v2'))
        self.assertEqual((self.work / 'AGENTS.md').read_bytes(), original)
        self.assertEqual(self.source.read_text(), 'Manually edited main v2')
        self.assertEqual(wake.session_name('dev', 'work', project='p'), session)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.work), 'status', '--porcelain']), b'')

    def test_unmanaged_override_preserved(self):
        self.target.write_text('Personal override', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '保留原文件'):
            project_rules.sync('p', self.work)
        self.assertEqual(self.target.read_text(), 'Personal override')

    def test_changed_managed_override_preserved(self):
        project_rules.sync('p', self.work)
        with self.target.open('ab') as f:
            f.write(b'personal changes')
        content = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, '手动修改'):
            project_rules.sync('p', self.work)
        self.assertEqual(self.target.read_bytes(), content)

    def test_higher_priority_file_and_missing_or_empty_source_block(self):
        other = self.work / '.hermes.md'
        other.write_text('Other rules')
        with self.assertRaisesRegex(ValueError, '优先'):
            project_rules.sync('p', self.work)
        other.unlink()
        self.source.write_text('')
        with self.assertRaisesRegex(ValueError, '为空'):
            project_rules.sync('p', self.work)
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            project_rules.sync('p', self.work)
        self.assertFalse(self.target.exists())

    def test_shared_directory_does_not_create_override(self):
        project_rules.sync('p', self.root)
        self.assertFalse((self.root / 'AGENTS.override.md').exists())

    def test_sync_failure_prevents_spawn(self):
        with patch.object(wake, '_workdir_for', return_value=str(self.work)), \
             patch('engine.task_retry.select_task', return_value=None), \
             patch.object(project_rules, 'sync', side_effect=ValueError('rules conflict')), \
             patch.object(wake.subprocess, 'Popen') as spawn:
            result = wake._spawn('dev', 'work', 'wake', None, 'p')
        self.assertFalse(result['ok'])
        self.assertIn('rules conflict', result['detail'])
        spawn.assert_not_called()

    def test_project_prompt_scoped_fresh_and_same_for_both_modes(self):
        self.conn.execute('UPDATE projects SET display_name=?,description=?,directory_mode=? WHERE name=?',
                          ('中文项目', '项目描述\n第二行', 'worktree', 'p'))
        self.conn.execute('INSERT INTO projects(name,root,display_name,created_at) VALUES(?,?,?,?)',
                          ('q', str(self.base), 'Other project', db.now_iso()))
        self.conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('maintenance_facts','p',?,?)",
                          ('[{"label":"旧备注","value":"LEGACY-SHARED-SENTINEL"}]', db.now_iso()))
        self.conn.commit()
        work = wake.wake_system_prompt('work', 'p', str(self.work))
        chat = wake.wake_system_prompt('chat', 'p', str(self.work))
        self.assertTrue(work.startswith(wake.system_prompt_for('work')))
        self.assertTrue(chat.startswith(wake.system_prompt_for('chat')))
        context = work.split('## 当前项目\n')[1]
        self.assertEqual(context, chat.split('## 当前项目\n')[1])
        injected = json.loads(context.split('\n群聊和任务工具的 project 参数')[0])
        self.assertEqual(set(injected), {'项目稳定 ID', '项目显示名', '项目描述', '项目负责人',
                                        '项目主目录', '目录模式', '当前工作目录', '工作区配置'})
        self.assertEqual(injected['项目稳定 ID'], 'p')
        self.assertEqual(injected['项目描述'], '项目描述\n第二行')
        for prompt in (work, chat):
            self.assertIn('群聊和任务工具的 project 参数使用项目稳定 ID。', prompt)
            self.assertNotIn('LEGACY-SHARED-SENTINEL', prompt)
            self.assertNotIn('用户确认的项目维护资料', prompt)
        self.assertIn('中文项目', context)
        self.assertIn('独立 worktree', context)
        self.assertIn(json.dumps(str(self.work)), context)
        self.assertIn(json.dumps(str(self.root)), context)
        for prompt in (work, chat):
            self.assertNotIn('项目文档存放规则', prompt)
            self.assertNotIn('docs/', prompt)
            self.assertNotIn('reports', prompt)
            self.assertNotIn('报告目录', prompt)
            self.assertNotIn('内部资料目录', prompt)
            self.assertNotIn('代码与版本化文档在当前工作目录操作', prompt)
        self.assertNotIn('Other project', context)
        self.assertNotIn('profile', context)
        self.assertNotIn('AGENTS', context)
        self.assertEqual(work, wake.wake_system_prompt('work', 'p', str(self.work)))
        self.conn.execute("UPDATE projects SET display_name='新名字' WHERE name='p'")
        self.conn.commit()
        updated = wake.wake_system_prompt('work', 'p', str(self.work))
        self.assertIn('新名字', updated)
        self.assertNotIn('中文项目', updated)
        other = wake.wake_system_prompt('work', 'q', str(self.base))
        self.assertIn('共用项目主目录', other)
        self.assertIn(json.dumps(str(self.base)), other)
        self.assertNotIn('报告目录', other)
        self.assertNotIn('新名字', other)

    def test_tracked_override_preserved(self):
        self.target.write_text('Tracked rules')
        subprocess.check_call(['git', '-C', str(self.work), 'add', 'AGENTS.override.md'])
        with self.assertRaisesRegex(ValueError, '已跟踪'):
            project_rules.sync('p', self.work)
        self.assertEqual(self.target.read_text(), 'Tracked rules')

    def test_failed_publication_can_retry_after_another_source_edit(self):
        project_rules.sync('p', self.work)
        self.source.write_text('version two')
        with patch.object(project_rules.os, 'replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                project_rules.sync('p', self.work)
        self.assertEqual(self.target.read_text(), 'Main rules v1')
        self.source.write_text('version three')
        project_rules.sync('p', self.work)
        self.assertEqual(self.target.read_text(), 'version three')

    def test_link_override_preserved(self):
        try:
            self.target.symlink_to(self.source)
        except OSError:
            self.skipTest('Windows symlink permission unavailable')
        with self.assertRaisesRegex(ValueError, '保留已有'):
            project_rules.sync('p', self.work)
        self.assertTrue(self.target.is_symlink())

    def test_wrong_project_and_invalid_encoding_preserved(self):
        project_rules.sync('p', self.work)
        previous = self.target.read_bytes()
        self.conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)',
                          ('q', str(self.root), db.now_iso()))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, '非本项目'):
            project_rules.sync('q', self.work)
        self.source.write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            project_rules.sync('p', self.work)
        self.assertEqual(self.target.read_bytes(), previous)

    def test_installed_hermes_loader(self):
        source = os.environ.get('HERMES_TEST_SOURCE')
        interpreter = os.environ.get('HERMES_TEST_PYTHON')
        if not source or not interpreter:
            self.skipTest('Set HERMES_TEST_SOURCE and HERMES_TEST_PYTHON for native loader integration')
        script = ('import json,sys; from agent.prompt_builder import build_context_files_prompt; '
                  'print(json.dumps(build_context_files_prompt(cwd=sys.argv[1], skip_soul=True)))')
        env = {**os.environ, 'HERMES_HOME': str(self.base / 'hermes-home')}
        for text in ('Main rules v1', 'Main edited outside engine v2'):
            self.source.write_text(text, encoding='utf-8')
            project_rules.sync('p', self.work)
            result = subprocess.check_output([interpreter, '-B', '-c', script, str(self.work)],
                                             cwd=source, env=env, text=True, encoding='utf-8')
            prompt = json.loads(result.strip().splitlines()[-1])
            self.assertIn('# Project Context', prompt)
            self.assertIn('## AGENTS.override.md', prompt)
            self.assertEqual(prompt.count(text), 1)
            if text.endswith('v2'):
                self.assertNotIn('Main rules v1', prompt)


if __name__ == '__main__':
    unittest.main()
