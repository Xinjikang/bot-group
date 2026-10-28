"""Native service entry using the Python standard library.

start reuses a healthy matching instance or starts it, then opens the UI.
restart checks readiness, drains the old instance, starts and verifies its replacement.
stop drains the matching instance. Maintenance helpers select restart semantics
through GROUP_SERVICE_MAINTENANCE so older rollback targets keep accepting start.
Dotenv loading preserves explicit environment values.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROGRAM_ROOT = Path(__file__).resolve().parents[2]
from engine import config


def venv_python() -> Path:
    if sys.platform == "win32":
        return PROGRAM_ROOT / ".venv" / "Scripts" / "python.exe"
    return PROGRAM_ROOT / ".venv" / "bin" / "python"


def _load_env() -> None:
    """数据目录 .env 统一解析（config._parse_dotenv 剥引号/CR/LF/export 前缀），只把进程环境未定义的键导入。"""
    env = config._parse_dotenv(config.data_dir() / ".env")
    for key, value in env.items():
        if key not in os.environ:
            os.environ[key] = value


def _run_install_check() -> int:
    return subprocess.run(
        [str(venv_python()), "-X", "utf8", str(PROGRAM_ROOT / "tools" / "install.py"), "check"]
    ).returncode


def _local_url(host: str, port: int) -> str:
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    return f'http://{host}:{port}'


def _running_instance(host: str, port: int) -> bool:
    from engine import auth
    headers = {}
    if auth.configured():
        headers['Authorization'] = 'Bearer ' + auth.login(auth._password())['token']
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(_local_url(host, port) + '/health', headers=headers)
    try:
        with opener.open(request, timeout=2) as response:
            health = json.load(response)
    except (OSError, ValueError):
        return False
    if not isinstance(health, dict) or health.get('status') != 'ok' or not health.get('instance_id'):
        raise RuntimeError('端口上的服务身份无法确认，请检查配置和服务日志')
    for key, expected in (('program_root', config.program_root()), ('db_path', config.db_path())):
        actual = health.get(key)
        if not isinstance(actual, str) or not actual or Path(actual).resolve() != Path(expected).resolve():
            raise RuntimeError(f'运行实例的 {key} 与当前安装不匹配，请检查端口和数据目录')
    return True


def _ui_request(host: str, port: int, path: str, method: str) -> dict:
    from engine import auth
    headers = {}
    if auth.configured():
        headers['Authorization'] = 'Bearer ' + auth.login(auth._password())['token']
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(_local_url(host, port) + path, method=method, headers=headers)
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def _claim_ui(host: str, port: int) -> dict:
    try:
        result = _ui_request(host, port, '/admin/ui/activate', 'POST')
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print('[start-engine] 当前服务尚未支持页面复用，请手动重启服务并刷新已有界面。')
            return {'action': 'open'}
        raise
    if not isinstance(result, dict) or result.get('action') not in ('open', 'reuse', 'pending'):
        raise ValueError('界面状态响应无效')
    return result


def _release_ui(host: str, port: int, reservation: str | None):
    if reservation:
        try:
            _ui_request(host, port, '/admin/ui/open/' + urllib.parse.quote(reservation, safe=''), 'DELETE')
        except (OSError, ValueError):
            pass


def do_launch(*, open_browser: bool = True) -> int:
    """Reuse the same healthy installation, or start it, then open its UI."""
    _load_env()
    host, port = config.host(), config.port()
    try:
        running = _running_instance(host, port)
    except RuntimeError as exc:
        print(f'[start-engine] {exc}', file=sys.stderr)
        return 1
    if not running:
        result = do_start()
        if result:
            return result
    url = _local_url(host, port) + '/app/'
    print(f'[start-engine] 界面地址：{url}')
    if open_browser:
        try:
            claim = _claim_ui(host, port)
        except (OSError, ValueError) as exc:
            print(f'[start-engine] 界面状态核对失败，请稍后重试：{exc}', file=sys.stderr)
            return 1
        if claim['action'] != 'open':
            print('[start-engine] 已请求唤回现有界面' if claim['action'] == 'reuse' else '[start-engine] 界面正在打开')
            return 0
        try:
            if not webbrowser.open(url):
                _release_ui(host, port, claim.get('reservation'))
                print(f'[start-engine] 浏览器打开失败，请访问 {url}', file=sys.stderr)
        except (OSError, webbrowser.Error) as exc:
            _release_ui(host, port, claim.get('reservation'))
            print(f'[start-engine] 浏览器打开失败：{exc}；请访问 {url}', file=sys.stderr)
    return 0


def _health_check(host: str, port: int) -> int:
    for _ in range(15):
        try:
            if _running_instance(host, port):
                print("[start-engine] 引擎健康检查通过")
                return 0
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(1)
    print("[start-engine] 启动失败，请检查日志", file=sys.stderr)
    return 1


def do_start() -> int:
    if not venv_python().exists():
        print("[start-engine] 未找到 .venv，请先运行 install（install.sh / install.bat）", file=sys.stderr)
        return 1
    if _run_install_check() != 0:
        return 1
    # 启动前请求旧引擎优雅退出，失败保留旧进程并中止启动
    if do_stop() != 0:
        return 1
    _load_env()
    from engine.maintenance.database_layout import migrate
    try:
        migrate()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"[start-engine] 数据库目录迁移失败：{exc}", file=sys.stderr)
        return 1
    host = config.host()
    port = config.port()
    print(f"[start-engine] 启动引擎，监听 {host}:{port}")
    r = subprocess.run([str(venv_python()), str(PROGRAM_ROOT / "tools" / "start_engine.py")])
    if r.returncode != 0:
        return r.returncode
    return _health_check(host, port)


def do_stop() -> int:
    if not venv_python().exists():
        print("[stop-engine] 未找到 .venv，请先运行 install（install.sh / install.bat）", file=sys.stderr)
        return 1
    _load_env()
    # 防误伤（task#215 事故教训）：本机管理命令的停机目标必须与 host():port() 一致。
    # engine_url() 优先级里 GROUP_ENGINE_URL/XDG_GROUP_ENGINE_URL 是给「bot 跨进程
    # spawn 转发」用的存量覆盖，从网关会话里跑 service stop 时会继承 XDG 变量指向
    # 别的实例（如生产 8180）——管理命令明确拒绝这种静默偏移，宁可失败也不打错引擎。
    expected = (config.host(), config.port())
    configured_url = config.engine_url()
    default_url = f'http://{expected[0]}:{expected[1]}'
    actual = urllib.parse.urlsplit(_local_url(*expected) if configured_url == default_url else configured_url)
    expected_host = urllib.parse.urlsplit(_local_url(*expected)).hostname
    if (actual.hostname or "") != expected_host or actual.port != expected[1]:
        print(f"[stop-engine] 停机目标不一致：engine_url={config.engine_url()} "
              f"与 host/port={expected[0]}:{expected[1]} 不符"
              f"（检查 GROUP_ENGINE_URL/XDG_GROUP_ENGINE_URL 环境变量），拒绝执行", file=sys.stderr)
        return 1
    return subprocess.run(
        [str(venv_python()), "-X", "utf8", str(PROGRAM_ROOT / "tools" / "stop_engine.py"),
         '--url', _local_url(*expected)]
    ).returncode


def _maintenance_start() -> bool:
    if os.environ.get('GROUP_SERVICE_MAINTENANCE') == '1' or os.environ.get('GROUP_RESTART_INSTANCE'):
        return True
    # Earlier upgrade helpers call start without a flag. Their receipt identifies
    # the direct parent, so a concurrent manual launcher keeps its own semantics.
    try:
        receipt = json.loads((config.logs_dir() / 'app-upgrade.json').read_text(encoding='utf-8'))
        return (isinstance(receipt, dict) and receipt.get('state') == 'running'
                and receipt.get('helper_pid') == os.getppid()
                and receipt.get('program_root') == str(config.program_root().resolve())
                and receipt.get('db_path') == str(Path(config.db_path()).resolve()))
    except (OSError, ValueError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop", "restart"])
    parser.add_argument('--data-dir', type=Path, help='实例数据目录')
    parser.add_argument('--no-browser', action='store_true', help='启动后跳过打开界面')
    args = parser.parse_args(argv)
    if args.data_dir is not None:
        os.environ['GROUP_DATA_DIR'] = str(args.data_dir.resolve())
    if args.command == "start":
        if _maintenance_start():
            return do_start()
        return do_launch(open_browser=not args.no_browser)
    if args.command == "restart":
        return do_start()
    return do_stop()


if __name__ == "__main__":
    sys.exit(main())
