#!/usr/bin/env bash
# 安装入口（req#122 task#213）：检查 Python、建 venv、装锁定依赖、首次配置、自检。
# 幂等可重跑；失败保持未就绪。无需预装 Git。
cd "$(dirname "$0")" || exit 1

# 探测 Python（干净环境 venv 尚未就绪，用系统解释器跑 install.py）。
if command -v python >/dev/null 2>&1; then
  PY=python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v py >/dev/null 2>&1; then
  PY=py
else
  echo "[install] 未找到 Python，请安装 Python 3.11+ 后重试。" >&2
  exit 1
fi

"$PY" tools/install.py install
