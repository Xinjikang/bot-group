"""req#171：来源 IP 白名单——存储、管理操作与 socket peer 判定。

两层独立：白名单管「谁可以访问」（来源），成员凭据管「你是谁」（身份）。
- 精确 IP 匹配（IPv4/IPv6），不自动放行网段；
- 回环 127.0.0.1/::1 恒放行（默认只允许本机）；
- 只信 socket peer IP，不读 X-Forwarded-For/X-Real-IP 等转发头；
- 无缓存：每次判定直查 SQLite，移除即时生效。
"""
from __future__ import annotations

import ipaddress
import sqlite3

from . import db

# 恒放行的本机回环地址（验收口径：127.0.0.1/::1 恒放行；127/8 其余与
# 其他网段一样走白名单——「精确 IP 匹配，不自动放行网段」）。
LOOPBACK = ("127.0.0.1", "::1")


def _normalize(ip: str) -> str | None:
    """解析并规范化 IP 文本（压缩 IPv6、去前导零）；非法输入返回 None。"""
    try:
        return str(ipaddress.ip_address(str(ip).strip()))
    except ValueError:
        return None


def allowed(conn: sqlite3.Connection, peer_ip: str | None) -> bool:
    """按 socket peer IP 判定来源是否可访问。

    peer_ip 必须是连接对端 socket 地址（request.client.host /
    websocket.client.host），绝不接受转发头内容——伪造头不能获得
    白名单资格或本机权限。
    """
    if peer_ip is None:
        return False
    norm = _normalize(peer_ip)
    if norm is None:
        return False
    if norm in LOOPBACK:
        return True
    return conn.execute(
        "SELECT 1 FROM source_ip_allowlist WHERE ip=? LIMIT 1", (norm,),
    ).fetchone() is not None


def list_entries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT ip, created_at FROM source_ip_allowlist ORDER BY created_at, id",
    ).fetchall()
    return [{"ip": r["ip"], "created_at": r["created_at"], "active": True} for r in rows]


def add(conn: sqlite3.Connection, ip: str) -> dict:
    """登记来源 IP（精确匹配）；回环恒放行无需登记；重复登记幂等。"""
    norm = _normalize(ip)
    if norm is None:
        raise ValueError("无效的 IP 地址")
    if norm in LOOPBACK:
        raise ValueError("本机回环地址始终放行，无需登记")
    conn.execute(
        "INSERT OR IGNORE INTO source_ip_allowlist(ip, created_at) VALUES(?,?)",
        (norm, db.now_iso()),
    )
    conn.commit()
    return {"ip": norm, "active": True}


def remove(conn: sqlite3.Connection, ip: str) -> bool:
    """移除登记项；无缓存，删除后立即生效（下一次判定即拒绝）。"""
    norm = _normalize(ip)
    if norm is None:
        raise ValueError("无效的 IP 地址")
    cur = conn.execute("DELETE FROM source_ip_allowlist WHERE ip=?", (norm,))
    removed = cur.rowcount > 0
    if removed:
        conn.commit()
    return removed
