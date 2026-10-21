#!/usr/bin/env bash
# 优雅停止 group-engine 引擎：原生 Python 入口 scripts/service.py stop（不依赖 Git Bash）。
# 读同一份数据目录 .env（统一 dotenv 解析，剥引号/CR，环境>.env），按同一配置停止。
cd "$(dirname "$0")" || exit 1

if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v py >/dev/null 2>&1; then PY=py
else
  echo "[stop-engine] 未找到 Python。请安装 Python 3.11+ 后重试。" >&2
  exit 1
fi

"$PY" scripts/service.py stop
