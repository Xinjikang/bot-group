"""Run the published v0.1.1 upgrader against newly built archives in isolation."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing

from scripts.test_upgrade import UpgradeTestBase
from engine.maintenance import make_release


REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = '098867cdef323f41b2d5f030900aff92aa6b9b52'
INSTALLER = '''import json, os, sys
from pathlib import Path
with Path(os.environ['INSTALL_AUDIT']).open('a', encoding='utf-8') as output:
    output.write(json.dumps([LAYOUT, *sys.argv[1:]]) + '\\n')
if LAYOUT == 'tools' and sys.argv[1] == os.environ.get('FAIL_INSTALL_COMMAND'):
    sys.exit(9)
'''
RUNNER = '''import os, socket, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
def deny_network(*args, **kwargs):
    raise AssertionError('legacy upgrade test forbids network access')
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
from scripts import upgrade
def stopped():
    sys.modules.pop('scripts.service', None)
    assert upgrade._admin_headers()['Authorization'].startswith('Bearer ')
    return 0
upgrade._stop_independent = stopped
upgrade._verify_migration = lambda backup: int(os.environ.get('FAIL_MIGRATION', '0'))
sys.exit(upgrade.main(['upgrade']))
'''


class LegacyReleaseUpgradeTests(UpgradeTestBase):
    def run_upgrade(self, failure=''):
        # Git supplies the exact released implementation; its upgrade functions stay intact.
        legacy_paths = ('scripts/upgrade.py', 'scripts/bootstrap.py', 'scripts/releases.py',
                        'scripts/service.py', 'engine/config.py', 'engine/auth.py')
        for relative in legacy_paths:
            result = subprocess.run(['git', 'show', f'{RELEASE_COMMIT}:{relative}'],
                                    cwd=REPOSITORY, capture_output=True, check=True)
            (self.prog / relative).write_bytes(result.stdout)
        (self.prog / 'scripts/install.py').write_text("LAYOUT = 'scripts'\n" + INSTALLER, encoding='utf-8')
        (self.prog / 'VERSION').write_text('v0.1.1', encoding='utf-8')
        (self.data / '.env').write_text('GROUP_ADMIN_PASSWORD=isolated-test\n', encoding='utf-8')
        self._seed_manifest(set(legacy_paths) | {'VERSION'})
        original = {p.relative_to(self.prog).as_posix(): p.read_bytes()
                    for p in self.prog.rglob('*') if p.is_file()}
        source = self.root / 'new-source'
        for relative in ('engine/config.py', 'engine/auth.py', 'tools/service.py', 'upgrade.bat',
                         'engine/maintenance/__init__.py', 'engine/maintenance/service.py'):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPOSITORY / relative).read_bytes())
        (source / 'tools/install.py').write_text("LAYOUT = 'tools'\n" + INSTALLER, encoding='utf-8')
        (source / 'engine/main.py').write_text('new', encoding='utf-8')
        assets = self.root / 'assets'
        self.assertEqual(make_release.make_release(source, assets, 'bot-group', 'v0.1.2'), 0)
        index = json.loads((assets / 'releases.json').read_text(encoding='utf-8'))
        for key in ('source', 'manifest'):
            index['releases'][0][key] = (assets / index['releases'][0][key]).as_uri()
        (assets / 'releases.json').write_text(json.dumps(index), encoding='utf-8')
        audit = self.root / 'installer-audit.jsonl'
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('GROUP_', 'XDG_GROUP_', 'UPGRADE_'))
               and key not in ('DATA_DIR', 'PORT', 'HOST', 'PYTHONPATH')}
        env.update(GROUP_DATA_DIR=str(self.data), INSTALL_AUDIT=str(audit),
                   RELEASE_INDEX_URL=(assets / 'releases.json').as_uri(),
                   FAIL_INSTALL_COMMAND=failure, FAIL_MIGRATION=str(int(failure == 'migration')),
                   PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-B', '-c', RUNNER], cwd=self.prog,
                                env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, int(bool(failure)), result.stdout + result.stderr)
        calls = [json.loads(line) for line in audit.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(calls[0], ['tools', 'install'])
        if failure:
            for relative, content in original.items():
                self.assertEqual((self.prog / relative).read_bytes(), content, relative)
            self.assertFalse((self.prog / 'tools/install.py').exists())
            self.assertEqual(calls[-2:], [['scripts', 'install', '--rebuild'], ['scripts', 'check']])
            evidence = next((self.data / 'backups').glob('upgrade-*/rollback_evidence.json'))
            self.assertIn('回滚完成', evidence.read_text(encoding='utf-8'))
        else:
            self.assertEqual((self.prog / 'VERSION').read_text(), 'v0.1.2')
            self.assertEqual(calls, [['tools', 'install'], ['tools', 'check']])
            self.assertIn(b'tools\\upgrade.py', (self.prog / 'upgrade.bat').read_bytes())
            manifest = json.loads((self.prog / 'release_manifest.json').read_text())
            self.assertIn('scripts/install.py', manifest['program_files'])
            self.assertIn('scripts/service.py', manifest['program_files'])
        self.assertEqual((self.data / 'attachments/a.txt').read_text(), 'att')
        self.assertEqual((self.data / '.env').read_text(), 'GROUP_ADMIN_PASSWORD=isolated-test\n')
        with closing(sqlite3.connect(self.data / 'group.db')) as conn:
            self.assertEqual(conn.execute('SELECT v FROM t').fetchall(), [('dbdata',)])

    def test_published_upgrader_reaches_tools_without_manual_migration(self):
        self.run_upgrade()

    def test_dependency_failure_restores_old_installation(self):
        self.run_upgrade('install')

    def test_migration_failure_restores_old_installation(self):
        self.run_upgrade('migration')

    def test_readiness_failure_restores_old_installation(self):
        self.run_upgrade('check')
