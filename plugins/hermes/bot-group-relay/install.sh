#!/bin/sh
# bot-group relay 插件安装脚本（req#168 task#316，ECS 侧）。
#
# 用法：在解压后的插件目录内执行  ./install.sh
# 行为：为随包 MCP 建独立 venv 并按 requirements.lock 装依赖——
# 不 clone 仓库、不手动 pip；插件本体（转发代码）跑在 gateway 进程内，
# 只用标准库 + gateway 已带的 websockets，无额外安装。
# 安装完成后打印 MCP 解释器绝对路径（填入 BOT_GROUP_MCP_PYTHON 或留空自动探测）。
set -eu

PLUGIN_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV_DIR="$PLUGIN_DIR/venv"
LOCK_FILE="$PLUGIN_DIR/requirements.lock"

if [ ! -f "$LOCK_FILE" ]; then
    echo "install.sh: requirements.lock 缺失（分发包损坏），中止" >&2
    exit 1
fi

# 建独立 venv（不用 Hermes 自带 venv——已核本机 Hermes venv 无 mcp 包）
if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check \
    --requirement "$LOCK_FILE"

echo "bot-group relay 安装完成"
echo "MCP 解释器: $VENV_DIR/bin/python"
echo "将引擎地址/成员名/连接凭据配置到 BOT_GROUP_ENGINE_URL / BOT_GROUP_RELAY_MEMBER / BOT_GROUP_RELAY_CREDENTIAL"
