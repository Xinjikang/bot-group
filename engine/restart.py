"""A detached restart receipt survives the engine it replaces."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from . import config
from .browser_resources import _pid_alive


def receipt_path():
    return config.logs_dir() / 'service-restart.json'


def read_receipt():
    try:
        return json.loads(receipt_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def write_receipt(receipt):
    path = receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def status():
    receipt = read_receipt()
    if receipt.get('state') in ('starting', 'restarting'):
        pid = receipt.get('helper_pid')
        if time.time() - receipt['created_at'] > 10 and (not pid or not _pid_alive(pid)):
            return {**receipt, 'state': 'failed', 'error': '重启进程已退出，请查看 service-restart.log'}
    return receipt


class Restart:
    def __init__(self, instance_id):
        self.instance_id = instance_id
        self.lock = threading.Lock()

    def begin(self):
        with self.lock:
            current = status()
            if current.get('state') in ('starting', 'restarting'):
                return current
            root = config.program_root().resolve()
            receipt = {'id': uuid.uuid4().hex, 'instance_id': self.instance_id,
                       'state': 'starting', 'created_at': time.time(), 'error': '',
                       'program_root': str(root), 'db_path': str(Path(config.db_path()).resolve())}
            # Freeze effective CLI/env settings before handing off to a new process.
            env = dict(os.environ)
            host, port = config.host(), config.port()
            env.update(GROUP_ENGINE_HOST=host, GROUP_ENGINE_PORT=str(port),
                       GROUP_DATA_DIR=str(config.data_dir()), GROUP_DB=receipt['db_path'],
                       GROUP_ATTACHMENTS_DIR=str(config.attachments_dir()),
                       GROUP_ENGINE_URL=f'http://{host}:{port}',
                       GROUP_RESTART_INSTANCE=self.instance_id)
            write_receipt(receipt)
            options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} \
                if os.name == 'nt' else {'start_new_session': True}
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            try:
                with (config.logs_dir() / 'service-restart.log').open('ab', buffering=0) as output:
                    subprocess.Popen([str(executable), '-X', 'utf8', str(root / 'tools' / 'restart_engine.py'),
                                      receipt['id']], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, close_fds=True, **options)
            except OSError as exc:
                receipt.update(state='failed', error=str(exc))
                write_receipt(receipt)
                raise
            return receipt
