"""Ask the engine to drain and exit; never kill a process or browser here."""
import argparse
from http.client import RemoteDisconnected
import json
import os
from pathlib import Path
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from engine import auth
from engine import config
from engine.browser_resources import _pid_alive


def stop(url=None, timeout=180):
    if url is None:
        url = config.engine_url()
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('shutdown URL must be local HTTP')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {}
    if auth._password():
        headers['Authorization'] = 'Bearer ' + auth.login(auth._password())['token']

    def request(path, method='GET'):
        req = urllib.request.Request(url.rstrip('/') + path, method=method, headers=headers)
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    try:
        health = request('/health')
    except urllib.error.HTTPError:
        raise RuntimeError('端口上的服务无法确认是引擎，已停止操作') from None
    except urllib.error.URLError:
        # Only a refused connection proves that no service is listening.
        try:
            with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=3):
                pass
        except ConnectionRefusedError:
            return '引擎已停止'
        raise RuntimeError('无法确认引擎状态，已停止操作') from None
    if health.get('graceful_shutdown') != 1 or not health.get('instance_id'):
        raise RuntimeError('当前运行的是旧版引擎，请手动完成首次重启以启用优雅退出')
    pid, instance = health['pid'], health['instance_id']
    expected_instance = os.environ.get('GROUP_RESTART_INSTANCE')
    if expected_instance and instance != expected_instance:
        raise RuntimeError('重启目标实例已变更，已停止操作')
    if type(pid) is not int or pid <= 0:
        raise RuntimeError('引擎进程标识无效')
    for key, expected in (('program_root', config.program_root()), ('db_path', config.db_path())):
        actual = health.get(key)
        if not isinstance(actual, str) or not actual or Path(actual).resolve() != Path(expected).resolve():
            raise RuntimeError(f'引擎 {key} 与当前实例不匹配，请核对配置后重试')
    result = request('/admin/shutdown', 'POST')
    if result.get('instance_id') != instance:
        raise RuntimeError('引擎实例已变化，已停止操作')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = request('/admin/shutdown')
        except urllib.error.HTTPError:
            raise RuntimeError('退出状态查询被拒绝，请检查引擎日志') from None
        except (urllib.error.URLError, RemoteDisconnected, ConnectionResetError):
            if not _pid_alive(pid):
                try:
                    with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=3):
                        pass
                except ConnectionRefusedError:
                    return '引擎已优雅退出，进程和监听端口已释放'
                except TimeoutError:
                    pass  # Windows may briefly time out while the listener closes.
            time.sleep(0.2)
            continue
        if result.get('instance_id') != instance:
            raise RuntimeError('引擎实例已变化，已停止等待')
        if result.get('state') == 'failed':
            raise RuntimeError(result.get('error') or '引擎退出清理失败')
        time.sleep(0.2)
    raise TimeoutError('等待引擎优雅退出超时；保留当前进程，请检查执行状态和日志')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=config.engine_url())
    args = parser.parse_args()
    try:
        print('[stop-engine] ' + stop(args.url))
    except (OSError, ValueError, RuntimeError) as exc:
        print('[stop-engine] ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
