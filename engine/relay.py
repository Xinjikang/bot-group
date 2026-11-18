"""远程 relay 节点：注册/凭据/连接代/心跳存活/离线点名状态机/MCP 指纹（req#168 task#315）。

数据模型两张表：
- relay_nodes：一成员一节点（首版）。连接凭据常驻节点、绑定成员、可单独吊销；
  连接代随重连递增、随派发下发（generation fencing）；心跳 90s TTL 只作存活判断。
- relay_offline_mentions：offline 点名三态状态机（唯一键 (message_id, member)）——
  offline_waiting / submitted / offline_expired，闸门反复检查只显示一条。

边界（设计 4/5 章）：机器级并发上限不承诺；此记录不是 adapter outbox；
发言位照常超时推进、不挂起 turn。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from . import db

# 心跳 TTL：只判存活，绝不据此判任务结束重跑（设计 4 章）。
HEARTBEAT_TTL_SECONDS = 90.0

# 离线点名三态（设计 4 章）。
MENTION_OFFLINE_WAITING = "offline_waiting"
MENTION_SUBMITTED = "submitted"
MENTION_OFFLINE_EXPIRED = "offline_expired"

# 连接代重连 +1 的上限（防无界增长；实际每重连一次即可，无上限风险，此为防御）。
MAX_GENERATION = 2**31 - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_nodes (
    member            TEXT PRIMARY KEY,          -- 成员 profile 名（首版一成员一节点）
    display_name      TEXT NOT NULL DEFAULT '',
    credential_hash   TEXT NOT NULL DEFAULT '',  -- 连接凭据 sha256（值本身不落库）
    credential_revoked INTEGER NOT NULL DEFAULT 0,
    generation        INTEGER NOT NULL DEFAULT 1, -- 连接代：重连 +1，随派发下发，拒旧代帧
    last_heartbeat    REAL,                       -- 最近心跳（monotonic 不可持久，存 wall time）
    state             TEXT NOT NULL DEFAULT 'offline',  -- online / offline
    code_version      TEXT NOT NULL DEFAULT '',   -- 插件上报的代码版本
    mcp_fingerprint   TEXT NOT NULL DEFAULT '',   -- 插件上报的随包 MCP 内容摘要
    mcp_config        TEXT NOT NULL DEFAULT '{}', -- 引擎侧节点配置：渲染 MCP 启动命令与 env
    expected_mcp_sha  TEXT NOT NULL DEFAULT '',   -- 引擎按渲染产物计算的指纹（configure 前复核）
    created_at        REAL NOT NULL,
    updated_at        REAL
);
CREATE TABLE IF NOT EXISTS relay_offline_mentions (
    message_id    INTEGER NOT NULL,
    member        TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'offline_waiting',
    submission_id TEXT,
    project       TEXT NOT NULL,
    thread_id     INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL,
    PRIMARY KEY (message_id, member)
);
"""


def initialize(conn) -> None:
    conn.executescript(SCHEMA)


def _now() -> float:
    return time.time()


def hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


