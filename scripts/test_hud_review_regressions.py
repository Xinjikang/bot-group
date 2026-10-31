"""API/real-frontend regressions for ordered updates, atomic writes and legacy-note isolation."""
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import unittest
from unittest.mock import patch

from engine import api, db, hud_configuration, hud_tools, project_hud as hud
from scripts import test_hud_native_tools as fixtures


class ReviewRegressions(unittest.TestCase):
    setUp = fixtures.NativeToolTests.setUp
    join = fixtures.NativeToolTests.join
    session = fixtures.NativeToolTests.session
    infer = fixtures.NativeToolTests.infer
    running = fixtures.NativeToolTests.running
    settings = fixtures.NativeToolTests.settings
    request = fixtures.NativeToolTests.request

    def record(self, label, value):
        folder = os.environ.get('HUD_REVIEW_EVIDENCE')
        if not folder:
            return
        target = Path(folder)
        if not target.is_absolute() or not target.is_dir():
            raise ValueError('HUD_REVIEW_EVIDENCE must be an existing absolute directory')
        index = getattr(self, '_trace_index', 0) + 1
        self._trace_index = index
        (target/f'{self._testMethodName}-{index}-{label}.json').write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def frontend(self, snapshots, **options):
        executable = shutil.which('node')
        self.assertIsNotNone(executable, 'Node.js is required for shipped frontend regression coverage')
        result = subprocess.run([executable, str(Path(__file__).with_name('verify_hud_config_sync.cjs'))],
            input=json.dumps({'snapshots': snapshots, **options}), text=True, encoding='utf-8',
            capture_output=True, check=True, timeout=20)
        actual = json.loads(result.stdout)
        self.record('frontend', {'updates': [s['state'].get('config_update') for s in snapshots], 'actual': actual})
        return actual

    def test_consecutive_wizard_updates_survive_missed_and_incremental_polls(self):
        item = self.running(None)
        bot_id = item['bot_id']
        created = self.settings(item, 'roles', 'create', {'name': 'developer'})
        self.assertEqual(created.status_code, 200, created.text)
        first = hud.view(item['id'])
        assigned = self.settings(item, 'members', 'assign_role', {'bot_id': bot_id, 'role': 'developer'})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        second = hud.view(item['id'])
        self.assertEqual(len(second['state']['config_update']['steps']), 2)
        for snapshots in ([second], [first, second], [second, second]):
            with self.subTest(polls=len(snapshots)):
                actual = self.frontend(snapshots)
                self.assertEqual([r['name'] for r in actual['roles']], ['developer'])
                self.assertEqual(actual['members'][0]['bot_id'], bot_id)
                self.assertEqual(actual['members'][0]['role'], 'developer')
                self.assertEqual(actual['applied'], 2)
                saved = hud.update(item['id'], {'revision': hud.view(item['id'])['revision'],
                    'draft': {'wizard': {'roles': actual['roles'], 'members': actual['members']}}})
                self.assertEqual(saved['state']['draft']['wizard']['roles'][0]['name'], 'developer')
        manual = {'name': 'manual-local', 'permissions': []}
        actual = self.frontend([first, second], edits=[{'after': 0, 'roles': [*first['state']['draft']['wizard']['roles'], manual]}])
        self.assertEqual([r['name'] for r in actual['roles']], ['developer', 'manual-local'])

    def test_wizard_member_comparison_uses_json_values_across_property_orders(self):
        item = self.running(None)
        roles = [{'name': 'developer', 'permissions': []}, {'name': 'reviewer', 'permissions': []}]
        members = [{'bot_id': item['bot_id'], 'role': 'developer', 'permissions': []}]
        item = hud.update(item['id'], {'revision': item['revision'], 'draft': {'wizard': {'roles': roles, 'members': members}}})
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='running' WHERE id=?", (item['id'],))
        item = hud.view(item['id'])
        response = self.settings(item, 'members', 'assign_role', {'bot_id': item['bot_id'], 'role': 'reviewer'})
        self.assertEqual(response.status_code, 200, response.text)
        actual = self.frontend([hud.view(item['id'])], initial={'roles': roles, 'members': members})
        self.assertEqual(actual['members'][0]['role'], 'reviewer')

    def test_consecutive_basic_updates_keep_both_values_and_unsaved_input(self):
        item = self.running()
        with hud.connection() as conn:
            before = hud_tools.configuration(conn, item)
        self.assertEqual(self.request(item, {'display_name': 'assistant-name'}).status_code, 200)
        first = hud.view(item['id'])
        self.assertEqual(self.request(item, {'description': 'assistant-description'}).status_code, 200)
        second = hud.view(item['id'])
        for snapshots in ([second], [first, second], [second, second]):
            actual = self.frontend(snapshots, draft=False, project='p', initial={'basic': before})
            self.assertEqual(actual['basic']['display_name'], 'assistant-name')
            self.assertEqual(actual['basic']['description'], 'assistant-description')
        actual = self.frontend([first, second], draft=False, initial={'basic': before},
                               edits=[{'after': 0, 'basic': {'display_name': 'manual-unsaved'}}])
        self.assertEqual(actual['basic']['display_name'], 'manual-unsaved')
        self.assertEqual(actual['basic']['description'], 'assistant-description')

    def write_race(self, method, manual_args, update):
        ready, released, done = threading.Event(), threading.Event(), threading.Event()
        first_attempt, failures = [], []
        connect, business = api._conn, getattr(api, method)
        def connections():
            if threading.current_thread().name == 'manual-writer':
                conn = sqlite3.connect(db.db_path(), timeout=0)
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                return conn
            return connect()
        def manual():
            try:
                try:
                    business(*manual_args, identity=('manual', 'user'))
                    first_attempt.append('committed')
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc):
                        raise
                    first_attempt.append('blocked')
                finally:
                    ready.set()
                if first_attempt == ['blocked']:
                    if not released.wait(10):
                        raise AssertionError('Assistant write did not finish')
                    business(*manual_args, identity=('manual', 'user'))
            except BaseException as exc:
                failures.append(exc)
            finally:
                done.set()
        def interleave(*args, **kwargs):
            worker.start()
            self.assertTrue(ready.wait(10))
            return business(*args, **kwargs)
        worker = threading.Thread(target=manual, name='manual-writer', daemon=True)
        with patch.object(api, '_conn', side_effect=connections), patch.object(api, method, side_effect=interleave):
            try:
                response = update()
                self.assertEqual(response.status_code, 200, response.text)
            finally:
                released.set()
                self.assertTrue(done.wait(10))
                worker.join(1)
        self.assertFalse(failures, failures)
        self.assertEqual(first_attempt, ['blocked'])
        self.record(method, {'first_manual_attempt': first_attempt, 'assistant_status': response.status_code})

    def test_role_writer_cannot_be_overtaken_between_version_check_and_write(self):
        item = self.running()
        role = api.post_project_role('p', {'name': 'original'}, identity=('fixture', 'user'))
        version = self.settings(item, 'roles')['sections']['roles']['config_version']
        self.write_race('patch_project_role', ('p', role['id'], {'name': 'manual-newer'}),
            lambda: self.settings(item, 'roles', 'rename', {'role_id': role['id'], 'name': 'assistant-stale'}, version=version))
        self.assertEqual(self.conn.execute('SELECT name FROM project_roles WHERE id=?', (role['id'],)).fetchone()[0], 'manual-newer')
        stale = self.settings(item, 'roles', 'rename', {'role_id': role['id'], 'name': 'stale-retry'}, version=version)
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_member_role_and_activation_share_the_version_check_write_lock(self):
        item = self.running()
        assistant_role = api.post_project_role('p', {'name': 'assistant-role'}, identity=('fixture', 'user'))
        api.post_project_role('p', {'name': 'manual-role'}, identity=('fixture', 'user'))
        profile = self.conn.execute("SELECT profile FROM project_members WHERE project='p' LIMIT 1").fetchone()[0]
        for operation, method, manual, changes in (
            ('assign_role', 'set_project_member_role', ('p', profile, {'role': 'manual-role'}), {'profile': profile, 'role_id': assistant_role['id']}),
            ('set_active', 'set_member_active', (profile, {'project': 'p', 'active': False}), {'profile': profile, 'active': True})):
            with self.subTest(operation=operation):
                version = self.settings(item, 'members')['sections']['members']['config_version']
                self.write_race(method, manual, lambda: self.settings(item, 'members', operation, changes, version=version))
                self.assertEqual(self.settings(item, 'members', operation, changes, version=version).status_code, 409)
        actual = self.conn.execute("SELECT role,active FROM project_members WHERE project='p' AND profile=?", (profile,)).fetchone()
        self.assertEqual(tuple(actual), ('manual-role', 0))

    def test_role_and_member_writes_rollback_with_their_audit(self):
        item = self.running()
        role = api.post_project_role('p', {'name': 'original'}, identity=('fixture', 'user'))
        profile = self.conn.execute("SELECT profile FROM project_members WHERE project='p' LIMIT 1").fetchone()[0]
        for section, operation, changes in (
            ('roles', 'rename', {'role_id': role['id'], 'name': 'uncommitted'}),
            ('members', 'assign_role', {'profile': profile, 'role_id': role['id']})):
            with self.subTest(section=section):
                before = self.settings(item, section)['sections'][section]
                audit = self.conn.execute('SELECT COUNT(*) FROM project_config_events').fetchone()[0]
                with patch.object(hud_configuration, '_record', side_effect=RuntimeError('rollback-fixture')):
                    with self.assertRaisesRegex(RuntimeError, 'rollback-fixture'):
                        self.settings(item, section, operation, changes)
                self.assertEqual(self.settings(item, section)['sections'][section], before)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_config_events').fetchone()[0], audit)

    def legacy(self, *, cards=False, confirmed=False, mixed=False):
        item = self.running()
        proposal = {'id': 'legacy-proposal', 'changes': {'description': 'safe-description'} if mixed else {},
                    'facts': [{'label': 'legacy', 'value': 'LEGACY-SENTINEL'}],
                    'before_facts': {'legacy': 'LEGACY-SENTINEL'}, 'reason': 'LEGACY-SENTINEL'}
        entry = {'text': 'LEGACY-SENTINEL', 'proposal': proposal}
        if cards:
            entry = {'text': 'LEGACY-SENTINEL', 'cards': [entry,
                {'question': {'id': 'safe-question', 'title': 'safe-title', 'reason': 'safe-reason', 'options': []}}]}
        with hud.connection() as conn:
            hud.add_entry(conn, item, 'assistant', entry)
            hud.add_entry(conn, item, 'user', {'text': 'unrelated-user-message'})
            item['state']['facts'] = [{'label': 'saved', 'value': 'LEGACY-SENTINEL'}] if confirmed else []
            if not confirmed:
                item['state']['cards' if cards else 'proposal'] = copy.deepcopy(entry['cards'] if cards else proposal)
            conn.execute("UPDATE hud_sessions SET state=?,status='waiting' WHERE id=?", (hud.encode(item['state']), item['id']))
        return item, entry

    def test_all_legacy_note_replay_channels_are_clean_and_history_is_retained(self):
        for options in ({}, {'confirmed': True}, {'mixed': True}, {'cards': True}, {'cards': True, 'mixed': True}):
            with self.subTest(options=options):
                # Each fixture has independent HUD history.
                old = self.legacy(**options)
                item, original = old
                current = hud.view(item['id'])
                with hud.connection() as conn:
                    payload = hud.context(conn, current)
                    raw = [json.loads(r[0]) for r in conn.execute('SELECT content FROM hud_entries WHERE session=?', (item['id'],))]
                self.assertNotIn('LEGACY-SENTINEL', hud.encode(payload))
                self.assertIn(original, raw)
                self.record('context', {'case': options, 'model_has_legacy_sentinel': 'LEGACY-SENTINEL' in hud.encode(payload), 'audit_preserved': original in raw, 'pending_proposal': payload['pending_proposal'], 'pending_cards': payload['pending_cards']})
                self.assertIn('unrelated-user-message', hud.encode(payload['history']))
                self.assertIn('template', payload)
                self.assertIn('project', payload)
                if options.get('mixed'):
                    proposal = payload['pending_cards'][0]['proposal'] if options.get('cards') else payload['pending_proposal']
                    self.assertEqual(proposal['changes'], {'description': 'safe-description'})
                elif options.get('cards'):
                    self.assertEqual(len(payload['pending_cards']), 1)
                else:
                    self.assertIsNone(payload['pending_proposal'])
                with hud.connection() as conn:
                    conn.execute('DELETE FROM hud_entries WHERE session=?', (item['id'],))
                    conn.execute('DELETE FROM hud_sessions WHERE id=?', (item['id'],))

    def test_new_note_payloads_are_rejected_at_both_response_levels(self):
        proposal = {'changes': {}, 'facts': [{'label': 'legacy', 'value': 'LEGACY-SENTINEL'}]}
        for response in ({'text': 'legacy', 'proposal': proposal}, {'text': 'legacy', 'cards': [{'text': 'legacy', 'proposal': proposal}]}):
            with self.assertRaisesRegex(ValueError, '项目备注已转入历史'):
                hud.validate_response(copy.deepcopy(response))


if __name__ == '__main__':
    unittest.main()
