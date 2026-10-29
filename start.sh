#!/usr/bin/env bash
# 启动 group-engine 引擎：就绪检查、优雅停止旧服务、启动并验证新服务。
# 端口/监听/数据目录/.env 均由统一配置层（engine/config.py）解析，优先级 命令行>环境>.env>默认；
# .env 统一 dotenv 解析（剥引号/CR/LF/export 前缀），只把进程环境未定义的键导入（环境>.env）。
cd "$(dirname "$0")" || exit 1

if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v py >/dev/null 2>&1; then PY=py
else
  echo "[start-engine] 未找到 Python。请安装 Python 3.11+ 后重试。" >&2
  exit 1
fi

"$PY" tools/service.py restart