def mcp_fingerprint(mcp_config: dict) -> str:
    """引擎侧指纹：对渲染产物（命令/env 规范 JSON）的 sha256——即对随包 MCP
    入口内容摘要的计算口径；插件上报其包内 MCP 入口的内容 hash，一致才算兼容。"""
    canonical = json.dumps(mcp_config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def register_node(conn, member: str, credential: str, *, display_name: str = "",
                  mcp_config: dict | None = None, now=None) -> dict:
    """注册/更新节点：凭据只存 hash。mcp_config 的 _package_sha 字段是
    随包 MCP 入口内容摘要（管理员记录的期望指纹），握手时与插件上报比对。"""
    now = _now() if now is None else now
    config_json = json.dumps(mcp_config or {}, sort_keys=True, ensure_ascii=False)
    expected = str((mcp_config or {}).get("_package_sha") or "")
    conn.execute(
        "INSERT INTO relay_nodes(member,display_name,credential_hash,generation,state,"
        "mcp_config,expected_mcp_sha,created_at,updated_at) "
        "VALUES(?,?,?,1,'offline',?,?,?,?) "
        "ON CONFLICT(member) DO UPDATE SET display_name=excluded.display_name,"
        "credential_hash=excluded.credential_hash,credential_revoked=0,"
        "mcp_config=excluded.mcp_config,expected_mcp_sha=excluded.expected_mcp_sha,"
        "updated_at=excluded.updated_at",
        (member, display_name, hash_credential(credential), config_json, expected, now, now))
    conn.commit()
    return read_node(conn, member)


def revoke_credential(conn, member: str, *, now=None) -> None:
    """单独吊销某成员连接凭据：现连立即失效，需管理员重发凭据。"""
    now = _now() if now is None else now
    conn.execute("UPDATE relay_nodes SET credential_revoked=1,updated_at=? WHERE member=?",
                 (now, member))
    conn.commit()


def verify_credential(conn, member: str, credential: str) -> bool:
    """常量时间比较：凭据绑定成员、未吊销才通过。"""
    row = read_node(conn, member)
    if row is None or row["credential_revoked"] or not row["credential_hash"]:
        return False
    return hmac.compare_digest(row["credential_hash"], hash_credential(credential))


def read_node(conn, member: str):
    return conn.execute("SELECT * FROM relay_nodes WHERE member=?", (member,)).fetchone()


def list_nodes(conn):
    return conn.execute("SELECT * FROM relay_nodes ORDER BY member").fetchall()


def generation_advance(conn, member: str, *, now=None) -> int:
    """重连后连接代 +1（旧代帧从此拒收）。"""
    now = _now() if now is None else now
    conn.execute(
        "UPDATE relay_nodes SET generation=(generation % ?) + 1, updated_at=? WHERE member=?",
        (MAX_GENERATION, now, member))
    conn.commit()
    row = read_node(conn, member)
    return row["generation"] if row else 1


def mark_online(conn, member: str, *, code_version: str, mcp_fingerprint: str,
                generation: int, now=None) -> None:
    conn = conn
    conn.execute(
        "UPDATE relay_nodes SET state='online',code_version=?,mcp_fingerprint=?,"
        "generation=?,last_heartbeat=?,updated_at=? WHERE member=?",
        (code_version, mcp_fingerprint, generation, _now() if now is None else now,
         _now() if now is None else now, member))
    conn.commit()


def heartbeat(conn, member: str, *, now=None) -> None:
    """心跳只更新存活槽；绝不据此判任务结束（设计 4 章）。"""
    conn.execute("UPDATE relay_nodes SET last_heartbeat=?,state='online' WHERE member=?",
                 (_now() if now is None else now, member))
    conn.commit()


def mark_offline(conn, member: str, *, now=None) -> None:
    conn.execute("UPDATE relay_nodes SET state='offline',updated_at=? WHERE member=?",
                 (_now() if now is None else now, member))
    conn.commit()


def is_online(conn, member: str) -> bool:
    row = read_node(conn, member)
    if row is None:
        return False
    # 心跳 TTL：90s 内无心跳即离线（只判存活）。
    if row["last_heartbeat"] is not None and time.time() - row["last_heartbeat"] > HEARTBEAT_TTL_SECONDS:
        mark_offline(conn, member)
        return False
    return row["state"] == "online"


def capacity_used(conn, member: str) -> int:
    """成员级占用：hermes_executions.state != 'terminal' 计数（设计 4 章）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM hermes_executions WHERE profile=? AND state!='terminal'",
        (member,)).fetchone()[0]


def capacity_limit(conn, member: str) -> int:
    """首版成员级并发上限：未配置则不限（节点级上限不承诺，设计 4 章）。"""
    row = read_node(conn, member)
    if row is None:
        return 0
    try:
        config = json.loads(row["mcp_config"] or "{}")
    except ValueError:
        return 0
    limit = config.get("_concurrency")
    return limit if isinstance(limit, int) and limit > 0 else 0


def at_capacity(conn, member: str) -> bool:
    limit = capacity_limit(conn, member)
    return limit > 0 and capacity_used(conn, member) >= limit


# ---------------- offline 点名三态状态机（设计 4 章） ----------------

def upsert_offline_mention(conn, *, message_id: int, member: str, project: str,
                           thread_id=None, now=None) -> str:
    """闸门发现节点离线且发言位仍有效：upsert offline_waiting（唯一键幂等，
    调度器反复检查只显示一条）。"""
    now = _now() if now is None else now
    conn.execute(
        "INSERT INTO relay_offline_mentions(message_id,member,state,project,thread_id,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id,member) DO UPDATE SET "
        "state=CASE WHEN relay_offline_mentions.state='submitted' THEN 'submitted' "
        "ELSE 'offline_waiting' END, updated_at=excluded.updated_at",
        (message_id, member, MENTION_OFFLINE_WAITING, project, thread_id, now, now))
    conn.commit()
    return read_offline_mention(conn, message_id, member)["state"]


def mark_submitted(conn, *, message_ids: list[int], member: str, submission_id: str,
                   now=None) -> int:
    """投递成功：本次注入快照内所有点名一起标 submitted（不能只标触发那条，
    否则同话题排队其他 @ 会停在等待态被误判未投递，设计 4 章）。"""
    now = _now() if now is None else now
    if not message_ids:
        return 0
    cur = conn.execute(
        "UPDATE relay_offline_mentions SET state=?,submission_id=?,updated_at=? "
        "WHERE member=? AND message_id IN (%s)" % ",".join("?" * len(message_ids)),
        (MENTION_SUBMITTED, submission_id, now, member, *message_ids))
    conn.commit()
    return cur.rowcount


def expire_mentions(conn, *, member=None, message_ids: list[int], now=None) -> int:
    """发言位到期且确认从未提交 → offline_expired（显示「成员离线，未投递」）。
    member=None 时对该消息集合内所有成员的点名执行（话题收口场景）。"""
    now = _now() if now is None else now
    if not message_ids:
        return 0
    placeholders = ",".join("?" * len(message_ids))
    query = ("UPDATE relay_offline_mentions SET state=?,updated_at=? "
             "WHERE state='offline_waiting' AND submission_id IS NULL "
             f"AND message_id IN ({placeholders})")
    args: list = [MENTION_OFFLINE_EXPIRED, now, *message_ids]
    if member is not None:
        query += " AND member=?"
        args.append(member)
    cur = conn.execute(query, args)
    conn.commit()
    return cur.rowcount


def read_offline_mention(conn, message_id: int, member: str):
    return conn.execute(
        "SELECT * FROM relay_offline_mentions WHERE message_id=? AND member=?",
        (message_id, member)).fetchone()


def waiting_mentions(conn, member: str):
    """该成员当前仍处 offline_waiting 的点名（重连后投递时取快照范围）。"""
    return conn.execute(
        "SELECT * FROM relay_offline_mentions WHERE member=? AND state='offline_waiting' "
        "ORDER BY message_id", (member,)).fetchall()


def expired_mentions(conn, *, thread_id=None, member=None):
    query = "SELECT * FROM relay_offline_mentions WHERE state='offline_expired'"
    args = []
    if thread_id is not None:
        query += " AND thread_id=?"
        args.append(thread_id)
    if member is not None:
        query += " AND member=?"
        args.append(member)
    query += " ORDER BY message_id"
    return conn.execute(query, args).fetchall()


# ---------------- MCP 随包配置渲染（验收 6） ----------------

def render_mcp_config(conn, member: str, *, engine_url: str = "") -> dict:
    """按节点配置渲染 MCP 启动命令与 env：占位符替换（{engine_url} 等引擎已知变量），
    经 relay 帧下发、插件原样转发。控制字段（_package_sha/_concurrency）不下发。"""
    row = read_node(conn, member)
    if row is None:
        return {}
    try:
        config = json.loads(row["mcp_config"] or "{}")
    except ValueError:
        return {}
    config = {key: value for key, value in config.items()
              if not key.startswith("_")}
    rendered = _substitute(config, {"engine_url": engine_url})
    return rendered


def _substitute(value, variables: dict):
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("{" + key + "}", replacement or "")
        return value
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, variables) for key, item in value.items()}
    return value


def new_credential() -> str:
    return secrets.token_urlsafe(32)
