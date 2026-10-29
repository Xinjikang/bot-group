#!/usr/bin/env bash
# Run upgrades with the base interpreter so the installation venv can be replaced.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "[upgrade] Run the bot-group installer first." >&2
  exit 1
fi
BASE_PY="$(.venv/bin/python -c 'import sys; print(sys._base_executable)')" || exit 1
if [ -z "$BASE_PY" ] || [ ! -x "$BASE_PY" ]; then
  echo "[upgrade] Base Python is unavailable. Repair the Python installation." >&2
  exit 1
fi
exec "$BASE_PY" -X utf8 tools/upgrade.py upgrade "$@"
