"""req#103 real isolated SQLite/HTTP, filesystem faults and readiness gates."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, db, project_initialization as init, project_permissions, queue, scanner, taskpool, wake
from scripts.test_project_readiness import snapshot
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields

EVIDENCE = {}


class ProcessCrash(BaseException):
    pass


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='project_init_')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'Blank'
        self.root.mkdir()
        env = patch.dict(os.environ, {'GROUP_DB': str(self.base / 'group.db'), 'HERMES_HOME': str(self.base / 'host')})
        env.start(); self.addCleanup(env.stop)
        self.conn = db.get_connection(); self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        app = FastAPI(); app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.app = app
        self.client = TestClient(app, client=('127.0.0.1', 4321)); self.addCleanup(self.client.close)

    def preview(self, **changes):
        return self.client.post('/projects/initializations/preview', json={
            'root': str(self.root), 'display_name': 'Blank <project>', **changes})

    def submit(self, preview, action='submit'):
        return self.client.post(f'/projects/initializations/{preview["initialization_id"]}/{action}',
                                json={'revision': preview['revision']})

    def test_empty_directory_preview_is_read_only_and_files_are_linked(self):
        plan = self.preview().json()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        self.assertTrue(result['files_ready']); self.assertFalse(result['team_configured'])
        self.assertEqual(result['uncompleted'], [])
        EVIDENCE['generated_files'] = [{'path': f['path'], 'sha256': f['sha256']} for f in result['files']]
        for file in result['files']:
            path = self.root / file['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), file['sha256'])
            for link in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
                self.assertTrue((path.parent / link).is_file(), link)
        for table in ['project_members', 'project_roles', 'project_permissions']:
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='owner_profile'").fetchone()[0], '')

    def test_new_directory_and_concurrent_repeated_id_are_idempotent(self):
        self.root.rmdir()
        plan = self.preview().json()
        self.assertFalse(self.root.exists())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.submit(plan).json(), range(2)))
        self.assertEqual(results[0], results[1])
        EVIDENCE['idempotent_concurrent_submit'] = {'same_result': results[0] == results[1], 'status': results[0]['status']}
        self.assertEqual(results[0]['status'], 'ready', results)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 1)
        self.assertEqual(self.client.get('/projects/initializations/' + plan['initialization_id']).json(), results[0])

    def test_nonempty_directory_and_post_preview_changes_protect_user_files(self):
        (self.root / 'code.txt').write_bytes(b'user')
        response = self.preview()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'import_required')
        (self.root / 'code.txt').unlink()
        plan = self.preview().json()
        (self.root / 'AGENTS.md').write_bytes(b'new user instructions')
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'file_conflict')
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), b'new user instructions')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)

    def test_new_unplanned_content_after_preview_is_rejected(self):
        plan = self.preview().json()
        (self.root / 'code.txt').write_bytes(b'keep')
        result = self.submit(plan).json()
        self.assertEqual(result['error']['code'], 'import_required')
        self.assertEqual(list(self.root.iterdir()), [self.root / 'code.txt'])

    def test_database_config_failure_rolls_back_all_config_and_retries(self):
        plan = self.preview().json()
        before = snapshot(self.conn)
        with patch.object(project_permissions, 'save_in_transaction', side_effect=sqlite3.OperationalError('injected transaction failure')):
            result = self.submit(plan).json()
        self.assertEqual(result['status'], 'failed')
        after = snapshot(self.conn)
        before.pop('project_initializations'); after.pop('project_initializations')
        self.assertEqual(before, after)
        EVIDENCE['configuration_rollback'] = {'all_configuration_tables_equal': before == after, 'target_files': len(list(self.root.iterdir()))}
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.submit(plan, 'retry').json()['status'], 'ready')

    def test_partial_publish_failure_reconciles_unlogged_file_without_rewrite(self):
        plan = self.preview().json()
        with patch.object(init, '_after_publish', side_effect=OSError('injected after publish')):
            failed = self.submit(plan).json()
        self.assertEqual(failed['phase'], 'publishing'); self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['uncompleted'], ['AGENTS.md', 'README.md', 'docs/INDEX.md'])
        agent = self.root / 'AGENTS.md'; stamp = agent.stat().st_mtime_ns
        self.assertEqual(self.conn.execute('SELECT readiness_status FROM projects').fetchone()[0], 'failed')
        result = self.submit(plan, 'retry').json()
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(agent.stat().st_mtime_ns, stamp)
        EVIDENCE['partial_publication_recovery'] = {'failure_phase': failed['phase'], 'failure_status': failed['status'],
            'uncompleted_before_retry': failed['uncompleted'], 'result': result['status'], 'first_file_rewritten': agent.stat().st_mtime_ns != stamp}

    def test_process_crash_after_write_before_journal_recovers_from_persisted_plan(self):
        plan = self.preview().json()
        def crash(_, relative):
            if relative == 'README.md':
                raise ProcessCrash()
        with patch.object(init, '_after_publish', side_effect=crash):
            with self.assertRaises(ProcessCrash):
                init.submit(plan['initialization_id'], {'revision': plan['revision']}, 'user')
        progress = init.progress(plan['initialization_id'])
        self.assertEqual(progress['status'], 'initializing')
        self.assertEqual(progress['uncompleted'], ['README.md', 'docs/INDEX.md'])
        recovered = self.submit(plan, 'retry').json()
        self.assertEqual(recovered['status'], 'ready')
        EVIDENCE['process_interruption_recovery'] = {'fault': 'BaseException after durable publication before journal',
            'before': {k: progress[k] for k in ('phase', 'status', 'uncompleted')}, 'after': recovered['status']}

    def test_manual_change_after_partial_publish_is_a_conflict_and_stays_gated(self):
        plan = self.preview().json()
        with patch.object(init, '_after_publish', side_effect=OSError('injected')):
            self.submit(plan)
        (self.root / 'AGENTS.md').write_bytes(b'edited by user')
        result = self.submit(plan, 'retry').json()
        self.assertEqual(result['error']['code'], 'file_conflict')
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), b'edited by user')
        self.assertEqual(self.conn.execute('SELECT readiness_status FROM projects').fetchone()[0], 'failed')

    def test_permissions_failure_and_changed_staging_are_explicit_and_preserved(self):
        plan = self.preview().json()
        with patch.object(init, '_publish', side_effect=PermissionError('permission denied')):
            result = self.submit(plan).json()
        self.assertEqual(result['error']['code'], 'filesystem_error')
        self.assertEqual(result['phase'], 'publishing')
        staged = self.root / ('.' + plan['initialization_id'] + '.AGENTS.md.tmp')
        staged.write_bytes(b'user edited staging')
        result = self.submit(plan, 'retry').json()
        self.assertEqual(result['error']['code'], 'file_conflict')
        self.assertEqual(staged.read_bytes(), b'user edited staging')
        self.assertFalse((self.root / 'AGENTS.md').exists())

    def test_half_written_staging_is_rebuilt_not_conflict(self):
        # 验收①③：暂存写一半（磁盘满/进程死）残留半份 .part，重试安全重建而非 file_conflict
        plan = self.preview().json()
        part = self.root / ('.' + plan['initialization_id'] + '.AGENTS.md.tmp.part')
        # 第一次：写 .part 中途 fsync 失败（模拟磁盘满），残留 .part
        with patch.object(init.os, 'fsync', side_effect=OSError('disk full mid-write')):
            failed = self.submit(plan).json()
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(part.exists())
        part.write_bytes(b'half-written staging')  # 精确模拟写一半的半份残留
        # 第二次 retry：识别 .part 残留为引擎未完成暂存，安全重建而非 file_conflict
        recovered = self.submit(plan, 'retry').json()
        self.assertEqual(recovered['status'], 'ready', recovered)
        self.assertFalse(part.exists())
        agents = next(f for f in recovered['files'] if f['path'] == 'AGENTS.md')
        self.assertEqual(hashlib.sha256((self.root / 'AGENTS.md').read_bytes()).hexdigest(), agents['sha256'])
        self.assertEqual(self.conn.execute('SELECT readiness_status FROM projects').fetchone()[0], 'ready')
        EVIDENCE['half_written_staging_recovery'] = {'first': failed['status'], 'recovered': recovered['status'],
                                                     'staging_cleaned': not part.exists()}

    def test_supplement_preserves_existing_rules_and_replace_backs_up(self):
        agent = self.root / 'AGENTS.md'; agent.write_bytes(b'# My workflow\r\n')
        project = taskpool.project_add('', str(self.root), display_name='Existing')['project']
        self.conn.execute('INSERT INTO project_roles(project,name,created_at) VALUES(?,?,?)', (project, 'retained', db.now_iso()))
        self.conn.commit()
        before = agent.read_bytes()
        plan = self.preview(mode='supplement', project=project).json()
        self.assertEqual(plan['files'][0]['action'], 'preserve')
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(agent.read_bytes(), before)
        plan = self.preview(mode='supplement', project=project, replace_agents=True).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual((self.root / plan['files'][0]['backup']).read_bytes(), before)
        self.assertNotEqual(agent.read_bytes(), before)
        self.assertEqual(self.conn.execute('SELECT name FROM project_roles').fetchone()[0], 'retained')

    def test_directory_replacement_junction_escape_and_traversal(self):
        plan = self.preview().json()
        other = self.base / 'Elsewhere'; other.mkdir()
        self.root.rmdir()
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.root), str(other)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.root.symlink_to(other, target_is_directory=True)
        try:
            failed = self.submit(plan).json()
            self.assertEqual(failed['error']['code'], 'unsafe_path')
            self.assertEqual(list(other.iterdir()), [])
        finally:
            if os.name == 'nt': os.rmdir(self.root)
            else: self.root.unlink()
        response = self.preview(root=str(self.base / 'x' / '..' / 'escape'))
        self.assertEqual(response.status_code, 409)

    def test_docs_junction_added_during_failure_is_blocked(self):
        plan = self.preview().json()
        with patch.object(init, '_after_publish', side_effect=OSError('partial')):
            self.submit(plan)
        other = self.base / 'Elsewhere'; other.mkdir()
        link = self.root / 'docs'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(other)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else: link.symlink_to(other, target_is_directory=True)
        try:
            self.assertEqual(self.submit(plan, 'retry').json()['error']['code'], 'unsafe_path')
            self.assertEqual(list(other.iterdir()), [])
        finally:
            if os.name == 'nt': os.rmdir(link)
            else: link.unlink()

    def test_worktree_git_validation_and_legacy_registration_remain(self):
        plan = self.preview(directory_mode='worktree').json()
        self.assertIn('detail', plan)
        subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                        'commit', '--allow-empty', '-m', 'Fixture'], check=True, capture_output=True)
        response = self.preview(directory_mode='worktree', worktree_root=str(self.base / 'trees'))
        self.assertEqual(response.status_code, 200, response.text)
        result = self.submit(response.json()).json()
        self.assertEqual(result['status'], 'ready', result)
        legacy = self.base / 'Legacy'; legacy.mkdir()
        response = self.client.post('/projects', json={'display_name': 'Legacy', 'root': str(legacy)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {'project', 'root'})
        self.assertEqual(list(legacy.iterdir()), [])

    def test_admin_only_version_confirmation_and_missing_record(self):
        plan = self.preview().json()
        self.assertEqual(self.client.post(f'/projects/initializations/{plan["initialization_id"]}/submit', json={'revision': 'stale'}).status_code, 409)
        self.assertEqual(self.client.get('/projects/initializations/missing').status_code, 404)
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('dev', 'bot')
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(self.submit(plan).status_code, 403)

    def test_recovered_plan_protects_new_manual_files_and_directory_configuration(self):
        plan = self.preview().json()
        with patch.object(init, '_after_publish', side_effect=OSError('partial')):
            self.submit(plan)
        (self.root / 'manual.txt').write_bytes(b'keep')
        result = self.submit(plan, 'retry').json()
        self.assertEqual(result['error']['code'], 'file_conflict')
        self.assertEqual((self.root / 'manual.txt').read_bytes(), b'keep')
        (self.root / 'manual.txt').unlink()
        self.conn.execute('UPDATE projects SET root=?', (str(self.base),)); self.conn.commit()
        self.assertEqual(self.submit(plan, 'retry').json()['error']['code'], 'configuration_changed')


class ReadinessGateTests(unittest.TestCase):
    def test_inbox_and_topic_scans_freeze_nonready_projects(self):
        from engine import router
        with isolated_database():
            conn = db.get_connection()
            try:
                old = db.now_iso(seconds_offset=-7200)
                tid = conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at,turn_profile,turn_wake_at,round) "
                    "VALUES('alpha','Topic','default','[\"dev\"]',?,?,'dev',?,1)", (old, old, old)).lastrowid
                for thread_id in (None, tid):
                    mid = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
                        "VALUES('alpha',?,'default','bot','@dev execute','[\"dev\"]',?,1)", (old, thread_id)).lastrowid
                    conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev','pending')", (mid,))
                conn.execute('UPDATE projects SET readiness_status="failed" WHERE name="alpha"'); conn.commit()
                before = snapshot(conn)
                with patch.object(wake, 'enqueue') as enqueue:
                    self.assertEqual(scanner.scan_inbox(conn), 0)
                    self.assertEqual(scanner.scan_thread_idle(conn), 0)
                    self.assertEqual(scanner.scan_thread_all_responded(conn), 0)
                    self.assertEqual(router.advance_timed_out_turns(conn), 0)
                    enqueue.assert_not_called()
                self.assertEqual(snapshot(conn), before)
                EVIDENCE['nonready_topic_scans'] = {'inbox_and_topics_unchanged': snapshot(conn) == before}
            finally: conn.close()

    def test_all_nonready_states_block_scans_enqueue_spawn_claim_and_creation(self):
        with isolated_database():
            fields = confirmed_requirement_fields('alpha')
            req = taskpool.req_create('alpha', 'default', title='Approved', **fields)['requirement_id']
            taskpool.task_create('alpha', 'architect', req, [dict(title='Task', next_bot='dev',
                description='Execute', design_basis='Approved', acceptance_criteria='Pass', prerequisites=[])])
            req2 = taskpool.req_create('alpha', 'default', title='Undecomposed', **fields)
            self.assertIn('requirement_id', req2)
            conn = db.get_connection()
            try:
                for state in ('pending', 'initializing', 'failed'):
                    conn.execute('UPDATE projects SET readiness_status=? WHERE name="alpha"', (state,)); conn.commit()
                    before = snapshot(conn)
                    with patch.object(wake, 'enqueue') as enqueue:
                        self.assertEqual(scanner.scan_tasks(conn)['claimable'], 0)
                        self.assertEqual(scanner.scan_requirements(conn), 0)
                        enqueue.assert_not_called()
                    self.assertFalse(wake.enqueue(conn, 'dev', 'work', 'test', project='alpha'))
                    with patch.object(wake, '_workdir_for') as workdir:
                        self.assertFalse(wake.deliver('dev', 'work', project='alpha')['ok'])
                        workdir.assert_not_called()
                    self.assertEqual(taskpool.task_claim('alpha', 'dev')['reason'], 'project_not_ready')
                    self.assertEqual(taskpool.req_create('alpha', 'default', title='New', **fields)['code'], 'project_not_ready')
                    self.assertEqual(taskpool.task_create('alpha', 'architect', req, [{'title': 'New'}])['code'], 'project_not_ready')
                    self.assertEqual(snapshot(conn), before)
                conn.execute('UPDATE projects SET readiness_status="ready" WHERE name="alpha"'); conn.commit()
                with patch.object(wake, 'enqueue', return_value=True):
                    self.assertGreater(scanner.scan_tasks(conn)['claimable'], 0)
                    self.assertGreater(scanner.scan_requirements(conn), 0)
                self.assertTrue(taskpool.task_claim('alpha', 'dev')['claimed'])
            finally: conn.close()

    def test_preexisting_queue_waits_and_atomic_claim_rechecks_readiness(self):
        with isolated_database():
            conn = db.get_connection()
            try:
                self.assertTrue(wake.enqueue(conn, 'dev', 'work', 'manual', project='alpha'))
                conn.execute('UPDATE projects SET readiness_status="failed" WHERE name="alpha"'); conn.commit()
                with patch.object(wake, 'deliver') as deliver:
                    self.assertFalse(queue.process_one(conn)); deliver.assert_not_called()
                self.assertEqual(conn.execute('SELECT status FROM wake_jobs').fetchone()[0], 'queued')
                conn.execute('UPDATE projects SET readiness_status="ready" WHERE name="alpha"'); conn.commit()
                def change(*_):
                    other = db.get_connection()
                    with other: other.execute('UPDATE projects SET readiness_status="initializing" WHERE name="alpha"')
                    other.close(); return False
                with patch.object(wake, 'session_busy', side_effect=change), patch.object(wake, 'deliver') as deliver:
                    self.assertFalse(queue.process_one(conn)); deliver.assert_not_called()
                self.assertEqual(conn.execute('SELECT status FROM wake_jobs').fetchone()[0], 'queued')
                conn.execute('UPDATE projects SET readiness_status="ready" WHERE name="alpha"'); conn.commit()
                with patch.object(wake, 'session_busy', return_value=False), patch.object(wake, 'deliver', return_value={'ok': True}) as deliver:
                    self.assertTrue(queue.process_one(conn)); deliver.assert_called_once()
            finally: conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence')
    options, remaining = parser.parse_known_args()
    if not options.evidence:
        unittest.main(argv=[sys.argv[0], *remaining])
    else:
        class EvidenceResult(unittest.TextTestResult):
            def addSuccess(self, test):
                super().addSuccess(test)
                EVIDENCE.setdefault('passed_cases', []).append(test.id())
        result = unittest.TextTestRunner(resultclass=EvidenceResult, verbosity=1).run(
            unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
        evidence = {'executed_at': db.now_iso(), 'isolated_database': True, 'temporary_directories': True,
                    'real_bot_spawn': False, 'platform': sys.platform, 'tests_run': result.testsRun,
                    'successful': result.wasSuccessful(), 'skipped': len(result.skipped), **EVIDENCE}
        Path(options.evidence).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        sys.exit(0 if result.wasSuccessful() else 1)
