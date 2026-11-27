"""Run the existing service entry outside the old engine's lifetime."""
from pathlib import Path
import json
import os
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]

from engine import auth, config, restart


def run(operation_id):
    receipt = restart.read_receipt()
    if receipt.get('id') != operation_id:
        raise RuntimeError('重启请求已变更')
    receipt.update(state='restarting', helper_pid=os.getpid())
    restart.write_receipt(receipt)
    try:
        # Maintenance preserves install check -> stop -> start -> health.
        command = ([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', str(ROOT / 'start.bat')]
                   if os.name == 'nt' else [sys.executable, str(ROOT / 'tools' / 'service.py'), 'start'])
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        # Detached Windows parents need explicit handles for child diagnostics.
        env = {**os.environ, 'GROUP_SERVICE_MAINTENANCE': '1'}
        with (config.logs_dir() / 'service-restart.log').open('a+b', buffering=0) as log:
            start = log.tell()
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=log, **options)
            if result.returncode:
                log.seek(start)
                for line in log:
                    if line.startswith(b'[check-error] '):
                        raise RuntimeError(line.removeprefix(b'[check-error] ').decode('utf-8', 'replace').strip())
                raise RuntimeError(f'服务启动脚本退出码 {result.returncode}')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {}
        if auth.configured():
            headers['Authorization'] = 'Bearer ' + auth.login(auth._password())['token']
        request = urllib.request.Request(config.engine_url() + '/health', headers=headers)
        with opener.open(request, timeout=5) as response:
            health = json.load(response)
        expected_db = receipt['db_path']
        legacy = config.program_root().resolve() / 'group.db'
        if expected_db == str(legacy):
            from engine.maintenance.database_layout import relocated_path
            expected_db = str(relocated_path(legacy))
        if (health.get('status') != 'ok' or not health.get('instance_id')
                or health['instance_id'] == receipt['instance_id']
                or health.get('program_root') != receipt['program_root']
                or health.get('db_path') != expected_db):
            raise RuntimeError('重启后实例校验失败，请查看 service-restart.log')
        if expected_db != receipt['db_path']:
            receipt.update(previous_db_path=receipt['db_path'], db_path=expected_db)
        receipt.update(state='succeeded', new_instance_id=health['instance_id'])
        return 0
    except Exception as exc:
        receipt.update(state='failed', error=str(exc))
        print(f'[restart-engine] {exc}', file=sys.stderr)
        return 1
    finally:
        restart.write_receipt(receipt)


if __name__ == '__main__':
    sys.exit(run(sys.argv[1]))
