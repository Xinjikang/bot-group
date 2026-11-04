"""Native execution recovery, profile identity and assistant protocol regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from engine import dsh_hud, hud_runtime, native_runtime, wake
from scripts import test_dsh_lifecycle, test_runtime_fork


class RecoveryTests(unittest.TestCase):
    setUp = test_dsh_lifecycle.DshStopLocateTests.setUp
    tearDown = test_dsh_lifecycle.DshStopLocateTests.tearDown
    def residual(self, runtime='dsh'):
        session = wake.session_name('dsh-bot', 'work', None, 'bot-group')
        self.conn.execute('INSERT INTO wake_runs(session,started_at,ended_at,exit_code,pid,project,profile,runtime) '
                          'VALUES(?,100,200,-1,NULL,?,?,?)', (session, 'bot-group', 'dsh-bot', runtime))
        self.conn.commit()
        return session

    def test_missing_pid_absence_releases_session_without_deleting_history(self):
        self.residual()
        with patch.object(wake.process_query, 'list_processes', return_value=[]):
            self.assertFalse(wake.session_busy('dsh-bot', 'work', None, 'bot-group'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM wake_runs').fetchone()[0], 1)

    def test_missing_pid_unknown_keeps_protection(self):
        self.residual()
        with patch.object(wake.process_query, 'list_processes', return_value=None):
            self.assertTrue(wake.session_busy('dsh-bot', 'work', None, 'bot-group'))

    def test_missing_pid_finds_named_profile_and_ignores_foreign_execution(self):
        session = self.residual()
        tag = wake._dsh_spawn_tag(session, 100.0)
        argv = ['dsh', '--profile', 'reviewer', '--patch', f'/tmp/{tag}-abc/overlay.yml', '--json', '-']
        foreign = ['dsh', '--profile', 'reviewer', '--patch', '/tmp/foreign/overlay.yml', '--json', '-']
        with patch.object(wake.process_query, 'list_processes', return_value=[(42, argv), (43, foreign)]):
            self.assertEqual(wake.find_bot_processes('dsh-bot', session), [42])


class ProfileAndHudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / 'profiles' / 'reviewer'
        self.profile.mkdir(parents=True)
        self.entry = self.root / 'dsh.cmd'
        self.entry.touch()
        self.bot = dict(runtime='dsh', runtime_entry=str(self.entry),
                        config_root=str(self.root), profile='reviewer')
        (self.profile / 'cordis.patch.yml').write_text(
            '- id: agent-default-model\n  config: {model: own-model, provider: own-provider}\n'
            '- id: system-prompt\n  config: {personaPrefix: own-persona}\n'
            '- insert:\n  - {id: own-plugin, name: own-package}\n', encoding='utf-8')

    def test_named_profile_is_native_and_inserts_are_loaded_once(self):
        command, home = native_runtime.dsh(self.bot, overlay_path='overlay.yml')
        self.assertEqual(command[2], 'reviewer')
        self.assertEqual(home, str(self.root))
        with patch('engine.dsh_auth.overlay_entries', return_value=[]):
            entries = native_runtime.dsh_overlay(self.bot, project_overrides={'model': 'project-model'})
        self.assertFalse(any('insert' in e for e in entries))
        self.assertEqual(entries[0]['config'], {'model': 'project-model', 'provider': 'own-provider'})

    def test_desktop_background_adapter_preserves_profile_patch(self):
        self.bot['profile'] = 'desktop'
        self.profile.rename(self.profile.with_name('desktop'))
        self.assertEqual(native_runtime.dsh(self.bot)[0][2], 'headless')
        with patch('engine.dsh_auth.overlay_entries', return_value=[]):
            entries = native_runtime.dsh_overlay(self.bot)
        self.assertTrue(any('insert' in e for e in entries))
        self.assertEqual(entries[1]['config']['personaPrefix'], 'own-persona')

    def run_hud(self, output, *, tools=True, auto=True, tool_names=None):
        def run(command, prompt, env, cwd, *, stderr_line):
            self.assertEqual(command[2], 'reviewer')
            self.assertNotIn('--session-id', command)
            self.assertEqual(env['DSH_HOME'], str(self.root))
            overlay = Path(command[command.index('--patch')+1]).read_text(encoding='utf-8')
            self.assertNotIn('personaPrefix', overlay)
            self.assertIn('hud-tool-scope', overlay)
            self.assertIn('hud-project-config', overlay)
            if tools:
                stderr_line(dsh_hud.TOOL_MARKER + json.dumps(
                    sorted(dsh_hud.REQUIRED_TOOLS) if tool_names is None else tool_names))
            if auto:
                stderr_line('[auto-enable] state preset=auto preexec=0 review=0 entry=0 blocked=0')
            return output
        server = {'command': 'fixture-python', 'args': ['hud_server.py'], 'env': {'FIXTURE': 'yes'}}
        with patch('engine.dsh_auth.copy_credentials', return_value=None), \
             patch('engine.dsh_auth.overlay_entries', return_value=[]), \
             patch('engine.dsh_auto.plugin_available', return_value=True), \
             patch.object(hud_runtime, '_run', side_effect=run), \
             hud_runtime.tool_server(server):
            return hud_runtime.respond(self.bot, {'project': {'root': str(self.root)}, 'user_request': 'check'})

    def test_hud_parses_final_and_cleans_private_overlay(self):
        result = self.run_hud(json.dumps({'type': 'final', 'text': '{"text":"done"}'}))
        self.assertEqual(result['text'], 'done')

    def test_hud_rejects_corrupt_missing_final_and_error_events(self):
        for output in ('bad json', '{"type":"session","sessionId":"one"}',
                       '{"type":"error"}', '[]'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                self.run_hud(output)

    def test_hud_requires_tool_scope_and_auto_evidence(self):
        for state in ({'tools': False}, {'auto': False}):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                self.run_hud('{"type":"final","text":"done"}', **state)

    def test_hud_rejects_incomplete_extra_duplicate_and_malformed_tool_evidence(self):
        required = sorted(dsh_hud.REQUIRED_TOOLS)
        for names in (required[:-1], required + ['pwsh'], required[:-1] + [required[0]],
                      'ready', [None] * len(required)):
            with self.subTest(names=names), self.assertRaises(RuntimeError):
                self.run_hud('{"type":"final","text":"done"}', tool_names=names)

    def test_hud_sanitizes_environment_without_mutating_parent(self):
        env = {'DEEPSEEK_API_KEY': 'foreign', 'DSH_PROFILE': 'foreign',
               'DSH_PERMISSION_MODE': 'danger-full-access', 'KEEP': 'yes'}
        original = dict(env)
        def run(command, prompt, child_env, cwd, *, stderr_line):
            self.assertNotIn('DEEPSEEK_API_KEY', child_env)
            self.assertNotIn('DSH_PROFILE', child_env)
            self.assertEqual(child_env['DSH_HOME'], str(self.root))
            self.assertEqual(child_env['DSH_PERMISSION_MODE'], 'workspace-write')
            self.assertEqual(child_env['DSH_TOOLS_MODE'], 'native')
            self.assertEqual(child_env['KEEP'], 'yes')
            stderr_line(dsh_hud.TOOL_MARKER + json.dumps(sorted(dsh_hud.REQUIRED_TOOLS)))
            return '{"type":"final","text":"done"}'
        with patch('engine.dsh_auth.copy_credentials', return_value=None), \
             patch('engine.dsh_auth.overlay_entries', return_value=[]), \
             patch('engine.dsh_auto.plugin_available', return_value=False):
            dsh_hud.respond(self.bot, 'prompt', 'system', self.root, str(self.root), None, run, env)
        self.assertEqual(env, original)


class SpawnIoTests(unittest.TestCase):
    setUp = test_runtime_fork.RuntimeForkTests.setUp
    tearDown = test_runtime_fork.RuntimeForkTests.tearDown
    _register_bot = test_runtime_fork.RuntimeForkTests._register_bot

    def test_pid_and_readers_precede_background_input(self):
        bot_id = self._register_bot(host_type='dsh')
        self.conn.execute('UPDATE bots SET config_root=? WHERE id=?', (self.tmp, bot_id))
        self.conn.commit()
        self.addCleanup(wake._running_wakes.clear)
        started = []
        proc = MagicMock(pid=4242, returncode=0)
        proc.stdout = iter([])
        proc.stderr = iter([])
        def write(prompt):
            self.assertIn('dsh-default', started)
            self.assertIn('dsh-err-default', started)
            self.assertIn('dsh-input-default', started)
            self.assertGreater(len(prompt), 65536)
            self.assertEqual(self.conn.execute('SELECT pid FROM wake_runs').fetchone()[0], 4242)
        proc.stdin.write.side_effect = write
        def thread(*, target, name, **kwargs):
            obj = MagicMock()
            def start():
                started.append(name)
                if name.startswith('dsh'):
                    target()
            obj.start.side_effect = start
            return obj
        def popen(*args, **kwargs):
            return proc
        with patch.object(wake.subprocess, 'Popen', side_effect=popen), \
             patch.object(wake, 'session_busy', return_value=False), \
             patch('engine.dsh_auth.copy_credentials', return_value=None), \
             patch('engine.dsh_auto.plugin_available', return_value=False), \
             patch('engine.native_runtime.dsh_overlay', return_value=[]), \
             patch('engine.native_runtime.dsh', return_value=(['fixture'], self.tmp)), \
             patch('engine.native_runtime.dsh_version', return_value=''), \
             patch('tempfile.TemporaryFile', return_value=MagicMock()), \
             patch('threading.Thread', side_effect=thread):
            result = wake._spawn('default', 'work', 'x' * 200000, None, 'bot-group')
        self.assertTrue(result['ok'], result)
        proc.stdin.write.assert_called_once()


if __name__ == '__main__':
    unittest.main()
