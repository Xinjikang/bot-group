"""Native settings and launch identity shared by group Bots and project assistants."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import native_runtime


class NativeRuntimeTests(unittest.TestCase):
    def test_selected_codex_settings_remain_authoritative(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            original = 'approval_policy="on-request"\n[features]\nmemories=false\n[memories]\nuse_memories=false\n[mcp_servers.task_queue]\ncommand="old-group-server"\n[mcp_servers.personal_memory]\ncommand="user-memory-server"\n'
            (home/'config.toml').write_text(original, encoding='utf-8')
            bot = {'config_root': str(home), 'profile': 'default'}
            with patch('engine.wake._codex_bin', return_value='native-codex'), patch('engine.codex_auth.sync') as sync:
                command, selected = native_runtime.codex(bot, excluded_mcp=('task_queue',), system_prompt='role')
            self.assertEqual(selected, str(home))
            sync.assert_called_once_with(str(home))
            self.assertIn('mcp_servers.task_queue.enabled=false', command)
            self.assertFalse(any(arg.startswith(('features.', 'memories.', 'approval_policy=', 'default_permissions=', 'mcp_servers.personal_memory')) for arg in command))
            self.assertNotIn('--sandbox', command)
            self.assertNotIn('--ephemeral', command)
            self.assertEqual((home/'config.toml').read_text(encoding='utf-8'), original)

    def test_absent_collaboration_server_does_not_create_invalid_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = {'config_root': folder, 'profile': 'default'}
            with patch('engine.wake._codex_bin', return_value='native-codex'), patch('engine.codex_auth.sync'):
                command, _ = native_runtime.codex(bot, excluded_mcp=('task_queue',))
            self.assertFalse(any('mcp_servers.task_queue' in arg for arg in command))

    def test_native_environment_preserves_settings_and_clears_execution_credentials(self):
        with patch.dict(os.environ, {'HERMES_MANAGED_DIR': 'managed-settings', 'CODEX_HOME': 'selected-home',
                'HERMES_EPHEMERAL_SYSTEM_PROMPT': 'old-role', 'HERMES_PROFILE': 'old-member',
                'GROUP_DB': 'engine-only', 'XDG_GROUP_ENGINE_TOKEN': 'expired-token', 'PYTHONHOME': 'engine-python'}, clear=True):
            env = native_runtime.environment()
        self.assertEqual(env['HERMES_MANAGED_DIR'], 'managed-settings')
        self.assertEqual(env['CODEX_HOME'], 'selected-home')
        for name in ('HERMES_EPHEMERAL_SYSTEM_PROMPT', 'HERMES_PROFILE', 'GROUP_DB', 'XDG_GROUP_ENGINE_TOKEN', 'PYTHONHOME'):
            self.assertNotIn(name, env)


if __name__ == '__main__':
    unittest.main()
