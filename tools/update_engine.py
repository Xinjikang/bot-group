"""Upgrade and restart an installation outside the engine and its virtualenv."""
from pathlib import Path
import json
import os
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import app_upgrade, config
from tools import releases, upgrade


def start_and_verify(receipt):
    root = config.program_root()
    service = root / 'tools/service.py'
    if not service.exists():
        service = root / 'scripts/service.py'
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run([sys.executable, '-X', 'utf8', str(service), 'start'], cwd=root, **options)
    if result.returncode:
        raise RuntimeError('服务启动失败，请查看 logs/app-upgrade.log 并运行 start.bat 恢复服务。')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = upgrade._admin_headers()
    with opener.open(urllib.request.Request(config.engine_url() + '/health', headers=headers), timeout=5) as response:
        health = json.load(response)
    if (health.get('status') != 'ok' or not health.get('instance_id')
            or health['instance_id'] == receipt['instance_id']
            or health.get('program_root') != receipt['program_root']
            or health.get('db_path') != receipt['db_path']):
        raise RuntimeError('启动后实例校验失败，请查看 logs/app-upgrade.log。')
    return health['instance_id']


def run(operation_id):
    receipt = app_upgrade.read_receipt()
    if receipt.get('id') != operation_id or receipt.get('state') != 'starting':
        return 1
    receipt.update(state='running', helper_pid=os.getpid())
    app_upgrade.write_receipt(receipt)
    stopped = False

    def progress(phase):
        nonlocal stopped
        if phase == 'backing_up':
            stopped = True
        receipt['phase'] = phase
        app_upgrade.write_receipt(receipt)

    try:
        reason = app_upgrade.blocked_reason()
        if reason:
            raise RuntimeError(reason)
        # Load environment before shutdown so the detached process can authenticate after restart.
        upgrade._admin_headers()
        target = releases.Release(**receipt['target'])
        result = upgrade.do_upgrade(target=target, progress=progress)
        restored = receipt['phase'] == 'rolled_back'
        safe_to_start = result == 0 or restored or receipt['phase'] == 'backing_up'
        if stopped and safe_to_start:
            progress('restarting')
            try:
                receipt['new_instance_id'] = start_and_verify(receipt)
            except Exception:
                if result:
                    raise
                backup = upgrade.latest_backup()
                if backup is None:
                    raise RuntimeError('新版本启动失败，升级备份缺失，请查看 logs/app-upgrade.log。')
                audit = json.loads((backup / 'upgrade_target.json').read_text(encoding='utf-8'))
                if audit.get('target') != receipt['target']:
                    raise RuntimeError('新版本启动失败，备份目标不匹配，请查看 logs/app-upgrade.log。')
                progress('rolling_back')
                if upgrade._rollback(backup) != 0:
                    progress('rollback_failed')
                    raise RuntimeError('新版本启动失败，恢复原版本失败，请查看 logs/app-upgrade.log。')
                progress('restarting')
                receipt['new_instance_id'] = start_and_verify(receipt)
                restored, result = True, 1
        if result:
            message = '升级失败，已恢复原版本并启动服务。' if restored else '升级失败，请查看 logs/app-upgrade.log。'
            raise RuntimeError(message)
        if upgrade.current_version() != receipt['target_version']:
            raise RuntimeError('升级后版本校验失败，请查看 logs/app-upgrade.log。')
        receipt.update(state='succeeded', phase='complete')
        return 0
    except Exception as exc:
        receipt.update(state='failed', error=str(exc))
        print(f'[app-upgrade] {exc}', file=sys.stderr)
        return 1
    finally:
        app_upgrade.write_receipt(receipt)


if __name__ == '__main__':
    sys.exit(run(sys.argv[1]))
