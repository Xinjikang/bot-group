"""bot-group relay CLI 子命令（req#173 验收 1/2）：listen / register / status。

- CLI 入口身份核验：按授权本地 OS 身份——插件目录所有者（POSIX st_uid）
  或配置的授权本地用户（BOT_GROUP_RELAY_ALLOWED_USERS 逗号分隔）；
- 与私聊入口共用签发/监听/兑换逻辑与审计（pairing/listen 模块）；
- 非交互终端：listen 成功后链接写 0600 文件，不打印到 stdout。
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path


def _authorized_local_user() -> bool:
    """CLI 授权本地 OS 身份核验：目录所有者或显式白名单。"""
    allowed = [name.strip() for name in
               (os.environ.get("BOT_GROUP_RELAY_ALLOWED_USERS") or "").split(",")
               if name.strip()]
    current = getpass.getuser()
    if current in allowed:
        return True
    if os.name == "posix" and allowed == []:
        # 缺省：插件目录所有者（安装者）可操作
        try:
            stat = (Path(__file__).resolve().parent / "__init__.py").stat()
            if stat.st_uid == os.getuid():
                return True
        except (AttributeError, OSError):
            return False
        return False
    # Windows：无 st_uid 语义且未配置白名单 → 拒绝（显式配置才放行）
    return False


def _require_authorized() -> None:
    if not _authorized_local_user():
        print("bot-group relay: unauthorized local user (配置 "
              "BOT_GROUP_RELAY_ALLOWED_USERS 显式授权)", file=sys.stderr)
        sys.exit(1)


def register_cli(subparsers) -> None:
    """向 CLI 注册子命令（ctx.register_cli_command 的 setup_fn）。"""
    parser = subparsers.add_parser("bot-group-relay", help="bot-group relay 插件控制")
    sub = parser.add_subparsers(dest="command", required=True)
    p_listen = sub.add_parser("listen", help="方向 B：绑定本机具体网卡地址监听")
    p_listen.add_argument("--address", required=True, help="绑定的本机网卡单播地址")
    p_listen.add_argument("--port", type=int, required=True, help="监听端口")
    p_listen.add_argument("--advertise", default="", help="对端可达地址/域名（公网映射场景）")
    p_listen.add_argument("--member", default="", help="成员名（缺省 HERMES_PROFILE）")
    p_listen.add_argument("--cert", default="", help="TLS 证书 PEM（缺省自动生成自签）")
    p_listen.add_argument("--key", default="", help="TLS 私钥 PEM")
    p_listen.add_argument("--no-tls", action="store_true", help="禁用 TLS（仅隔离测试）")
    p_listen.add_argument("--no-proxy", action="store_true", help="不启动 MCP loopback 代理")
    p_register = sub.add_parser("register", help="方向 A：连引擎 WS 端点注册（复用 #316）")
    p_register.add_argument("--engine-url", default="", help="引擎地址（缺省 BOT_GROUP_ENGINE_URL）")
    p_register.add_argument("--member", default="", help="成员名")
    p_register.add_argument("--credential", default="", help="节点凭据")
    sub.add_parser("status", help="当前监听/配对状态（脱敏）")


def dispatch(args) -> int:
    """CLI 入口（ctx.register_cli_command 的 handler_fn）。"""
    _require_authorized()
    from . import pairing
    if args.command == "status":
        print(json.dumps(pairing.pairing_metadata(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "listen":
        from . import listen as listen_mod
        try:
            result = listen_mod.listen_serve(
                address=args.address, port=args.port, advertise=args.advertise,
                member=args.member,
                cert_path=args.cert or None, key_path=args.key or None,
                tls=not args.no_tls,
                link_to_file=not sys.stdout.isatty())
        except (ValueError, RuntimeError) as exc:
            print(f"bot-group relay listen failed: {exc}", file=sys.stderr)
            return 1
        # 交互终端：打印脱敏摘要（链接不落 stdout——交互场景用户本人在终端前，
        # 链接只写 0600 文件，输出仅提示文件路径）
        summary = {k: v for k, v in result.items() if k not in ("pairing_url",)}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "register":
        engine_url = args.engine_url or os.environ.get("BOT_GROUP_ENGINE_URL") or ""
        member = args.member or os.environ.get("BOT_GROUP_RELAY_MEMBER") or ""
        credential = args.credential or os.environ.get("BOT_GROUP_RELAY_CREDENTIAL") or ""
        if not engine_url or not member or not credential:
            print("bot-group relay register: 需要 engine-url/member/credential",
                  file=sys.stderr)
            return 1
        from . import config as relay_config
        from . import relay as relay_mod
        cfg = relay_config.RelayConfig(engine_url=engine_url, member=member,
                                       credential=credential, mcp_python="",
                                       home=os.environ.get("HERMES_HOME") or
                                       str(Path.home() / ".hermes"))
        client = relay_mod.RelayClient(cfg)
        client.start()  # 复用 #316 出站链路；后台线程维护连接
        print(json.dumps({"status": "registering", "member": member}, ensure_ascii=False))
        return 0
    return 2
