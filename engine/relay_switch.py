"""Coordinate member direction changes over the current authenticated transport."""
from contextlib import closing
import json
import threading

from fastapi import HTTPException

from . import db, listen_control, relay, relay_control, relay_dialer, relay_server

# Concurrent requests for one node must agree on which authenticated link is old.
_locks = tuple(threading.Lock() for _ in range(32))
RETURN_TIMEOUT = 45


def _expire_return(member, old, ticket):
    with _locks[hash(member) % len(_locks)]:
        if getattr(old, "return_ticket", None) is not ticket:
            return
        with closing(db.get_connection()) as conn, conn:
            cursor = conn.execute(
                "UPDATE relay_nodes SET switch_state='',switch_target='',switch_error=? "
                "WHERE member=? AND generation=? AND direction='dial' "
                "AND switch_state='switching' AND switch_target='listen'",
                ("云端未在时限内连入，本机继续使用原拨号方向", member,
                 relay_server._db_generation(old)))
        if cursor.rowcount and relay_server.active_link(member) is None:
            relay_dialer.start_dial(member)


def _prepare_dial(conn, member, endpoint, link):
    if not endpoint.get("pairing_token"):
        raise HTTPException(409, "云端拨号凭据缺失，请更新配对链接")
    prepare = endpoint.get("tls") and not relay_dialer.probe_endpoint(
        endpoint["host"], endpoint["port"], endpoint["fingerprint"], timeout=2)["ok"]
    try:
        if prepare:
            result = relay_control.request(
                link, kind="direction_listen", field="direction",
                spec={"fingerprint": endpoint["fingerprint"], "port": endpoint["port"]}, timeout=8,
                timeout_message="云端开启监听超时，请核对网关插件版本")
            if result.get("ready") is not True:
                raise HTTPException(409, "云端未确认监听就绪")
        session = relay_dialer.connect_endpoint(conn, member, endpoint)
        if session is None:
            failure = relay_dialer.dial_failure(member) or {}
            raise HTTPException(409, f"云端连接验证失败：{failure.get('error') or '认证失败'}")
    except Exception:
        if prepare and not link.closed:
            try:
                link.send_text(json.dumps({"relay": {"kind": "direction_cancel_listen",
                                                     "generation": link.generation}}))
            except ConnectionError:
                pass
        raise
    return session


def switch(member, target):
    if target not in ("listen", "dial"):
        raise HTTPException(400, "direction 必须是 listen 或 dial")
    with _locks[hash(member) % len(_locks)], closing(db.get_connection()) as conn:
        row = relay.read_node(conn, member)
        if row is None:
            raise HTTPException(404, "节点不存在")
        if row["credential_revoked"]:
            raise HTTPException(409, "节点凭据已吊销")
        old = relay_server.active_link(member)
        current = row["direction"]
        switching = row["switch_state"] == "switching"
        physical = "dial" if isinstance(old, relay_dialer.DialLink) else "listen"
        if old and switching and row["switch_target"] == target:
            if physical == target or getattr(old, "return_ticket", None):
                return {"member": member, "direction": target, "switch_state": "switching"}
        if old and current == target == physical and switching:
            if getattr(old, "return_timer", None):
                old.return_timer.cancel()
            old.return_ticket = None
            relay.begin_switch(conn, member, target)
            relay.complete_switch(conn, member)
            switching = False
        if current == target and not switching:
            if target == "dial" and old is None:
                relay_dialer.start_dial(member)
            return {"member": member, "direction": target, "switch_state": "",
                    "dial_state": relay_dialer._task_state(member)}

        session = None
        if target == "dial":
            endpoint = relay.read_dial_endpoint(conn, member)
            if endpoint is None:
                raise HTTPException(400, "拨号方向必须先保存插件监听端点（dial-endpoint）")
            if old and not isinstance(old, relay_dialer.DialLink):
                relay_dialer.stop_dial(member)
                session = _prepare_dial(conn, member, endpoint, old)

        try:
            relay.begin_switch(conn, member, target)
            if target == "dial":
                relay_dialer.start_dial(member, initial=session)
                session = None
            elif isinstance(old, relay_dialer.DialLink):
                ticket = old.return_ticket = object()
                timer = threading.Timer(RETURN_TIMEOUT, _expire_return, args=(member, old, ticket))
                old.return_timer = timer
                timer.daemon = True
                timer.start()
                try:
                    frame = {"relay": {"kind": "direction_dial", "generation": old.generation}}
                    endpoint = relay.read_dial_endpoint(conn, member) or {}
                    listener = listen_control.status()
                    if (listener["remote_enabled"] and
                            relay.verify_credential(conn, member, endpoint.get("pairing_token") or "")):
                        frame["return_port"] = listener["remote_port"]
                    old.send_text(json.dumps(frame))
                except ConnectionError:
                    pass  # The gateway may already have replaced the old connection.
            else:
                relay_dialer.stop_dial(member)
        finally:
            if session is not None:
                session[0].close()
    return {"member": member, "direction": target, "switch_state": "switching",
            "dial_state": relay_dialer._task_state(member)}
