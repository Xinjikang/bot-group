"""统一配置解析层（req#122 task#212 配置底座）。

职责：install / start / stop / 升级 / 健康检查 / MCP 的共同底座。
优先级：命令行 > 进程环境 > 本地配置(.env) > 默认值。

统一一份「端口 / 监听地址 / 数据目录」配置；路径从脚本位置推导；数据目录
默认程序根目录，可通过启动参数或环境变量指定其他位置；Hermes/Codex 探测
沿用各模块既有 _root 口径（HERMES_HOME / CODEX_HOME 优先、缺省回退 native）。
默认回环监听 127.0.0.1、默认免密（未设管理员密码时本机放行，见 api._resolve_identity）。
"""

from __future__ import annotations

import os
from pathlib import Path

# 程序根 = 代码 + 依赖所在目录（从模块位置推导，不依赖工作目录）。
_PROGRAM_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8180

# 数据目录默认值用到的环境键（进程环境优先，其次 .env，其次默认值）。
_ENV_HOST = ("GROUP_ENGINE_HOST", "HOST")
_ENV_PORT = ("GROUP_ENGINE_PORT", "PORT")
_ENV_DATA_DIR = ("GROUP_DATA_DIR", "DATA_DIR")

# 模块级 CLI 覆盖状态：main 启动时 apply_cli 写入，host()/port()/data_dir()/engine_url()
# 无显式参数时读此状态，使命令行 --host/--port 贯穿到 wake/MCP 回调的 engine_url 派生。
_cli: dict[str, object] = {"host": None, "port": None, "data_dir": None}


def apply_cli(host=None, port=None, data_dir=None) -> None:
    """应用命令行覆盖（main 启动时调用一次）。"""
    _cli["host"] = host
    _cli["port"] = port
    _cli["data_dir"] = data_dir


def program_root() -> Path:
    """程序根目录（代码 + 依赖）。从脚本位置推导，防工作目录变化串目录。"""
    return _PROGRAM_ROOT


_dotenv_cache: dict[str, dict[str, str]] = {}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """解析 .env（KEY=VALUE，兼容 bash source 格式：export 前缀/注释/引号）。"""
    result: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key] = value
    except OSError:
        pass
    return result


def _dotenv() -> dict[str, str]:
    """惰性解析数据目录 .env，默认位于程序根目录。

    缓存按 data_dir 分桶，data_dir 变化时读取对应目录下的 .env。
    """
    path = data_dir()
    key = str(path)
    cached = _dotenv_cache.get(key)
    if cached is None:
        cached = _parse_dotenv(path / ".env")
        _dotenv_cache[key] = cached
    return cached


def _pick(cli, env_names: tuple[str, ...], default):
    """命令行 > 进程环境 > .env > 默认值。cli 为 None 表示未从命令行传入。"""
    if cli is not None and str(cli).strip() != "":
        return cli
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in env_names:
        value = _dotenv().get(name, "").strip()
        if value:
            return value
    return default


def host(cli=None) -> str:
    """监听地址，默认 127.0.0.1（回环信任边界）。"""
    if cli is None:
        cli = _cli["host"]
    value = str(_pick(cli, _ENV_HOST, DEFAULT_HOST)).strip()
    return value or DEFAULT_HOST


def port(cli=None) -> int:
    """引擎端口，默认 8180。非数字回退默认。"""
    if cli is None:
        cli = _cli["port"]
    value = str(_pick(cli, _ENV_PORT, DEFAULT_PORT)).strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def data_dir(cli=None) -> Path:
    """数据目录（group.db / attachments / backups / .env / 日志落这里）。

    确定顺序：命令行 > 环境(GROUP_DATA_DIR/DATA_DIR) > 程序根目录。
    不读 .env 的 DATA_DIR——.env 本身落数据目录，数据目录须由 CLI/环境决定，避免循环。
    """
    if cli is None:
        cli = _cli["data_dir"]
    if cli is not None and str(cli).strip() != "":
        return Path(str(cli)).expanduser().resolve()
    for name in _ENV_DATA_DIR:
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return program_root()


def db_path(cli=None) -> str:
    """group.db 路径。GROUP_DB 显式覆盖优先级最高（向后兼容既有测试/部署）。"""
    env = _pick(None, ("GROUP_DB",), "").strip()
    if env:
        if Path(env).resolve() == (program_root() / "group.db").resolve():
            from .maintenance.database_layout import relocated_path
            return str(relocated_path(Path(env).resolve()))
        return env
    root = data_dir(cli)
    if root.resolve() != program_root().resolve():
        return str(root / "group.db")
    legacy = root / "group.db"
    # Maintenance must identify a running legacy instance before relocating it.
    if legacy.exists():
        return str(legacy)
    from .maintenance.database_layout import relocated_path
    return str(relocated_path(root / "data" / "group.db"))


def attachments_dir(cli=None) -> Path:
    """附件目录。GROUP_ATTACHMENTS_DIR 覆盖；默认随 db 目录（保持既有语义）。"""
    env = _pick(None, ("GROUP_ATTACHMENTS_DIR",), "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if Path(db_path(cli)).resolve() == (program_root() / "data" / "group.db").resolve():
        return program_root() / "attachments"
    return Path(db_path(cli)).resolve().parent / "attachments"


def backups_dir(cli=None) -> Path:
    """引擎运行时备份目录，落数据目录。"""
    return data_dir(cli) / "backups"


def logs_dir(cli=None) -> Path:
    """引擎日志目录，落数据目录（升级不丢日志）。"""
    return data_dir(cli) / "logs"


def engine_url(cli_host=None, cli_port=None) -> str:
    """群聊工具转发的引擎 API 入口。优先级：GROUP_ENGINE_URL（存量覆盖）>
    XDG_GROUP_ENGINE_URL（跨进程 spawn 注入，XDG_ 前缀可穿过 Hermes gateway
    _build_safe_env 过滤）> host/port 派生。"""
    env = os.environ.get("GROUP_ENGINE_URL", "").strip()
    if env:
        return env.rstrip("/")
    xdg = os.environ.get("XDG_GROUP_ENGINE_URL", "").strip()
    if xdg:
        return xdg.rstrip("/")
    return f"http://{host(cli_host)}:{port(cli_port)}"
