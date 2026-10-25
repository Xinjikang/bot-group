"""req#124 #217：Windows 原生 exe 替身，真实 cmd 截断复现及 spawn 完整传输。

使用 pip 自带的 distlib PE launcher + 标准库 argv 记录脚本；零模型、零服务、零网络。
"""
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config, db, wake


@unittest.skipUnless(sys.platform == 'win32', 'Windows command transport')
class HermesTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hermes-transport-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / '中文 入口'
        self.bin.mkdir()
        self.work = self.root / '中文 项目'
        self.work.mkdir()
        self.capture = self.root / 'received.json'
        self.receipt_number = 0
        self.native = self.bin / 'native hermes.exe'
        self.wrapper = self.bin / 'hermes.cmd'
        self._build_native_probe()
        self.wrapper.write_text('@echo off\n"%~dp0native hermes.exe" %*\n', encoding='ascii')
        self.env = patch.dict(os.environ, {
            'PATH': str(self.bin), 'HERMES_BIN': '',
            'HERMES_HOME': str(self.root / 'hermes-home'), 'CODEX_HOME': str(self.root / 'codex-home'),
            'GROUP_DB': str(self.root / 'group.db'), 'GROUP_DATA_DIR': str(self.root / 'data'),
            'GROUP_ATTACHMENTS_DIR': str(self.root / 'attachments'),
            'GROUP_ENGINE_HOST': '127.0.0.1', 'GROUP_ENGINE_PORT': '18042',
            'GROUP_ENGINE_URL': 'http://127.0.0.1:18042', 'XDG_GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'TRANSPORT_PROBE_REPORT': str(self.capture),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.block = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止网络'))
        self.block.start()
        self.addCleanup(self.block.stop)
        self.block_ex = patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('禁止网络'))
        self.block_ex.start()
        self.addCleanup(self.block_ex.stop)
        self.messages = [
            dict(message_id=101, sender='发起者甲', sender_type='user', need_reply=1,
                 content='中文第一行\n\n正文 "双引号" 与 \'单引号\'\r\nLF/CRLF 尾行',
                 attachments=[dict(id='img-217', filename='图 "甲".png', mime='image/png',
                                   width=2, height=3, size=24)]),
            dict(message_id=102, sender='reviewer-02', sender_type='bot', need_reply=0,
                 content='第二条 & | < > ^ %PATH% !NAME! $(echo 217) `literal`\n结尾\\'),
        ]

    def _build_native_probe(self):
        # 与 Python console_scripts 相同的真实 PE 启动器，替身仅记录 argv 后退出。
        import pip
        stub = Path(pip.__file__).parent / '_vendor' / 'distlib' / 't64.exe'
        script = '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
