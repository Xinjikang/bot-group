"""#215：真实发布构建/file URL 下载/校验/备份/文件落地/失败恢复；禁止真实 socket。"""
import hashlib
import json
import os
from pathlib import Path
from unittest import mock
import unittest
import zipfile
from contextlib import closing
from urllib.parse import urlsplit
from urllib.request import url2pathname

from scripts import make_release, upgrade, releases
from scripts.test_upgrade import UpgradeTestBase


class ReleaseUpgradeTests(UpgradeTestBase):
    def setUp(self):
        super().setUp()
        (self.prog / "VERSION").write_text("v1.0.0", encoding="utf-8")
        self._seed_manifest({"VERSION"})
        self.index = self.root / "releases.json"
        self.entry = self.build("v2.0.0")
        self.publish(self.entry)

    def build(self, version):
        src = self.root / ("src-" + version)
        (src / "engine").mkdir(parents=True)
        (src / "scripts").mkdir()
        (src / "engine" / "main.py").write_text(version, encoding="utf-8")
        (src / "scripts" / "install.py").write_text("pass\n", encoding="utf-8")
        (src / "requirements.lock").write_text("fastapi==2\n", encoding="utf-8")
        out = self.root / ("assets-" + version)
        make_release.make_release(src, out, "bot-group", version)
        entry = json.loads((out / "releases.json").read_text(encoding="utf-8"))["releases"][0]
        for key in ("source", "manifest"):
            entry[key] = (out / entry[key]).as_uri()
        return entry

    def publish(self, *entries):
        self.index.write_text(json.dumps({"releases": entries}), encoding="utf-8")

    def execute(self):
        return upgrade.main(["upgrade", "--index-url", self.index.as_uri()])

    def test_real_discovery_download_landing_and_pinned_audit(self):
        # 仅模拟进程停机、pip 与迁移/就绪子进程；文件下载、校验、备份、落地均真实。
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
             mock.patch.object(upgrade, "_sync_deps", return_value=0), \
             mock.patch.object(upgrade, "_verify_migration", return_value=0), \
             mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(upgrade.current_version(), "v2.0.0")
        manifest = upgrade._current_release_manifest()
        self.assertIn("VERSION", manifest)
        audit = json.loads((upgrade.latest_backup() / "upgrade_target.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["current_version"], "v1.0.0")
        self.assertEqual(audit["target"], {k: self.entry[k] for k in
                         ("version", "source", "sha256", "manifest", "manifest_sha256")})

    def test_query_once_pins_version_when_index_changes_during_stop(self):
        def stop():
            self.publish(dict(self.entry, version="v9.0.0"))
            return 0
        with mock.patch.object(upgrade, "_stop_independent", side_effect=stop), \
             mock.patch.object(upgrade, "_sync_deps", return_value=0), \
             mock.patch.object(upgrade, "_verify_migration", return_value=0), \
             mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(upgrade.current_version(), "v2.0.0")

    def _check_custom_data_upgrade(self, nested, fail):
        import shutil
        import sqlite3
        self._dp.stop()
        self._bp.stop()
        self._hp.stop()
        self._pt.stop()
        if nested:
            target = self.prog / 'custom data'
            shutil.copytree(self.data, target)
            self.data = target
        (self.data / '.env').write_text('GROUP_ENGINE_PORT=19321\n', encoding='utf-8')
        (self.data / 'install.json').write_text('{"fixture":true}', encoding='utf-8')
        preserved = {name: (self.data / name).read_bytes()
                     for name in ('.env', 'install.json', 'attachments/a.txt')}
        with mock.patch.dict(os.environ, {'GROUP_DATA_DIR': str(self.data), 'DATA_DIR': '',
                                         'GROUP_ENGINE_PORT': '', 'PORT': ''}), \
             mock.patch.object(upgrade.config, '_cli', {'host': None, 'port': None, 'data_dir': None}), \
             mock.patch.object(upgrade.config, '_dotenv_cache', {}), \
             mock.patch.object(upgrade, '_stop_independent', return_value=0), \
             mock.patch.object(upgrade, '_sync_deps', return_value=int(fail)), \
             mock.patch.object(upgrade, '_verify_migration', return_value=0), \
             mock.patch.object(upgrade, '_verify_ready', return_value=0), \
             mock.patch.object(upgrade.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            self.assertEqual(upgrade.config.data_dir(), self.data)
            self.assertEqual(upgrade.config.port(), 19321)
            self.assertEqual(self.execute(), int(fail))
            self.assertEqual(upgrade.current_version(), 'v1.0.0' if fail else 'v2.0.0')
            backup = upgrade.latest_backup()
            self.assertEqual(backup.parent, self.data / 'backups')
            self.assertTrue((backup / 'data/group.db').exists())
            self.assertEqual((backup / 'data/.env').read_bytes(), preserved['.env'])
            self.assertEqual(upgrade.config.data_dir(), self.data)
            self.assertEqual(upgrade.config.port(), 19321)
        for name, content in preserved.items():
            self.assertEqual((self.data / name).read_bytes(), content)
        with closing(sqlite3.connect(self.data / 'group.db')) as conn:
            self.assertEqual(conn.execute('SELECT v FROM t').fetchall(), [('dbdata',)])
        self.assertFalse((self.prog / 'group.db').exists())

    def test_external_custom_data_survives_upgrade(self):
        self._check_custom_data_upgrade(nested=False, fail=False)

    def test_nested_custom_data_survives_upgrade(self):
        self._check_custom_data_upgrade(nested=True, fail=False)

    def test_external_custom_data_survives_rollback(self):
        self._check_custom_data_upgrade(nested=False, fail=True)

    def test_nested_custom_data_survives_rollback(self):
        self._check_custom_data_upgrade(nested=True, fail=True)

    def test_dependency_subprocess_inherits_custom_data_directory(self):
        import shutil
        from engine import config
        shutil.copyfile(config.__file__, self.prog / 'engine/config.py')
        (self.prog / 'engine/__init__.py').touch()
        (self.prog / 'scripts/install.py').write_text(
            'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n'
            'from engine import config\n'
            '(config.data_dir() / "child-directory.txt").write_text(str(config.data_dir()), encoding="utf-8")\n',
            encoding='utf-8')
        with mock.patch.dict(os.environ, {'GROUP_DATA_DIR': str(self.data)}):
            self.assertEqual(upgrade._sync_deps(), 0)
        self.assertEqual((self.data / 'child-directory.txt').read_text(encoding='utf-8'), str(self.data))

    def test_no_update_or_older_release_keeps_running_instance(self):
        for current in ("v2.0.0", "2.0.0", "v3.0.0"):
            with self.subTest(current=current):
                (self.prog / "VERSION").write_text(current, encoding="utf-8")
                with mock.patch.object(upgrade, "_safe_stop") as stop:
                    self.assertEqual(self.execute(), 0)
                    stop.assert_not_called()
                self.assertEqual(upgrade.current_version(), current)

    def test_missing_assets_query_and_corruption_fail_before_stop(self):
        cases = [dict(self.entry, source=(self.root / "missing.zip").as_uri()),
                 dict(self.entry, sha256="0" * 64),
                 dict(self.entry, manifest_sha256="0" * 64),
                 dict(self.entry, manifest=""), dict(self.entry, sha256="bad")]
        for entry in cases:
            with self.subTest(entry=entry):
                self.publish(entry)
                with mock.patch.object(upgrade, "_safe_stop") as stop:
                    self.assertEqual(self.execute(), 1)
                    stop.assert_not_called()
                self.assertEqual(upgrade.current_version(), "v1.0.0")
                self.assertEqual((self.prog / "engine" / "main.py").read_text(), "old")
        self.index.write_text("invalid JSON", encoding="utf-8")
        with mock.patch.object(upgrade, "_safe_stop") as stop:
            self.assertEqual(self.execute(), 1)
            stop.assert_not_called()

    def test_embedded_manifest_must_match_fixed_asset(self):
        path = Path(url2pathname(urlsplit(self.entry["manifest"]).path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["program_files"].append("extra.py")
        raw = json.dumps(payload).encode()
        path.write_bytes(raw)
        self.publish(dict(self.entry, manifest_sha256=hashlib.sha256(raw).hexdigest()))
        with mock.patch.object(upgrade, "_safe_stop") as stop:
            self.assertEqual(self.execute(), 1)
            stop.assert_not_called()

    def test_dependency_failure_restores_matching_version_and_data(self):
        original = (self.data / "group.db").read_bytes()
        (self.prog / "engine" / "user_notes.txt").write_text("USER", encoding="utf-8")
        with mock.patch.object(upgrade, "_stop_independent", return_value=0) as stop, \
             mock.patch.object(upgrade, "_sync_deps", return_value=1), \
             mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(upgrade.current_version(), "v1.0.0")
        self.assertEqual((self.prog / "engine" / "main.py").read_text(), "old")
        self.assertEqual((self.prog / "engine" / "user_notes.txt").read_text(), "USER")
        # SQLite backup API 按页生成独立库；核对业务内容而非文件字节。
        import sqlite3
        with closing(sqlite3.connect(self.data / "group.db")) as conn:
            self.assertEqual(conn.execute("select v from t").fetchone()[0], "dbdata")
        self.assertTrue(original)

    def test_index_uses_data_dotenv_and_cli_priority(self):
        (self.data / ".env").write_text(f'RELEASE_INDEX_URL="{self.index.as_uri()}"\n', encoding="utf-8")
        upgrade.config._dotenv_cache.clear()
        with mock.patch.dict(os.environ, {"RELEASE_INDEX_URL": ""}):
            self.assertEqual(upgrade.discover_release().version, "v2.0.0")
        with mock.patch.dict(os.environ, {"RELEASE_INDEX_URL": "file:///missing"}):
            self.assertEqual(upgrade.discover_release(self.index.as_uri()).version, "v2.0.0")

    def test_drafts_and_prereleases_excluded(self):
        self.publish(self.entry, dict(self.entry, version="v4.0.0", draft=True),
                     dict(self.entry, version="v5.0.0", prerelease=True))
        self.assertEqual(upgrade.discover_release(self.index.as_uri()).version, "v2.0.0")

    def test_process_liveness_uses_native_handle_without_tasklist(self):
        # 实际读取当前进程句柄，任何 tasklist 子进程都会触发失败。
        with mock.patch.object(upgrade.subprocess, 'run', side_effect=AssertionError('禁止 tasklist')):
            self.assertTrue(upgrade._pid_alive(os.getpid()))

    def test_stop_client_rejects_foreign_binding_before_post(self):
        from scripts import stop_engine
        from contextlib import nullcontext
        import io
        other = {'graceful_shutdown': 1, 'instance_id': 'other', 'pid': 123,
                 'program_root': str(self.root / 'foreign'), 'db_path': str(self.data / 'group.db')}
        opener = mock.Mock()
        opener.open.return_value = nullcontext(io.StringIO(json.dumps(other)))
        with mock.patch.object(stop_engine.urllib.request, 'build_opener', return_value=opener), \
             mock.patch.object(stop_engine.auth, '_password', return_value=''):
            with self.assertRaisesRegex(RuntimeError, 'program_root'):
                stop_engine.stop('http://127.0.0.1:18042')
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.args[0].method, 'GET')

    def test_source_archive_keeps_public_docs_and_template_resources_only(self):
        root = self.root / 'publication-source'
        public = {
            'engine/main.py': 'pass\n',
            'doc/INDEX.md': '# Existing documentation directory\n',
            'docs/engineering/ENGINEERING.md': '# Technical documentation\n',
            'docs/requirements/req-1.md': '# Versioned requirement\n',
            'docs/design/decision.md': '# Versioned design\n',
            'templates/projects/software/files/reports/gitignore.tmpl': '*\n',
            'templates/projects/algorithm/manifest.json': '{}\n',
            'templates/projects/testing/manifest.json': '{}\n',
            'templates/projects/testing/files/docs/product/PRD.md.tmpl': '# PRD reference\n',
            'templates/projects/testing/files/scripts/TEST_INDEX.md.tmpl': '# Case script mapping\n',
            'templates/projects/testing/files/reports/REPORT.md.tmpl': '# Test report\n',
            'templates/projects/testing/references/souls/tester.md': '# Tester reference\n',
            'templates/projects/algorithm/files/docs/design/ALGORITHM.md.tmpl': '# Algorithm plan\n',
            'templates/projects/algorithm/files/reports/REPORT.md.tmpl': '# Research report\n',
            'templates/projects/algorithm/files/reports/experiments/EXPERIMENT.md.tmpl': '# Experiment record\n',
            'templates/projects/product-prototype/manifest.json': '{}\n',
            'templates/projects/product-prototype/files/outputs/PRD.md.tmpl': '# PRD skeleton\n',
            'templates/projects/product-prototype/files/ui-spec/UI-SPEC.md.tmpl': '# Default UI baseline\n',
            'templates/projects/product-prototype/files/materials/references/README.md.tmpl': '# References\n',
        }
        private = {
            'frontend/index.html.bak': 'INTERNAL_ONLY',
            'engine/main.py.BAK': 'INTERNAL_ONLY',
            'scripts/tool.py.bak_20261020': 'INTERNAL_ONLY',
            'frontend/unfinished.tmp': 'INTERNAL_ONLY',
            'engine/patch.py.orig': 'INTERNAL_ONLY',
            'engine/patch.py.rej': 'INTERNAL_ONLY',
            'engine/editor.py~': 'INTERNAL_ONLY',
            'internal/requirements/req-1.md': 'INTERNAL_ONLY',
            'internal/reports/result.json': 'INTERNAL_ONLY',
            'reports/tests/v1/task/REPORT.md': 'INTERNAL_ONLY',
            'reports/tests/v1/task/evidence/result.json': 'INTERNAL_ONLY',
            'reports/research/selection/REPORT.md': 'INTERNAL_ONLY',
            'frontend/.hermes-tmp.fixture': 'INTERNAL_ONLY',
            'engine/.hermes-tmp.fixture/prompt.txt': 'INTERNAL_ONLY',
            'scripts/.private-note': 'INTERNAL_ONLY',
        }
        for relative, content in {**public, **private}.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        out = self.root / 'publication-assets'
        self.assertEqual(make_release.make_release(root, out, 'bot-group', 'v1.2.3'), 0)
        manifest = json.loads((out / 'release_manifest.json').read_text())
        self.assertEqual(set(manifest['program_files']), set(public) | {'VERSION'})
        with zipfile.ZipFile(out / 'bot-group-v1.2.3.zip') as archive:
            self.assertEqual(set(archive.namelist()), set(public) | {'VERSION', 'release_manifest.json'})
            for name in archive.namelist():
                self.assertNotIn(b'INTERNAL_ONLY', archive.read(name))

    def test_formal_builder_rejects_unknown_version(self):
        with self.assertRaises(SystemExit) as raised:
            make_release.main(['--formal', '--version', 'dev'])
        self.assertEqual(raised.exception.code, 2)

    def test_formal_builder_exports_standalone_bootstrap_and_checksums(self):
        root = self.root / 'formal-source'
        (root / 'scripts').mkdir(parents=True)
        (root / 'scripts/bootstrap.py').write_text('print("standalone")\n')
        out = self.root / 'formal-assets'
        with mock.patch.object(make_release, '__file__', str(root / 'scripts/make_release.py')):
            self.assertEqual(make_release.main(['--formal', '--version', 'v1.2.3', '--out', str(out)]), 0)
        raw = (out / 'bootstrap.py').read_bytes()
        self.assertIn(hashlib.sha256(raw).hexdigest() + '  bootstrap.py',
                      (out / 'SHA256SUMS').read_text())
        index = releases.fetch_index((out / 'releases.json').as_uri())
        target = releases.resolve(releases.pick_formal(index))
        self.assertEqual(releases.checked_manifest(target)['version'], 'v1.2.3')


if __name__ == "__main__":
    unittest.main()
