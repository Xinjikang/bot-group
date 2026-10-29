#!/usr/bin/env bash
# bootstrap 下载校验执行入口：探测 Python 后调 tools/bootstrap.py。
# 下载/校验/解压/落地/install 全在 bootstrap.py（Python 标准库）实现，无 Git Bash 也能跑；
# 临时目录只存下载/解压，install 装到持久目录 INSTALL_DIR（默认当前目录）。
# 发布页通过环境变量注入：BOOTSTRAP_URL、BOOTSTRAP_SHA256、（可选）INSTALL_DIR。
cd "$(dirname "$0")" || exit 1

if command -v python >/dev/null 2>&1; then
  PY=python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v py >/dev/null 2>&1; then
  PY=py
else
  echo "[bootstrap] 未找到 Python，请安装 Python 3.11+ 后重试。" >&2
  exit 1
fi

"$PY" tools/bootstrap.py