query=args[args.index('-q')+1] if '-q' in args else None
overlay=os.environ.get('HERMES_MANAGED_DIR')
config=json.loads((Path(overlay)/'config.yaml').read_text(encoding='utf-8')) if overlay else None
Path(os.environ['TRANSPORT_PROBE_REPORT']).write_text(json.dumps({'argv':args,'query':query,'cwd':os.getcwd(),'pid':os.getpid(),'overlay':overlay,'config':config,'database_env':os.environ.get('GROUP_DB')},ensure_ascii=False),encoding='utf-8')
'''
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as z:
            z.writestr('__main__.py', script)
        self.native.write_bytes(stub.read_bytes() + f'#!"{sys.executable}"\n'.encode('utf-8') + payload.getvalue())

    def _record(self, received, **evidence):
        # 可选保留合成正文证据；生产 wake 日志仅记录入口路径。
        directory = os.environ.get('HERMES_TRANSPORT_EVIDENCE_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            self.receipt_number += 1
            (folder / f'{self._testMethodName}-{self.receipt_number}.json').write_text(
                json.dumps({'received': received, **evidence}, ensure_ascii=False, indent=2), encoding='utf-8')

    def _run(self, entry, signal, sid='续会话-217'):
        if self.capture.exists():
            self.capture.unlink()
        result = subprocess.run([str(entry), '-p', 'fixture', 'chat', '--in', str(self.work),
                                 '-c', sid, '-Q', '-q', signal], cwd=self.work,
                                capture_output=True, timeout=15, creationflags=wake._NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        received = json.loads(self.capture.read_text(encoding='utf-8'))
        self._record(received, entry=str(entry), expected_query=signal, exit_code=result.returncode)
        return received

    def test_old_cmd_returns_zero_but_truncates_multiline(self):
        # 原始机制复现：真正启动 cmd 包装，无 Popen mock；仅使用无副作用正文。
        body = '[system wake] full messages follow\n\nmessage id=101 body=Chinese-text\nmessage id=102 body=second'
        received = self._run(self.wrapper, body)
        self.assertEqual(received['query'], body.splitlines()[0])
        self.assertNotEqual(received['query'], body)

    def test_default_path_cmd_transfers_two_messages_exactly(self):
        self.assertEqual(Path(shutil.which('hermes')).resolve(), self.wrapper.resolve())
        entry = wake._hermes_bin()
        self.assertEqual(Path(entry), self.native.resolve())
        body = wake._compose_injection(self.messages, 'work', trigger_msg_id=102) + wake._TASK_SUFFIX.format(task_id=217)
        received = self._run(entry, body)
        self.assertEqual(received['query'], body)
        self.assertEqual(received['cwd'], str(self.work))
        self.assertEqual(received['argv'][received['argv'].index('-c') + 1], '续会话-217')

    def test_explicit_entries_and_line_endings_are_literal(self):
        sentinel = self.root / 'shell-executed.txt'
        for entry in (self.native, self.wrapper):
            for newline in ('\n', '\r\n'):
                with self.subTest(entry=entry, newline=repr(newline)), patch.dict(os.environ, {'HERMES_BIN': str(entry)}):
                    text = f'首行 "quotes"{newline}{newline}& echo BAD > "{sentinel}" | ^ %PATH% !X!{newline}末行\\'
                    self.assertEqual(self._run(wake._hermes_bin(), text)['query'], text)
                    self.assertFalse(sentinel.exists())

    def test_path_exe_wins_over_cmd_and_explicit_override_wins(self):
        preferred = self.bin / 'hermes.exe'
        shutil.copyfile(self.native, preferred)
        self.assertEqual(Path(wake._hermes_bin()), preferred)
        with patch.dict(os.environ, {'HERMES_BIN': str(self.wrapper)}):
            self.assertEqual(Path(wake._hermes_bin()), self.native)

    def test_native_venv_fallback_is_executable(self):
        candidate = self.root / 'hermes/hermes-agent/venv/Scripts/hermes.exe'
        candidate.parent.mkdir(parents=True)
        shutil.copyfile(self.native, candidate)
        with patch.object(Path, 'home', return_value=self.root), patch.object(shutil, 'which', return_value=None):
            entry = wake._hermes_bin()
        self.assertEqual(Path(entry), candidate)
        self.assertEqual(self._run(entry, '兜底入口\n完整正文')['query'], '兜底入口\n完整正文')

    def test_explicit_path_command_and_bom_crlf_wrapper(self):
        self.wrapper.write_text(f'@echo off\r\nREM pure forwarding\r\n@"{self.native}" %*\r\n', encoding='utf-8-sig', newline='')
        for configured in ('hermes', 'hermes.cmd', str(self.wrapper)):
            with self.subTest(configured=configured), patch.dict(os.environ, {'HERMES_BIN': configured}):
                entry = wake._hermes_bin()
                self.assertEqual(Path(entry), self.native)
                self.assertEqual(self._run(entry, '命令名与CRLF包装\n完整正文')['query'], '命令名与CRLF包装\n完整正文')

    def test_unresolvable_wrapper_and_missing_target_fail_explicitly(self):
        for script in ('@echo off\nset X=1\n"%~dp0native hermes.exe" %*\n',
                       '@echo off\n"%VENV%\\hermes.exe" %*\n',
                       '@echo off\n"relative.exe" %*\n'):
            with self.subTest(script=script):
                self.wrapper.write_text(script, encoding='ascii')
                with self.assertRaises(RuntimeError):
                    wake._hermes_bin()
        self.wrapper.write_text('@echo off\n"%~dp0missing.exe" %*\n', encoding='ascii')
        with self.assertRaises(FileNotFoundError):
            wake._hermes_bin()

    def _database(self, kind):
        conn = db.get_connection()
        db.init_schema(conn)
        conn.execute("INSERT INTO projects(name,root,created_at,directory_mode) VALUES('transport',?,'now','shared')", (str(self.work),))
        from scripts.project_test_support import enable_members
        enable_members(conn, ('transport',), ('fixture',))
        if kind == 'chat':
            conn.execute("INSERT INTO threads(id,project,title,creator,members,status,turn_profile,round,created_at,last_active_at) VALUES(217,'transport','fixture','user','[\"user\",\"fixture\"]','open','fixture',1,'now','now')")
        for m in self.messages:
            conn.execute("INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions,need_reply,thread_id) VALUES(?,'transport','now',?,?,?,?,?,?)",
                         (m['message_id'], m['sender'], m['sender_type'], m['content'], '["fixture"]', m['need_reply'], 217 if kind == 'chat' else None))
            conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'fixture','pending')", (m['message_id'],))
        conn.execute("INSERT INTO attachments VALUES('img-217','transport','fixture',101,0,?,'image.png','image/png',24,'fixture-sha',2,3,'now')", ('图 "甲".png',))
        conn.commit()
        conn.close()

    def _spawn(self, kind, signal=None, *, binding=False, bound_task=None, runtime='hermes'):
        self._database(kind)
        from engine import events, heartbeat, project_rules, session_adapter, task_retry
        if runtime == 'codex':
            with closing(db.get_connection()) as conn:
                bot = conn.execute("INSERT INTO bots(host_type,profile,config_root,runtime,display_name,role) VALUES('codex','fixture',?,'codex','fixture','')", (str(self.root / 'codex-home'),)).lastrowid
                conn.execute("UPDATE project_members SET bot_id=? WHERE profile='fixture'", (bot,))
                conn.commit()
        # 保留真实 _spawn/_finalize、SQLite 和 Popen；隔离运行时会话创建/外部任务收尾。
        with patch.object(session_adapter, 'available', return_value=binding), \
             patch.object(session_adapter, 'resolve_or_create', return_value='bound-session-217'), \
             patch.object(wake, 'session_busy', return_value=False), \
             patch.object(wake, 'topic_busy', return_value=False), \
             patch.object(project_rules, 'sync'), \
             patch.object(task_retry, 'select_task', return_value=bound_task), \
             patch.object(wake, '_handle_task_exit'), \
             patch.object(events.bus, 'publish'), patch.object(heartbeat, 'on_exit'), \
             patch.object(wake, '_codex_bin', return_value=str(self.native)):
            result = wake._spawn('fixture', kind, signal, 217 if kind == 'chat' else None,
                                 'transport', trigger_msg_id=102)
            self.assertTrue(result['ok'], result)
            session = wake.session_name('fixture', kind, 217 if kind == 'chat' else None, 'transport')
            deadline = time.monotonic() + 15
            while session in wake._running_sessions and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertNotIn(session, wake._running_sessions)
            # 收尾线程完成后才退出隔离 patch，覆盖 wake.finished/heartbeat 最后两步。
            for thread in threading.enumerate():
                if thread.name == 'session-fixture':
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
        received = json.loads(self.capture.read_text(encoding='utf-8'))
        with closing(db.get_connection()) as conn:
            run = dict(conn.execute('SELECT ended_at,exit_code,runtime_session_id FROM wake_runs WHERE session=? ORDER BY started_at DESC LIMIT 1', (session,)).fetchone())
        self.assertIsNotNone(run['ended_at'])
        self.assertEqual(run['exit_code'], 0)
        self.assertIsNone(received['database_env'])
        if runtime == 'hermes':
            server = received['config']['mcp_servers']['task_queue']
            self.assertEqual(server['env']['TASK_QUEUE_ROLE'], 'fixture')
            self.assertEqual(server['env']['HERMES_CHAT_MODE'], '1' if kind == 'chat' else '0')
            self.assertNotIn('GROUP_DB', server['env'])
            self.assertFalse(Path(received['overlay']).exists())
        self._record(received, spawn_result=result, runtime=runtime, wake_run=run)
        return received

    def test_real_work_spawn_keeps_snapshot_attachment_and_task_suffix(self):
        received = self._spawn('work', bound_task=217)
        conn = db.get_connection()
        snapshot = wake._snapshot_pending(conn, 'fixture', 'work', None, 'transport')
        conn.close()
        expected = wake._compose_injection(snapshot, 'work', trigger_msg_id=102) + wake._TASK_SUFFIX.format(task_id=217)
        self.assertEqual(received['query'], expected)
        self.assertIn('附件', received['query'])

    def test_failed_spawn_cleans_runtime_mcp_overlay(self):
        self._database('work')
        with patch('engine.session_adapter.available', return_value=False), \
             patch('engine.project_rules.sync'), patch('engine.task_retry.select_task', return_value=None), \
             patch.object(wake, 'session_busy', return_value=False), \
             patch.object(wake, '_handle_task_exit'), \
             patch.object(wake.subprocess, 'Popen', side_effect=OSError('synthetic startup failure')):
            result = wake._spawn('fixture', 'work', 'signal', None, 'transport')
        self.assertFalse(result['ok'])
        from engine.execution_errors import MESSAGES
        self.assertEqual(result['detail'], MESSAGES['unknown'])
        conn = db.get_connection()
        try:
            self.assertIn('synthetic startup failure', conn.execute('SELECT diagnostic FROM execution_errors').fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(list((self.root / 'data' / 'runtime' / 'mcp').iterdir()), [])

    def test_real_chat_spawn_preserves_bound_session_and_complete_body(self):
        received = self._spawn('chat', binding=True)
        conn = db.get_connection()
        snapshot = wake._snapshot_pending(conn, 'fixture', 'chat', 217, 'transport')
        conn.close()
        self.assertEqual(received['query'], wake._compose_injection(snapshot, 'chat', trigger_msg_id=102))
        self.assertEqual(received['argv'][received['argv'].index('-c') + 1], 'bound-session-217')
        self.assertNotIn('--create-if-missing', received['argv'])

    def test_real_approved_spawn_and_whole_snapshot_fallback(self):
        signal = wake._APPROVED_SIGNAL.format(task_id=217)
        received = self._spawn('work', signal, bound_task=217)
        self.assertEqual(received['query'], signal + wake._TASK_SUFFIX.format(task_id=217))

    def test_real_over_budget_spawn_uses_whole_snapshot_fallback(self):
        self.messages[1]['content'] = '超预算正文' * 10000
        received = self._spawn('work')
        self.assertIn('upper_id=102', received['query'])
        self.assertIn('分页补读', received['query'])
        self.assertNotIn('正文：', received['query'])
        self.assertNotIn('已全量注入', received['query'])

    def test_real_codex_spawn_preserves_bare_prompt(self):
        signal = 'Codex 保持原始 prompt\n第二行 "quotes" & literal'
        received = self._spawn('work', signal, runtime='codex')
        self.assertEqual(received['argv'][-1], signal)
        self.assertNotIn('-q', received['argv'])

    def test_unsupported_wrapper_spawn_fails_before_launch(self):
        self._database('work')
        self.wrapper.write_text('@echo off\nset X=1\n"%~dp0native hermes.exe" %*\n', encoding='ascii')
        with patch('engine.session_adapter.available', return_value=False), \
             patch('engine.project_rules.sync'):
            result = wake._spawn('fixture', 'work', '完整输入\n第二行', None, 'transport')
        self.assertFalse(result['ok'], result)
        from engine.execution_errors import MESSAGES
        self.assertEqual(result['detail'], MESSAGES['unknown'])
        self.assertFalse(self.capture.exists())


if __name__ == '__main__':
    unittest.main()
