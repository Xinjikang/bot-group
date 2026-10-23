"""Persist and launch administrator-confirmed Release upgrades."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import config, restart
from .browser_resources import _pid_alive
from .version import version_info
from tools import releases, upgrade

ACTIVE = {'starting', 'running'}


def receipt_path():
    return config.logs_dir() / 'app-upgrade.json'


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
    if receipt.get('state') in ACTIVE and time.time() - receipt['created_at'] > 15:
        pid = receipt.get('helper_pid')
        if not pid or not _pid_alive(pid):
            return {**receipt, 'state': 'failed', 'error': '升级进程已退出，请查看 logs/app-upgrade.log'}
    return receipt


def blocked_reason(info=None):
    info = info or version_info()
    if info.get('version_source') == 'git':
        return '当前为 Git 源码工作区，请通过 Git 更新；界面自动升级适用于 Release 安装。'
    if not releases.version_key(info.get('current_version') or ''):
        return '当前安装缺少有效版本号，请核对安装来源。'
    if not upgrade._current_release_manifest():
        return '当前安装缺少发布文件清单，请核对安装来源。'
    return ''


def base_python():
    executable = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    if not executable.is_file() or executable.is_relative_to(config.program_root() / '.venv'):
        raise ValueError('无法定位独立 Python，请检查安装环境。')
    return executable


def begin(request_id, version, instance_id):
    with restart.maintenance_lock:
        current = status()
        if current.get('id') == request_id or current.get('state') in ACTIVE:
            return current
        if restart.status().get('state') in ('starting', 'restarting'):
            raise ValueError('服务正在重启，请完成后再升级。')
        info = version_info()
        reason = blocked_reason(info)
        if reason:
            raise ValueError(reason)
        target = upgrade.discover_release()
        if target.version != version:
            raise ValueError('可用版本已变化，请重新检查并确认升级。')
        if releases.version_key(target.version) <= releases.version_key(info['current_version']):
            raise ValueError('当前已是相同或更高版本。')
        root = config.program_root().resolve()
        executable = base_python()
        receipt = {'id': request_id, 'state': 'starting', 'phase': 'queued',
                   'created_at': time.time(), 'instance_id': instance_id, 'error': '',
                   'current_version': info['current_version'], 'target_version': target.version,
                   'target': asdict(target), 'program_root': str(root),
                   'db_path': str(Path(config.db_path()).resolve())}
        env = dict(os.environ)
        host, port = config.host(), config.port()
        env.update(GROUP_ENGINE_HOST=host, GROUP_ENGINE_PORT=str(port),
                   GROUP_DATA_DIR=str(config.data_dir()), GROUP_DB=receipt['db_path'],
                   GROUP_ATTACHMENTS_DIR=str(config.attachments_dir()),
                   GROUP_ENGINE_URL=f'http://{host}:{port}')
        write_receipt(receipt)
        options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} \
            if os.name == 'nt' else {'start_new_session': True}
        try:
            with (config.logs_dir() / 'app-upgrade.log').open('ab', buffering=0) as output:
                subprocess.Popen([str(executable), '-X', 'utf8', str(root / 'tools/update_engine.py'), request_id],
                                 cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.STDOUT, close_fds=True, **options)
        except OSError as exc:
            receipt.update(state='failed', error=str(exc))
            write_receipt(receipt)
            raise
        return receipt
