"""bot-group relay CLI 子命令（req#173 验收 1/2）：listen / register / status。

- CLI 入口身份核验：按授权本地 OS 身份——插件目录所有者（POSIX st_uid）
  或配置的授权本地用户（BOT_GROUP_RELAY_ALLOWED_USERS 逗号分隔）；
- 与私聊入口共用签发/监听/兑换逻辑与审计（pairing/listen 模块）；
- 非交互终端：listen 成功后链接写 0600 文件，不打印到 stdout。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _os_kernel_identity() -> str:
    """按平台取内核 OS 主体身份（manager node2074/12711：不以可伪造的
    USERNAME/USER 环境变量为准）：
    - Windows：secur32 GetUserNameExW（登录会话主体，环境变量无法伪造；
      manager node2084 实测该导出在 secur32 而非 advapi32——advapi32 无
      此导出，原实现恒返回空）；
    - POSIX：内核 uid → pwd 主体名。
    取不到返回空串 → 白名单匹配失败（拒绝）。"""
    if os.name == "nt":
        import ctypes
        try:
            buffer = ctypes.create_unicode_buffer(256)
            size = ctypes.c_uint(256)
            if ctypes.windll.secur32.GetUserNameExW(2, buffer, ctypes.byref(size)):
                return buffer.value
        except Exception:  # noqa: BLE001 API 不可用按未授权处理
            return ""
        return ""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001
        return ""


def _authorized_local_user() -> bool:
    """CLI 授权本地 OS 身份核验：内核主体对白名单，目录所有者兜底。"""
    allowed = [name.strip() for name in
               (os.environ.get("BOT_GROUP_RELAY_ALLOWED_USERS") or "").split(",")
               if name.strip()]
    current = _os_kernel_identity()
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


def register_cli(parser) -> None:
    """向 CLI 注册子命令（ctx.register_cli_command 的 setup_fn）。

    manager 12801：宿主 _attach_plugin_cli_command 已为本插件建好专属
    ArgumentParser（hermes bot-group-relay ...）再调 setup_fn——首步必须
    在传入 parser 上 add_subparsers（ArgumentParser 无 add_parser 方法，
    把 parser 当 subparsers 用会 AttributeError）。子命令 dest=command
    挂在插件 parser 命名空间内，独立于宿主顶层参数。"""
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
    p_listen.add_argument("--proxy-port", type=int, default=0,
                          help="MCP loopback 代理端口（缺省自动分配）")
    p_register = sub.add_parser("register", help="方向 A：连引擎 WS 端点注册（复用 #316）")
    p_register.add_argument("--engine-url", default="", help="引擎地址（缺省 BOT_GROUP_ENGINE_URL）")
    p_register.add_argument("--member", default="", help="成员名")
    p_register.add_argument("--credential", default="", help="节点凭据")
    sub.add_parser("status", help="当前监听/配对状态（脱敏）")


def _run_foreground(cleanup) -> int:
    """前台存活（manager node2060 ①：CLI 明示前台运行）——阻塞等待
    SIGINT/SIGTERM，收到信号执行清理后 rc=0。cleanup 无异常。
    测试可用合成 cleanup 验证信号语义。"""
    import signal
    import threading
    stopped = threading.Event()
    previous = {}

    def _handle(signum, _frame):
        previous.pop(signum, None)
        cleanup()
        stopped.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _handle)
        except (ValueError, OSError):
            pass  # 非主线程注册失败：前台语义由主线程承担
    try:
        print("bot-group relay: 前台运行中（Ctrl-C 停止）", file=sys.stderr)
        while not stopped.wait(1.0):
            pass
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
    return 0


def dispatch(args) -> int:
    """CLI 入口（ctx.register_cli_command 的 handler_fn）。"""
    _require_authorized()
    from . import pairing
    if args.command == "status":
        print(json.dumps(pairing.pairing_metadata(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "listen":
        from . import listen as listen_mod
        interactive = sys.stdout.isatty()
        try:
            result = listen_mod.listen_serve(
                address=args.address, port=args.port, advertise=args.advertise,
                member=args.member,
                cert_path=args.cert or None, key_path=args.key or None,
                tls=not args.no_tls,
                proxy_port=int(args.proxy_port or 0),
                no_proxy=args.no_proxy,
                # 交互终端用户本人在场：配对材料直接输出（manager node2060 ①
                # 交互可取得配对材料）；非交互不落 stdout、写 0600 文件
                link_to_file=not interactive)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"bot-group relay listen failed: {exc}", file=sys.stderr)
            return 1
        summary = {k: v for k, v in result.items()
                   if k in ("status", "address", "port", "advertise", "scheme",
                            "fingerprint_short", "expires_in_seconds",
                            "proxy_port", "proxy_url")}
        if interactive:
            # 交互终端：直接给出配对材料（链接 + 提示），前台运行
            summary["pairing_url"] = result["pairing_url"]
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return _run_foreground(lambda: result["_server"].stop())
        # 非交互：链接已写 0600 文件（不落 stdout），打印摘要含文件路径
        summary["link_file"] = result.get("link_file", "")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return _run_foreground(lambda: result["_server"].stop())
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
        try:
            client.start()  # 复用 #316 出站链路；后台线程维护连接
        except Exception as exc:  # noqa: BLE001 启动失败清理后如实报错
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
            print(f"bot-group relay register failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"status": "registering", "member": member},
                         ensure_ascii=False))
        return _run_foreground(client.stop)  # 前台运行：连接存活到 Ctrl-C
    return 2
