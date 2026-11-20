"""bot-group relay 私聊工具（req#173 验收 1/2）：Hermes 管理员私聊操作。

- 与 CLI 共用签发/监听/兑换逻辑与审计（listen/pairing 模块）；
- 私聊按渠道发送者身份核验：工具 handler 接收网关注入的会话上下文
  （session_id 等 kwargs，插件契约 plugins/AGENTS.md 的 optional context
  kwargs 机制），按插件配置的授权管理员 profile 白名单核对；上下文不可
  用或不在白名单时拒绝——默认拒绝，绝不裸奔；
- 工具只输出脱敏结果：链接/凭据不进入对话历史（写 0600 文件后只提示
  文件路径与指纹短值供确认）。
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# 授权管理员白名单：BOT_GROUP_RELAY_ADMIN 逗号分隔（profile 名/成员名）。
# 缺省空 = 私聊入口拒绝一切（必须先显式授权），CLI 白名单不受影响。


def _authorized_profiles() -> set[str]:
    return {name.strip() for name in
            (os.environ.get("BOT_GROUP_RELAY_ADMIN") or "").split(",")
            if name.strip()}


def _caller_identity(kwargs: dict) -> str:
    """从网关注入的会话上下文提取调用者身份（渠道发送者/profile）。
    字段按 Hermes 工具 dispatcher 实际注入的 kwargs 为准（session_id 等
    由插件契约保证；不同版本字段名有差异时按常见名依次尝试）。"""
    for key in ("session_id", "task_id"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _check_authorized(kwargs: dict) -> bool:
    allowed = _authorized_profiles()
    if not allowed:
        return False
    caller = _caller_identity(kwargs)
    if not caller:
        return False
    return caller in allowed


def _tool_result(ok: bool, message: str, **fields) -> str:
    payload = {"ok": ok, "message": message}
    payload.update({k: v for k, v in fields.items() if v not in ("", None)})
    return json.dumps(payload, ensure_ascii=False)


def bot_group_relay_status(args: dict, **kwargs) -> str:
    if not _check_authorized(kwargs):
        return _tool_result(False, "仅授权管理员可通过私聊操作（BOT_GROUP_RELAY_ADMIN）")
    from . import pairing
    return _tool_result(True, "状态查询成功", **pairing.pairing_metadata())


def bot_group_listen(args: dict, **kwargs) -> str:
    if not _check_authorized(kwargs):
        return _tool_result(False, "仅授权管理员可通过私聊操作（BOT_GROUP_RELAY_ADMIN）")
    from . import listen as listen_mod
    try:
        result = listen_mod.listen_serve(
            address=str(args.get("address") or ""),
            port=int(args.get("port") or 0),
            advertise=str(args.get("advertise") or ""),
            member=str(args.get("member") or ""),
            tls=not bool(args.get("no_tls")),
            # 私聊上下文：链接绝不进入对话历史——写 0600 文件
            link_to_file=True)
    except (ValueError, RuntimeError) as exc:
        return _tool_result(False, f"listen 失败：{exc}")
    return _tool_result(
        True, "监听已开启，配对链接已写入本机 0600 文件",
        address=result["address"], port=result["port"],
        advertise=result["advertise"], scheme=result["scheme"],
        fingerprint_short=result["fingerprint_short"],
        link_file=result.get("link_file", ""))


def bot_group_register(args: dict, **kwargs) -> str:
    if not _check_authorized(kwargs):
        return _tool_result(False, "仅授权管理员可通过私聊操作（BOT_GROUP_RELAY_ADMIN）")
    engine_url = str(args.get("engine_url") or os.environ.get("BOT_GROUP_ENGINE_URL") or "")
    member = str(args.get("member") or os.environ.get("BOT_GROUP_RELAY_MEMBER") or "")
    credential = str(args.get("credential") or os.environ.get("BOT_GROUP_RELAY_CREDENTIAL") or "")
    if not engine_url or not member or not credential:
        return _tool_result(False, "需要 engine_url/member/credential")
    from pathlib import Path
    from . import config as relay_config
    from . import relay as relay_mod
    cfg = relay_config.RelayConfig(
        engine_url=engine_url, member=member, credential=credential,
        mcp_python="", home=os.environ.get("HERMES_HOME") or
        str(Path.home() / ".hermes"))
    client = relay_mod.RelayClient(cfg)
    client.start()
    return _tool_result(True, "方向 A 注册连接已启动", member=member)


def register_tools(ctx) -> None:
    """向 gateway 注册私聊工具（check_fn 默认启用；handler 内做身份核验）。"""
    tools = (
        ("bot_group_listen", bot_group_listen,
         "开启 bot-group 方向 B 监听（绑本机具体网卡地址），生成一次性配对链接",
         {"address": {"type": "string", "description": "绑定的本机网卡单播地址"},
          "port": {"type": "integer", "description": "监听端口"},
          "advertise": {"type": "string", "description": "对端可达地址/域名（可选）"},
          "member": {"type": "string", "description": "成员名（可选，缺省 HERMES_PROFILE）"},
          "no_tls": {"type": "boolean", "description": "禁用 TLS（仅隔离测试）"}},
         ["address", "port"]),
        ("bot_group_register", bot_group_register,
         "bot-group 方向 A 注册：连引擎 WS 端点（复用 #316 出站链路）",
         {"engine_url": {"type": "string", "description": "引擎地址（可选）"},
          "member": {"type": "string", "description": "成员名（可选）"},
          "credential": {"type": "string", "description": "节点凭据（可选）"}},
         []),
        ("bot_group_relay_status", bot_group_relay_status,
         "查看 bot-group relay 监听/配对状态（脱敏）", {}, []),
    )
    for name, handler, description, properties, required in tools:
        parameters = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(
            name=name, toolset="bot-group-relay", handler=handler,
            description=description,
            schema={"name": name, "description": description,
                    "parameters": parameters},
            emoji="\U0001f91d")
