"""req#172：引擎远程监听动态配置与真实生效（限来源）。

- 仅绑定主人选定的本机具体单播地址；禁 0.0.0.0 / :: 通配，禁失败回退通配；
  默认仅回环（config.DEFAULT_HOST=127.0.0.1）。
- 动态生效=另起一个 uvicorn Server（lifespan='off'，不跑第二份 queue worker/
  scanner/topic-dispatch/hud），与回环 Server 同端口不同地址共存（Windows
  实测：不同本地地址同端口可同时监听）。
- 监听状态从运行结果取得（server.started + sockets），不是配置值；
  绑定失败（端口占用/地址不可用）如实报错、回环保持可用、配置回滚。
- 与成员级来源绑定联动：未绑定来源只能在有有效管理员邀请时进入
  `/relay/ws` 首次认证；HTTP 等其它入口仍按来源绑定准入。
- 重启恢复上次监听配置：main() 启动时调 restore_on_startup()。
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time

from . import config, db

# settings 表（project=''）里的配置键。
_CONFIG_KEY = 'engine_remote_listen_address'

# 枚举排除项：通配、回环、链路本地 fe80::/10、组播 ff00::/8。
_EXCLUDED_NETS = (
    ipaddress.ip_network('0.0.0.0/32'),
    ipaddress.ip_network('::/128'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fe80::/10'),
    ipaddress.ip_network('ff00::/8'),
)

# 运行时状态（模块级单例；远程 Server 生命周期）。
_lock = threading.Lock()
_state: dict = {
    'server': None,      # uvicorn.Server | None
    'thread': None,      # threading.Thread | None
    'address': None,     # str | None（当前运行中的监听地址）
    'port': None,        # int | None
    'error': '',         # 最近一次启停错误
}


def read_config(conn) -> str | None:
    """读取保存的远程监听地址；None=未配置（仅回环）。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE project='' AND key=?",
        (_CONFIG_KEY,)).fetchone()
    return row['value'] if row else None


def save_config(conn, address: str | None) -> None:
    """保存（非 None）或清除（None）远程监听地址配置。"""
    if address is None:
        conn.execute("DELETE FROM settings WHERE project='' AND key=?", (_CONFIG_KEY,))
    else:
        conn.execute(
            "INSERT INTO settings(key, project, value, updated_at) VALUES(?,'',?,?) "
            "ON CONFLICT(key, project) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (_CONFIG_KEY, address, db.now_iso()))
    conn.commit()


def _network_label(name: str, addr) -> str:
    adapter = name.casefold()
    if addr.is_global:
        return '公网'
    if adapter in ('tailscale', 'tailscale0'):
        return 'Tailscale'
    if adapter.startswith('vethernet ('):
        return 'Hyper-V 本机虚拟机'
    if adapter.startswith(('以太网', 'ethernet', 'wi-fi', 'wlan', 'eth', 'enp', 'ens', 'eno', 'enx', 'wlp', '无线局域网')):
        return '局域网'
    return ''


def enumerate_interfaces() -> list[dict]:
    """枚举本机网卡具体单播地址（IPv4/IPv6），排除通配/回环/链路本地/组播。"""
    import psutil
    found: dict[str, dict] = {}
    for name, addrs in psutil.net_if_addrs().items():
        if name.casefold() == 'mihomo':
            continue
        for a in addrs:
            if a.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            if a.address == a.broadcast:
                continue
            try:
                addr = ipaddress.ip_address(a.address)
            except ValueError:
                continue
            if addr.is_multicast or addr.is_link_local or any(addr in net for net in _EXCLUDED_NETS):
                continue
            found[str(addr)] = {
                'address': str(addr),
                'version': addr.version,
                'family': 'IPv4' if addr.version == 4 else 'IPv6',
                'name': name,
                'network': _network_label(name, addr),
            }
    return sorted(found.values(), key=lambda x: (x['version'], x['address']))


def validate_address(address: str) -> str:
    """校验监听地址：必须是本机网卡具体单播地址（禁通配/回环/链路本地/组播）。"""
    try:
        addr = ipaddress.ip_address(str(address).strip())
    except ValueError:
        raise ValueError(f"无效的 IP 地址：{address}") from None
    if addr.version == 4 and addr == ipaddress.IPv4Address('0.0.0.0'):
        raise ValueError("禁止 0.0.0.0 通配监听，只绑定本机具体网卡地址")
    if addr.version == 6 and addr == ipaddress.IPv6Address('::'):
        raise ValueError("禁止 :: 通配监听，只绑定本机具体网卡地址")
    if any(addr in net for net in _EXCLUDED_NETS):
        raise ValueError("回环/链路本地/组播地址不可作为远程监听地址")
    available = {e['address'] for e in enumerate_interfaces()}
    if str(addr) not in available:
        raise ValueError("地址不是本机网卡的具体单播地址")
    return str(addr)


def _stop_locked() -> None:
    """停掉运行中的远程 Server（调用方持锁）。"""
    server, thread = _state['server'], _state['thread']
    _state['server'] = _state['thread'] = None
    _state['address'] = _state['port'] = None
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=15)


