"""req#122 task#213 service：原生启停入口（Python 标准库，不依赖 Git Bash）。

start/stop 子命令：
  start  就绪检查(install.py check) -> 数据目录解析 -> .env 导入(统一 dotenv 剥引号/CR，环境优先)
         -> 旧引擎优雅退出 -> 启动(start_engine.py 后台) -> 健康检查
  stop   数据目录解析 -> .env 导入 -> 优雅停止(stop_engine.py)

.env 导入复用 engine/config.py 的 _parse_dotenv（strip 掉 CR/空白、剥单双引号、去 export 前缀、
跳注释），只把进程环境未定义的键导入 os.environ，保留「环境 > .env」优先级（不覆盖用户已 export 的键）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROGRAM_ROOT = Path(__file__).resolve().parent.parent
if str(PROGRAM_ROOT) not in sys.path:
    sys.path.insert(0, str(PROGRAM_ROOT))
from engine import config  # noqa: E402


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


def _health_check(host: str, port: int) -> int:
    url = f"http://{host}:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(15):
        try:
            with opener.open(url, timeout=1) as response:
                if json.load(response).get("status") == "ok":
                    print("[start-engine] 引擎健康检查通过")
                    return 0
        except (OSError, ValueError):
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
    actual = urllib.parse.urlsplit(config.engine_url())
    if (actual.hostname or "") != expected[0] or actual.port != expected[1]:
        print(f"[stop-engine] 停机目标不一致：engine_url={config.engine_url()} "
              f"与 host/port={expected[0]}:{expected[1]} 不符"
              f"（检查 GROUP_ENGINE_URL/XDG_GROUP_ENGINE_URL 环境变量），拒绝执行", file=sys.stderr)
        return 1
    return subprocess.run(
        [str(venv_python()), "-X", "utf8", str(PROGRAM_ROOT / "tools" / "stop_engine.py")]
    ).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop"])
    args = parser.parse_args(argv)
    if args.command == "start":
        return do_start()
    return do_stop()


if __name__ == "__main__":
    sys.exit(main())
