"""Windows queue -> native process -> real MCP -> reply/seen without WMIC."""
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from engine import db, process_query, project_rules, queue, session_adapter, wake
from scripts.unified_test_support import isolated_api, isolated_database


@unittest.skipUnless(os.name == 'nt', 'Windows console launcher')
class WmicFreeWakeTests(unittest.TestCase):
    def test_queue_process_mcp_reply_and_seen(self):
        with isolated_database() as database:
            root = Path(database).parent
            report = root / 'receipt.json'
            work = root / 'alpha'
            work.mkdir()
            import pip
            stub = Path(pip.__file__).parent / '_vendor/distlib/t64.exe'
            payload = io.BytesIO()
            fixture = Path(__file__).parent / 'fixtures/hermes_wake_probe.py'
            with zipfile.ZipFile(payload, 'w') as archive:
                archive.writestr('__main__.py', fixture.read_bytes())
            entry = root / 'hermes.exe'
            entry.write_bytes(stub.read_bytes() + f'#!"{sys.executable}"\n'.encode('utf-8') + payload.getvalue())
            env = {'PATH': '', 'HERMES_HOME': str(root / 'hermes'), 'HERMES_MANAGED_DIR': str(root / 'managed'),
                   'GROUP_DATA_DIR': str(root / 'data'), 'GROUP_ATTACHMENTS_DIR': str(root / 'attachments'),
                   'HERMES_BIN': str(entry), 'PROCESS_PROBE_REPORT': str(report)}
            conn = db.get_connection()
            try:
                conn.execute("UPDATE projects SET directory_mode='shared' WHERE name='alpha'")
                conn.execute("UPDATE messages SET content='@default probe',mentions='[\"default\"]',need_reply=1 WHERE id=1")
                conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(1,'default','pending')")
                conn.commit()
                session_name = wake.session_name('default', 'work', None, 'alpha')
                with patch.dict(os.environ, env), isolated_api() as url, \
                     patch.dict(os.environ, {'GROUP_ENGINE_URL': url, 'XDG_GROUP_ENGINE_URL': url}), \
                     patch.object(session_adapter, 'available', return_value=False), \
                     patch.object(project_rules, 'sync'), \
                     patch.object(wake, '_workdir_for', return_value=str(work)):
                    self.assertTrue(wake.enqueue(conn, 'default', 'work', 'msg:1', trigger_msg_id=1, project='alpha'))
                    with patch.object(process_query, 'list_processes', return_value=None):
                        self.assertFalse(queue.process_one(conn))
                    self.assertEqual(conn.execute('SELECT status FROM wake_jobs').fetchone()[0], 'queued')
                    try:
                        self.assertTrue(queue.process_one(conn))
                        job = dict(conn.execute('SELECT status,detail FROM wake_jobs').fetchone())
                        self.assertEqual(job['status'], 'done', job)
                        deadline = time.monotonic() + 20
                        while not report.with_suffix('.ready').exists() and time.monotonic() < deadline:
                            time.sleep(.05)
                        self.assertTrue(report.with_suffix('.ready').exists())
                        pids = wake.find_bot_processes('default', session_name)
                        self.assertTrue(pids, pids)
                        self.assertTrue(wake.session_busy('default', 'work', None, 'alpha'))
                        report.with_suffix('.continue').touch()
                        deadline = time.monotonic() + 40
                        while session_name in wake._running_sessions and time.monotonic() < deadline:
                            time.sleep(.05)
                        self.assertNotIn(session_name, wake._running_sessions)
                        run = dict(conn.execute('SELECT pid,exit_code,ended_at FROM wake_runs').fetchone())
                        self.assertEqual(run['exit_code'], 0, run)
                        receipt = json.loads(report.read_text(encoding='utf-8'))
                        self.assertEqual(receipt['blocked']['blocked'], [1])
                        self.assertEqual(receipt['seen'], {'seen': 1})
                        self.assertEqual(conn.execute("SELECT status FROM inbox WHERE profile='default' AND message_id=1").fetchone()[0], 'seen')
                        reply = conn.execute('SELECT sender,mentions,need_reply FROM messages WHERE id=?',
                                             (receipt['sent']['message_id'],)).fetchone()
                        self.assertEqual(tuple(reply), ('default', '["user"]', 0))
                        self.assertEqual(wake.find_bot_processes('default', session_name), [])
                        evidence = os.environ.get('PROCESS_QUERY_EVIDENCE_DIR')
                        if evidence:
                            folder = Path(evidence)
                            folder.mkdir(parents=True, exist_ok=True)
                            (folder / 'wake-loop.json').write_text(json.dumps(
                                dict(job=job, run=run, detected_pids=pids, receipt=receipt,
                                     runtime='deterministic Hermes stand-in; real queue/process/MCP/API'),
                                ensure_ascii=False, indent=2), encoding='utf-8')
                    finally:
                        row = conn.execute('SELECT pid FROM wake_runs WHERE ended_at IS NULL').fetchone()
                        if row and row['pid']:
                            try:
                                parent = process_query.psutil.Process(row['pid'])
                                children = parent.children(recursive=True)
                                for child in reversed(children):
                                    child.kill()
                                parent.kill()
                                process_query.psutil.wait_procs(children + [parent], timeout=5)
                            except process_query.psutil.NoSuchProcess:
                                pass
                        for thread in threading.enumerate():
                            if thread.name == 'session-default':
                                thread.join(timeout=10)
                                self.assertFalse(thread.is_alive())
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