def _run_remote_server(address: str, port: int) -> None:
    """后台线程：跑远程监听 Server（lifespan='off'），运行结果回写 _state。"""
    import uvicorn
    from .main import app
    from . import relay_certificate
    try:
        pem, _ = relay_certificate.identity()
        server_config = relay_certificate.listener_config(
            app, pem, host=address, port=port, lifespan='off', log_level='warning',
            proxy_headers=False, timeout_graceful_shutdown=1)
    except (ValueError, OSError) as exc:
        _state['error'] = f'远程监听证书无法加载：{exc}'
        return
    server = uvicorn.Server(server_config)
    with _lock:
        _state['server'] = server
    try:
        server.run()
    except BaseException as exc:  # 启动失败路径（含 uvicorn sys.exit）：如实记录
        # 线程退出路径不抢 _lock（_stop_locked 可能正持锁 join 本线程）；
        # GIL 下单次赋值原子，由 ensure 的回滚分支统一读状态。
        _state['error'] = f"远程监听启动失败：{exc}"
        return
    # 注意：不在 _stop_locked 持锁 join 期间再抢锁（死锁）；先读 started 再按需写
    if not getattr(server, 'started', False):
        with _lock:
            _state['error'] = '远程监听启动失败（端口占用或地址不可用）'


def ensure(address: str | None, port: int | None = None) -> dict:
    """启用/停用远程监听并返回实际状态。

    address=None/''：停用远程监听（回退回环）。
    address=本机单播地址：校验→保存配置→起远程 Server；来源准入由中间件和端点执行。
    启动失败：配置回滚、回环保持可用、状态含错误信息。
    """
    port = port or config.port(None)
    with _lock:
        running = _state['address']
    if not address:
        with _lock:
            _stop_locked()
            _state['error'] = ''
        conn = db.get_connection()
        try:
            save_config(conn, None)
        finally:
            conn.close()
        return status()
    norm = validate_address(address)
    if norm == running and _state['server'] is not None and \
            getattr(_state['server'], 'started', False):
        return status()  # 已在监听同一地址，幂等
    # req#175 node2118 ①：不再要求「来源绑定预先就绪」——首次接入的
    # 来源绑定随一次性邀请兑换从零建立（非回环未绑定来源在端点第二层
    # 仅凭有效邀请可通过），不存在「先绑定再启用监听/先监听再首接」
    # 两步互为前置。监听启用后来源控制依然生效：非回环未绑定来源
    # 除有效邀请兑换外一律拒绝（SourceBindingMiddleware + relay 握手）。
    conn = db.get_connection()
    try:
        save_config(conn, norm)
    finally:
        conn.close()
    with _lock:
        _stop_locked()
        _state['error'] = ''
        _state['address'] = norm
        _state['port'] = port
        thread = threading.Thread(target=_run_remote_server, args=(norm, port),
                                  name='engine-remote-listen', daemon=True)
        _state['thread'] = thread
        thread.start()
    # 等待启动结果：轮询 started 或线程结束（成功=started True；失败=run 快速返回）
    deadline = time.monotonic() + 8
    while True:
        with _lock:
            started = bool(_state['server'] is not None and
                           getattr(_state['server'], 'started', False))
            alive = thread.is_alive()
        if started or not alive or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    with _lock:
        if not started:
            # 启动失败：回滚配置，保持仅回环
            conn = db.get_connection()
            try:
                save_config(conn, None)
            finally:
                conn.close()
            _state['address'] = _state['port'] = None
    return status()


def status() -> dict:
    """实际监听状态（从运行结果取得，不是配置值）。"""
    with _lock:
        server = _state['server']
        return {
            'remote_enabled': bool(server is not None and
                                   getattr(server, 'started', False)),
            'remote_address': _state['address'] if server is not None and
                              getattr(server, 'started', False) else None,
            'remote_port': _state['port'] if server is not None and
                           getattr(server, 'started', False) else None,
            'loopback': f"{config.DEFAULT_HOST}:{config.port(None)}",
            'error': _state['error'],
        }


def restore_on_startup(port: int | None = None) -> dict:
    """数据库初始化后恢复上次监听配置，绑定失败时仍可通过回环访问。"""
    conn = db.get_connection()
    try:
        saved = read_config(conn)
    finally:
        conn.close()
    if not saved:
        return status()
    try:
        return ensure(saved, port=port)
    except ValueError as exc:
        with _lock:
            _state['error'] = str(exc)
        return status()
