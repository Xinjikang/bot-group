"""req#122 task#213 install：检查 Python、建专用 venv、装锁定依赖、探测 Hermes/Codex、首次配置、自检。

幂等可重跑；失败保持未就绪（开始前失效旧状态，失败后 check 判未就绪）。
依赖一律用 venv 内 python -m pip（不裸 pip）；requirements.lock 随版本交付（发布时锁定）作为 pip
安装输入，安装后校验摘要，不事后 freeze；就绪判据 = Python 版本 + requirements 摘要 + 锁定清单摘要，
跟代码绑定。

子命令：
  install  完整安装（失效旧状态 -> Python 检查 -> venv -> pip 装锁定清单 -> 探测 -> 首次配置 -> 自检 -> 写状态）
  check    就绪检查（start 前轻量调用）：venv + 状态文件 + venv 实际 Python 版本 + requirements 摘要 + 锁定清单摘要
  repair   迁移旧环境（已有 .venv 但无 install.json）：验证现有 venv 依赖 + 记录 venv 实际版本，不重装

安装记录与就绪检查统一以 venv 内实际解释器为唯一口径（install.bat 用 PATH Python 建 venv 时，
PATH Python 与 venv Python 版本可能不同，记录/校验都用 venv 实际版本，避免 check 恒失败）。

本脚本只依赖标准库（干净环境用系统 Python 跑，此时 venv/依赖尚未就绪）；数据目录复用
engine/config.py（同样只依赖标准库）的 data_dir() 口径，保证 install/start/stop/健康检查共用一份。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROGRAM_ROOT = Path(__file__).resolve().parents[2]
VENV_DIR = PROGRAM_ROOT / ".venv"
REQUIREMENTS = PROGRAM_ROOT / "requirements.txt"
LOCKFILE = PROGRAM_ROOT / "requirements.lock"
# 已安装发布依赖的 Requires-Python 元数据（pydantic-settings/rpds-py）要求 >=3.11。
MIN_PYTHON = (3, 11)

from engine import config  # 仅标准库，干净环境可 import


# 首次配置 .env 模板（默认免密 + 回环监听，全部注释，用户按需取消注释）。
_ENV_TEMPLATE = """# bot-group 本地配置（默认免密、回环监听 127.0.0.1:8180）。
# 端口/监听地址/凭据在此按需覆盖，优先级：命令行 > 进程环境 > 本文件 > 默认值。
# 配置与运行数据默认保存在程序根目录；修改参数并保存后重启引擎。
# 自定义数据目录需在运行安装、启停和升级命令前设置环境变量 GROUP_DATA_DIR。
# GROUP_ENGINE_HOST=127.0.0.1
# GROUP_ENGINE_PORT=8180
# GROUP_ADMIN_PASSWORD=<设置后启用登录，留空则本机免密直通>
"""


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_path() -> Path:
    return config.data_dir() / "install.json"


def current_python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def venv_python_version() -> str:
    """查询 venv 内解释器的实际版本（引擎真实运行的解释器）。

    安装记录与就绪检查统一以 venv 实际解释器为唯一口径，避免 install.bat 用 PATH Python
    建 venv、但 venv 已是另一版本时记录错版本导致 check 恒失败（node 1066 启动回归）。
    """
    code = "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    try:
        out = subprocess.run([str(venv_python()), "-c", code], capture_output=True, text=True)
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def check_python() -> str:
    """检查解释器版本，不足直接报错（不建 venv）。"""
    v = sys.version_info
    if (v.major, v.minor) < MIN_PYTHON:
        raise RuntimeError(
            f"需要 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，当前 {current_python_version()}"
        )
    return current_python_version()


def _invalidate_state() -> None:
    """失效旧就绪状态：install 开始前调用，任何一步失败后 check 判未就绪（可重装）。"""
    sp = state_path()
    try:
        sp.unlink()
    except FileNotFoundError:
        pass


def ensure_venv(base_python: str | None = None) -> None:
    """建专用 venv（幂等：已存在则跳过）。base_python 指定建 venv 的解释器（回滚重建匹配旧解释器）。"""
    if venv_python().exists():
        return
    python = base_python or sys.executable
    subprocess.run([python, "-m", "venv", str(VENV_DIR)], check=True)


def pip_install() -> None:
    """venv 内 python -m pip 装依赖（不裸 pip）。

    发布锁定：requirements.lock 随版本交付，作为 pip 安装输入（确定性版本）；
    仓库缺 lock 时回退 requirements.txt（开发环境未锁定，仅提示）。
    """
    if LOCKFILE.exists():
        src = LOCKFILE
    else:
        src = REQUIREMENTS
        print("[install] 提示：仓库无 requirements.lock，回退 requirements.txt（未锁定）", file=sys.stderr)
    subprocess.run([str(venv_python()), "-m", "pip", "install", "-r", str(src)], check=True)


def _find_executable(name: str) -> str:
    """在 PATH 找可执行文件（Windows 补 .exe/.cmd/.bat 后缀），返回绝对路径或空串。"""
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        for ext in (".exe", ".cmd", ".bat"):
            p = shutil.which(name + ext)
            if p:
                return p
    return ""


def detect_runtime() -> dict:
    """探测 Hermes/Codex 运行环境：验证可执行文件真实存在，缺项报告（只读，不安装，不阻塞引擎本体）。"""
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    hermes_bin = os.environ.get("HERMES_BIN", "").strip()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    exe = "hermes.exe" if sys.platform == "win32" else "hermes"

    # Hermes 可执行文件：HERMES_BIN > HERMES_HOME/bin/hermes > PATH
    hermes_candidates = [hermes_bin] if hermes_bin else []
    if hermes_home:
        hermes_candidates.append(str(Path(hermes_home) / "bin" / exe))
    hermes_candidates.append(_find_executable("hermes"))
    hermes_found = next((p for p in hermes_candidates if p and Path(p).exists()), "")

    # Codex 可执行文件：CODEX_HOME/codex > PATH（codex 通常由 npm 全局装在 PATH）
    codex_candidates = []
    if codex_home:
        codex_candidates.append(str(Path(codex_home) / exe.replace("hermes", "codex")))
    codex_candidates.append(_find_executable("codex"))
    codex_found = next((p for p in codex_candidates if p and Path(p).exists()), "")

    return {
        "hermes": {"home": hermes_home, "bin": hermes_found},
        "codex": {"home": codex_home, "bin": codex_found},
    }


def first_config() -> Path:
    """首次配置：确保数据目录存在 + 生成 .env 模板（已存在则保留，不覆盖用户配置）。"""
    data_dir = config.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    env = data_dir / ".env"
    if not env.exists():
        env.write_text(_ENV_TEMPLATE, encoding="utf-8")
    return env


def selfcheck() -> None:
    """用 venv 内 python 校验关键依赖可 import（装错/半装立即暴露，含 MCP server 的 fastmcp）。"""
    code = "import fastapi, uvicorn, httpx, yaml, PIL, tomlkit, ruamel.yaml, psutil, mcp.server.fastmcp"
    subprocess.run([str(venv_python()), "-c", code], check=True)


def verify_lock_versions() -> bool:
    """核验 venv 已装依赖版本与 requirements.lock 锁定版本逐项一致（lock 漂移不得盖就绪章）。

    只对 `name==version` 精确锁定行做版本核对（本仓库 lock 均为精确 == 行）；缺包或版本不符返回 False。
    修复 node 1071 P1：selfcheck 仅 import 少数包，fastapi==999.999.999 锁漂移会被误盖就绪章。
    """
    if not LOCKFILE.exists():
        return False
    code = (
        "import importlib.metadata as md, sys\n"
        "bad = []\n"
        "for raw in open(sys.argv[1], encoding='utf-8'):\n"
        "    line = raw.split('#', 1)[0].strip()\n"
        "    if '==' not in line:\n"
        "        continue\n"
        "    name, want = line.split('==', 1)\n"
        "    name = name.strip()\n"
        "    want = want.split(';', 1)[0].strip()\n"
        "    try:\n"
        "        got = md.version(name)\n"
        "    except md.PackageNotFoundError:\n"
        "        bad.append(f'{name}: missing')\n"
        "        continue\n"
        "    if got != want:\n"
        "        bad.append(f'{name}: {got} != {want}')\n"
        "if bad:\n"
        "    print('LOCK DRIFT: ' + '; '.join(bad))\n"
        "    sys.exit(1)\n"
        "print('lock versions OK')\n"
    )
    out = subprocess.run(
        [str(venv_python()), "-c", code, str(LOCKFILE)], capture_output=True, text=True
    )
    return out.returncode == 0


def write_state() -> None:
    """写就绪状态（成功才写；失败保持未就绪）。记录 venv 实际解释器版本，与 check 口径一致。"""
    data = {
        "python_version": venv_python_version(),
        "requirements_sha256": _sha256(REQUIREMENTS),
        "lock_sha256": _sha256(LOCKFILE),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def do_install(rebuild: bool = False, base_python: str | None = None) -> int:
    try:
        # 失败重装：开始前失效旧状态，任何一步失败后 check 判未就绪
        _invalidate_state()
        version = check_python()
        print(f"[install] 调用方 Python {version} OK")
        if rebuild and VENV_DIR.exists():
            # 回滚依赖恢复：删除旧 venv（含升级残留新依赖/新解释器），重建匹配旧解释器与旧 lock
            shutil.rmtree(VENV_DIR)
            print(f"[install] 重建 venv：已删除旧 {VENV_DIR}")
        ensure_venv(base_python)
        print(f"[install] venv 就绪：{VENV_DIR}")
        pip_install()
        print("[install] 依赖安装完成")
        runtime = detect_runtime()
        print(f"[install] 探测 Hermes/Codex：{json.dumps(runtime, ensure_ascii=False)}")
        missing = [k for k, v in runtime.items() if not v.get("bin")]
        if missing:
            print(f"[install] 提示：未探测到可执行文件 {', '.join(missing)}（引擎本体仍可运行，bot 运行时需补齐）")
        env = first_config()
        print(f"[install] 首次配置：{env}")
        selfcheck()
        print("[install] 自检通过")
        write_state()
        print(f"[install] 就绪状态：{state_path()}（venv Python {venv_python_version()}）")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[install] 失败，保持未就绪：{exc}", file=sys.stderr)
        return 1


def do_check() -> int:
    """就绪检查（start 前调用）：缺环境/依赖摘要变化提示重跑 install，不误判就绪。"""
    if not venv_python().exists():
        print("[check] 未找到 .venv，请先运行 install（install.sh / install.bat）", file=sys.stderr)
        return 1
    sp = state_path()
    if not sp.exists():
        # 旧环境（已有 .venv 但无安装记录）：给出可操作迁移提示，而非只叫用户重跑 install
        print(
            "[check] 检测到已有 .venv 但缺少安装记录（旧版本环境）。\n"
            "       请运行 install.bat repair（或 tools/install.py repair）验证并迁移现有环境（不重装依赖）；\n"
            "       或运行 install.bat 重新安装。",
            file=sys.stderr,
        )
        return 1
    try:
        state = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[check] 安装状态损坏，请重新运行 install", file=sys.stderr)
        return 1
    venv_version = venv_python_version()
    if not venv_version:
        print("[check] venv 解释器无法运行，请重新运行 install", file=sys.stderr)
        return 1
    if state.get("python_version") != venv_version:
        print("[check] venv 解释器版本变化，请重新运行 install", file=sys.stderr)
        return 1
    if state.get("requirements_sha256") != _sha256(REQUIREMENTS):
        print("[check] 依赖声明变化，请重新运行 install", file=sys.stderr)
        return 1
    if not LOCKFILE.exists():
        print("[check] 缺少锁定清单，请重新运行 install", file=sys.stderr)
        return 1
    if state.get("lock_sha256") != _sha256(LOCKFILE):
        print("[check] 锁定清单变化，请重新运行 install", file=sys.stderr)
        return 1
    print("[check] 就绪")
    return 0


def do_repair() -> int:
    """迁移旧环境（已有 .venv 但无 install.json）：验证现有 venv 依赖 + 记录 venv 实际版本，不重装。

    半安装/缺依赖时 selfcheck 失败，不写就绪状态（保持可重装），返回 1 提示 install。
    """
    if not venv_python().exists():
        print("[repair] 未找到 .venv，请先运行 install（install.sh / install.bat）", file=sys.stderr)
        return 1
    version = venv_python_version()
    if not version:
        print("[repair] venv 解释器无法运行，请重新运行 install", file=sys.stderr)
        return 1
    try:
        parts = tuple(int(x) for x in version.split("."))
    except ValueError:
        print(f"[repair] venv Python 版本异常（{version}），请重新运行 install", file=sys.stderr)
        return 1
    if parts[:2] < MIN_PYTHON:
        print(f"[repair] venv Python {version} 低于 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，请重新运行 install", file=sys.stderr)
        return 1
    try:
        # 真实校验 venv 内关键依赖可 import（半安装/缺依赖会失败，不写就绪，避免伪造就绪标记）
        selfcheck()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[repair] 依赖校验失败，请运行 install 重新安装：{exc}", file=sys.stderr)
        return 1
    # 核验已装依赖版本与锁定清单一致：lock 漂移（如 fastapi==999.999.999）不得盖就绪章
    if not verify_lock_versions():
        print("[repair] 已装依赖与 requirements.lock 锁定版本不一致，请运行 install 重新安装", file=sys.stderr)
        return 1
    try:
        write_state()
    except OSError as exc:
        print(f"[repair] 写状态失败，保持未就绪：{exc}", file=sys.stderr)
        return 1
    print(f"[repair] 已迁移旧环境：venv Python {version}，就绪状态 {state_path()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    inst = sub.add_parser("install", help="完整安装（建 venv + 装锁定依赖 + 首次配置 + 自检）")
    inst.add_argument("--rebuild", action="store_true",
                      help="重建 venv（回滚依赖恢复用：删除旧 venv + 重装旧 lock，清掉升级残留新依赖）")
    inst.add_argument("--python", default=None,
                      help="重建 venv 用的基础解释器（回滚匹配旧解释器；缺省用当前解释器）")
    sub.add_parser("check", help="就绪检查（start 前轻量调用）")
    sub.add_parser("repair", help="迁移旧环境（验证现有 venv 依赖 + 记录版本，不重装）")
    args = parser.parse_args(argv)
    if args.command == "install":
        return do_install(rebuild=args.rebuild, base_python=args.python)
    if args.command == "check":
        return do_check()
    if args.command == "repair":
        return do_repair()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
