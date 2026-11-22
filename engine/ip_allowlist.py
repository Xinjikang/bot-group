"""req#171：成员级来源绑定——数据、判定与按成员管理。

主人 12417/bot-36 12419 口径：来源 IP 按成员绑定（一成员多 IP、同 IP 多成员），
节点与 IP 一一对应可见，relay 握手核对 peer IP 属于该成员绑定集合（防交换
来源冒用）。凭据管身份、来源绑定管来源，两层独立。

- 精确 IP 匹配（IPv4/IPv6），不自动放行网段；
- 回环 127.0.0.1/::1 恒放行（本机访问不变）；
- 只信 socket peer IP，不读 X-Forwarded-For/X-Real-IP 等转发头；
- 归一化：IPv4-mapped IPv6（::ffff:x.x.x.x）折为 IPv4 文本、IPv6 压缩；
- 无缓存：每次判定直查 SQLite，移除/变更即时生效。
"""
from __future__ import annotations

import ipaddress
import sqlite3

from . import db

# 恒放行的本机回环地址（验收口径：127.0.0.1/::1 恒放行；127/8 其余与
# 其他网段一样走绑定——「精确 IP 匹配，不自动放行网段」）。
LOOPBACK = ("127.0.0.1", "::1")

# 成员级绑定表（schema.sql）：member + ip 联合主键——一成员多 IP、
# 同 IP 多成员均可表达。
_TABLE = "relay_source_bindings"


def _normalize(ip: str) -> str | None:
    """解析并规范化 IP 文本；非法输入返回 None。

    - IPv4-mapped IPv6（::ffff:192.0.2.1）折为 IPv4 文本（socket peer 在
      dual-stack 监听下可能给出映射形式）；
    - 普通 IPv6 按 RFC 压缩、去前导零。
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return str(addr)


def _is_loopback(peer_ip: str | None) -> bool:
    if peer_ip is None:
        return False
    norm = _normalize(peer_ip)
    return norm is not None and norm in LOOPBACK


def source_allowed(conn: sqlite3.Connection, peer_ip: str | None) -> bool:
    """第一层（ASGI 中间件，身份解析之前）：peer IP 属于「已绑定并集」或回环。

    只能校并集——此层拿不到 member；member 级严格核对在第二层。
    """
    if _is_loopback(peer_ip):
        return True
    norm = _normalize(peer_ip or "")
    if norm is None:
        return False
    return conn.execute(
        f"SELECT 1 FROM {_TABLE} WHERE ip=? LIMIT 1", (norm,),
    ).fetchone() is not None


def member_allowed(conn: sqlite3.Connection, member: str, peer_ip: str | None) -> bool:
    """第二层（身份解析之后/relay 握手）：peer IP 必须属于该 member 的绑定集合。

    回环恒放行；否则严格核对 member→ip 绑定——B 成员凭据从 A 成员已绑定
    IP 发起连接/请求在此被拒（防交换来源冒用）。
    """
    if _is_loopback(peer_ip):
        return True
    norm = _normalize(peer_ip or "")
    if norm is None:
        return False
    return conn.execute(
        f"SELECT 1 FROM {_TABLE} WHERE member=? AND ip=? LIMIT 1", (member, norm),
    ).fetchone() is not None


def list_bindings(conn: sqlite3.Connection) -> list[dict]:
    """按成员回读绑定（节点记录按成员保存的 IP 集合）。"""
    rows = conn.execute(
        f"SELECT member, ip, created_at FROM {_TABLE} ORDER BY member, created_at, ip",
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["member"], []).append(r["ip"])
    return [{"member": member, "ips": ips} for member, ips in grouped.items()]


def set_bindings(conn: sqlite3.Connection, member: str, ips: list[str]) -> dict:
    """按成员登记/更新来源 IP 集合（整体替换该成员的绑定）。

    回环与非法 IP 拒绝；精确 IP 匹配。空集合=移除该成员全部绑定。
    """
    if not member or not member.strip():
        raise ValueError("member 必填")
    member = member.strip()
    normalized: list[str] = []
    for ip in ips or []:
        norm = _normalize(ip)
        if norm is None:
            raise ValueError(f"无效的 IP 地址：{ip}")
        if norm in LOOPBACK:
            raise ValueError("本机回环地址始终放行，无需登记")
        if norm not in normalized:
            normalized.append(norm)
    conn.execute(f"DELETE FROM {_TABLE} WHERE member=?", (member,))
    conn.executemany(
        f"INSERT INTO {_TABLE}(member, ip, created_at) VALUES(?,?,?)",
        [(member, ip, db.now_iso()) for ip in normalized])
    conn.commit()
    return {"member": member, "ips": normalized}


def add_binding(conn: sqlite3.Connection, member: str, ip: str,
                *, commit: bool = True) -> dict:
    """按 (member, ip) 原子幂等添加单条绑定（#320 manager node2030）。

    与 set_bindings 同口径校验（归一化/回环拒绝/格式），但只插入、
    不清除该成员已有绑定——并发页面各自添加不同地址时互不覆盖；
    重复添加同地址仅保留一条（INSERT OR IGNORE，复合主键去重）。
    """
    if not member or not member.strip():
        raise ValueError("member 必填")
    member = member.strip()
    norm = _normalize(ip)
    if norm is None:
        raise ValueError(f"无效的 IP 地址：{ip}")
    if norm in LOOPBACK:
        raise ValueError("本机回环地址始终放行，无需登记")
    conn.execute(
        f"INSERT OR IGNORE INTO {_TABLE}(member, ip, created_at) VALUES(?,?,?)",
        (member, norm, db.now_iso()))
    if commit:
        conn.commit()
    return {"member": member, "ip": norm}


def remove_binding(conn: sqlite3.Connection, member: str, ip: str) -> bool:
    """移除单条绑定；无缓存，删除后立即生效。"""
    norm = _normalize(ip)
    if norm is None:
        raise ValueError("无效的 IP 地址")
    cur = conn.execute(f"DELETE FROM {_TABLE} WHERE member=? AND ip=?", (member, norm))
    removed = cur.rowcount > 0
    if removed:
        conn.commit()
    return removed
