"""Hermes 私聊中的 bot-group relay 操作。发送者准入由宿主网关负责。"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


def _session_context() -> dict:
    """宿主受信会话字段（gateway.session_context get_session_env：
    ContextVar 优先、os.environ 兜底；工具 handler 运行在宿主进程内）。
    读不到一律空串——授权按空处理（拒绝）。"""
    try:
        from gateway.session_context import get_session_env
    except Exception:  # noqa: BLE001 非宿主进程（测试/CLI）不可用

        def get_session_env(name, default=""):  # noqa: ANN001
            return os.getenv(name, default) or default

    def _read(name: str) -> str:
        try:
            return get_session_env(name, "") or ""
        except Exception:  # noqa: BLE001
            return ""

    return {
        "chat_type": _read("HERMES_SESSION_CHAT_TYPE").strip(),
        "platform": (_read("HERMES_SESSION_PLATFORM").strip()
                     or _read("HERMES_SESSION_SOURCE").strip()),
        "actor_id": _read("HERMES_SESSION_ACTOR_ID").strip(),
        # 宿主归属（审计用，不作为授权主体）
        "profile": _read("HERMES_SESSION_PROFILE").strip(),
    }


def _is_direct_message(kwargs: dict) -> bool:
    """读取当前轮次的会话类型，来源字段仅用于审计。"""
    ctx = _session_context()
    logger.info("bot-group relay tool chat_type=%s platform=%s actor=%s profile=%s session=%s",
                ctx["chat_type"], ctx["platform"], ctx["actor_id"], ctx["profile"],
                kwargs.get("session_id") or "")
    return ctx["chat_type"] == "dm"


def _tool_result(ok: bool, message: str, **fields) -> str:
    payload = {"ok": ok, "message": message}
    payload.update({k: v for k, v in fields.items() if v not in ("", None)})
    return json.dumps(payload, ensure_ascii=False)


def bot_group_relay_status(args: dict, **kwargs) -> str:
    if not _is_direct_message(kwargs):
        return _tool_result(False, "请在 Hermes 私聊中使用此工具")
    from . import pairing
    return _tool_result(True, "状态查询成功", **pairing.status())


def bot_group_reconnect(args: dict, **kwargs) -> str:
    if not _is_direct_message(kwargs):
        return _tool_result(False, "请在 Hermes 私聊中使用此工具")
    from . import config, pairing, relay
    url = str(args.get("engine_url") or "").rstrip("/")
    peers = config.peer_configs()
    with relay._GATEWAY_LOCK:
        clients = {client.peer: client for client in relay._GATEWAY_CLIENTS.values()
                   if client.cfg.home == config.host_home()}
    for peer, client in clients.items():
        peers.setdefault(peer, client.cfg)
    matches = []
    for peer, cfg in peers.items():
        with pairing.using_peer(peer):
            address, key = pairing.saved_register(cfg.member)
        address = address or cfg.engine_url
        if address and (key or cfg.credential) and (not url or address.rstrip("/") == url):
            matches.append((peer, cfg, address))
    if len(matches) != 1:
        return _tool_result(False, "请指定已配对的引擎地址" if matches else "未找到匹配的已配对节点",
                            engines=[address for _, _, address in matches])
    peer, cfg, _ = matches[0]
    with pairing.using_peer(peer):
        if pairing.listener_selected(cfg.member, confirmed=True) and not pairing.outbound_requested(cfg.member):
            return _tool_result(False, "当前由引擎拨号，请在引擎节点页重试", state="listening")
        state, rearmed = pairing.resume_outbound()
    client = clients.get(peer)
    if client and rearmed:
        client._retry.set()
    message = "网关正在拨号，沿用本轮重试"
    if state == "requested":
        message = "重连请求已保存，等待网关拨号；若适配器已停止，请启动适配器"
    if state == "online":
        message = "已在线，沿用当前连接"
    return _tool_result(True, message, member=cfg.member, state=state)


def bot_group_listen(args: dict, **kwargs) -> str:
    if not _is_direct_message(kwargs):
        return _tool_result(False, "请在 Hermes 私聊中使用此工具")
    from . import listen as listen_mod
    try:
        result = listen_mod.listen_serve(
            address=str(args.get("address") or ""),
            port=int(args.get("port") or 0),
            advertise=str(args.get("advertise") or ""),
            engine_url=str(args.get("engine_url") or ""),
            tls=not bool(args.get("no_tls")),
            proxy_port=int(args.get("proxy_port") or 0),
            # 私聊上下文：链接绝不进入对话历史——写 0600 文件
            link_to_file=True)
    except (ValueError, RuntimeError, OSError) as exc:
        return _tool_result(False, f"listen 失败：{exc}")
    return _tool_result(
        True, "监听已开启（网关进程持有存活），配对链接已写入本机 0600 文件",
        address=result["address"], port=result["port"],
        advertise=result["advertise"], scheme=result["scheme"],
        fingerprint_short=result["fingerprint_short"],
        proxy_port=result.get("proxy_port", 0),
        link_file=result.get("link_file", ""))


def bot_group_register(args: dict, **kwargs) -> str:
    if not _is_direct_message(kwargs):
        return _tool_result(False, "请在 Hermes 私聊中使用此工具")
    engine_url = str(args.get("engine_url") or os.environ.get("BOT_GROUP_ENGINE_URL") or "")
    from . import config as relay_config
    credential = str(args.get("credential") or os.environ.get("BOT_GROUP_RELAY_CREDENTIAL") or "")
    invite = str(args.get("invite") or os.environ.get("BOT_GROUP_RELAY_INVITE") or "")
    member = (relay_config.host_member() if invite else args.get("member")
              or os.environ.get("BOT_GROUP_RELAY_MEMBER") or relay_config.host_member())
    if not engine_url or not member or not (credential or invite):
        return _tool_result(False, "需要 engine_url/member 与 credential 或 invite")
    from . import relay as relay_mod
    cfg = relay_config.RelayConfig(
        engine_url=engine_url, member=member, credential=credential,
        invite=invite, mcp_python="",
        home=relay_config.host_home())
    from . import pairing
    peer = pairing.select_peer(member, engine_url=engine_url)
    client = relay_mod.RelayClient(cfg, peer=peer, follow_saved=False)
    def activate():
        from dataclasses import replace
        with pairing.using_peer(peer):
            if invite:
                client.pair_invitation()
            else:
                client.confirm_credential()
            _, key = pairing.saved_register(member)
        relay_mod.gateway_client(replace(cfg, invite="", credential=key), peer)

    try:
        if invite:
            import threading
            threading.Thread(target=activate, daemon=True, name="relay-pair").start()
        else:
            activate()
    except Exception as exc:  # noqa: BLE001 启动失败清理资源后如实报错
        try:
            client.stop()
        except Exception:  # noqa: BLE001
            pass
        return _tool_result(False, f"register 启动失败：{exc}")
    return _tool_result(True, "方向 A 注册连接已启动（网关进程持有存活）",
                        member=member)


def register_tools(ctx) -> None:
    """向 gateway 注册 relay 私聊工具。"""
    tools = (
        ("bot_group_listen", bot_group_listen,
         "开启 bot-group 方向 B 监听（绑本机具体网卡地址），生成一次性配对链接",
         {"address": {"type": "string", "description": "绑定的本机网卡单播地址"},
          "port": {"type": "integer", "description": "监听端口"},
          "advertise": {"type": "string", "description": "对端可达地址/域名（可选）"},
          "engine_url": {"type": "string", "description": "为已注册引擎准备反向监听时指定其地址"},
          "no_tls": {"type": "boolean", "description": "禁用 TLS（仅隔离测试）"}},
         ["address", "port"]),
        ("bot_group_register", bot_group_register,
         "bot-group 方向 A 注册：连引擎 WS 端点（复用 #316 出站链路）",
         {"engine_url": {"type": "string", "description": "引擎地址（可选）"},
          "member": {"type": "string", "description": "成员名（可选）"},
          "credential": {"type": "string", "description": "已有节点凭据（可选）"},
          "invite": {"type": "string", "description": "首次接入的一次性邀请（可选）"}},
         []),
        ("bot_group_reconnect", bot_group_reconnect,
         "私聊说“重连”时，对已配对引擎重新拨号。多个配对时用已保存的引擎地址选择。",
         {"engine_url": {"type": "string", "description": "已配对的引擎地址"}}, []),
        ("bot_group_relay_status", bot_group_relay_status,
         "查看 bot-group relay 各配对的监听和拨号状态、停止原因及重试统计", {}, []),
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
