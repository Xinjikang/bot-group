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
        # start.bat already performs install check -> stop -> start -> health.
        command = ([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', str(ROOT / 'start.bat')]
                   if os.name == 'nt' else [sys.executable, str(ROOT / 'tools' / 'service.py'), 'start'])
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        # Detached Windows parents need explicit handles for child diagnostics.
        result = subprocess.run(command, cwd=ROOT, stdout=sys.stdout, stderr=sys.stderr, **options)
        if result.returncode:
            raise RuntimeError(f'服务启动脚本退出码 {result.returncode}，请查看 service-restart.log')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {}
        if auth.configured():
            headers['Authorization'] = 'Bearer ' + auth.login(auth._password())['token']
        request = urllib.request.Request(config.engine_url() + '/health', headers=headers)
        with opener.open(request, timeout=5) as response:
            health = json.load(response)
        if (health.get('status') != 'ok' or not health.get('instance_id')
                or health['instance_id'] == receipt['instance_id']
                or health.get('program_root') != receipt['program_root']
                or health.get('db_path') != receipt['db_path']):
            raise RuntimeError('重启后实例校验失败，请查看 service-restart.log')
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
