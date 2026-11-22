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
import logging
import re
import secrets
import time

from . import db

logger = logging.getLogger(__name__)

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
    -- req#173 双向接入：连接方向与切换状态机（先断后建，动作驱动不设计时器）
    direction         TEXT NOT NULL DEFAULT 'listen',  -- listen=引擎监听(方向A) / dial=引擎拨号(方向B)
    dial_endpoint     TEXT NOT NULL DEFAULT '',   -- 插件监听端点 JSON：{host,port,tls,fingerprint}
    proxy_port        INTEGER NOT NULL DEFAULT 0,  -- 插件 loopback MCP 代理端口（方向B随包MCP GROUP_ENGINE_URL指向）
    mcp_verify_state  TEXT NOT NULL DEFAULT '',   -- MCP 只读验证（manager node2102）：''待验/ok/failed；换代作废
    mcp_verify_error  TEXT NOT NULL DEFAULT '',   -- 验证失败原因（脱敏：阶段/状态码/拒绝原因，不含凭据）
    switch_state      TEXT NOT NULL DEFAULT '',   -- ''=无切换 / 'switching'=切换中（暂停派发）
    switch_target     TEXT NOT NULL DEFAULT '',   -- 切换目标方向（随节点持久化，重启可读回）
    switch_error      TEXT NOT NULL DEFAULT '',   -- 最近一次切换/拨号失败原因（页面显示）
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
-- req#175 首次接入（方向 A=云端主动连电脑）：引擎签发的一次性邀请。
-- 兑换原子单次、过期失效；兑换后按 hello 上报的远端成员建独立节点。
CREATE TABLE IF NOT EXISTS relay_invitations (
    token_hash   TEXT PRIMARY KEY,
    expires_at   REAL NOT NULL,
    redeemed     INTEGER NOT NULL DEFAULT 0,
    redeemed_by  TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    redeemed_at  REAL
);
CREATE TABLE IF NOT EXISTS relay_invitation_attempts (
    pairing_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE REFERENCES relay_invitations(token_hash),
    member TEXT,
    peer_ip TEXT,
    proof_hash TEXT,
    credential_commitment TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0,
    failure_stage TEXT
);
CREATE INDEX IF NOT EXISTS relay_invitation_expiry ON relay_invitations(expires_at);
CREATE TABLE IF NOT EXISTS relay_renewals (
    token_hash TEXT PRIMARY KEY REFERENCES relay_invitations(token_hash) ON DELETE CASCADE,
    target_member TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_node_identities (
    member TEXT PRIMARY KEY REFERENCES relay_nodes(member),
    host_profile TEXT NOT NULL,
    pairing_id TEXT NOT NULL UNIQUE,
    device_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS relay_host_profile ON relay_node_identities(host_profile);
"""


def initialize(conn) -> None:
    from . import relay_pairing
    conn.executescript(SCHEMA + relay_pairing.SCHEMA)
    # Previews from older versions have no further use for a saved candidate.
    with conn:
        conn.execute('UPDATE relay_dial_attempts SET candidate=NULL WHERE candidate IS NOT NULL')
    if 'failure_stage' not in {row[1] for row in conn.execute('PRAGMA table_info(relay_invitation_attempts)')}:
        conn.execute('ALTER TABLE relay_invitation_attempts ADD COLUMN failure_stage TEXT')
        conn.commit()


def _now() -> float:
    return time.time()


def hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


# ---------------- req#175 一次性邀请（方向 A=云端主动连电脑） ----------------

INVITATION_TTL_SECONDS = 3600.0   # 一次性邀请 1 小时内兑换有效


def _cancel_renewals(conn, member):
    conn.execute("UPDATE relay_invitation_attempts SET cancelled=1 WHERE token_hash IN "
                 "(SELECT r.token_hash FROM relay_renewals r JOIN relay_invitations i USING(token_hash) "
                 "WHERE r.target_member=? AND i.redeemed=0)", (member,))


def issue_invitation(conn, *, ttl_seconds: float = INVITATION_TTL_SECONDS,
                     now=None, renew_member=None) -> dict:
    """引擎签发一次性邀请（管理员动作）：返回
    {invite_token, engine_url_scheme_placeholder? 不含}——调用方组装展示。
    只存 token hash；兑换原子单次、过期失效。"""
    token, pairing_id = secrets.token_urlsafe(32), secrets.token_hex(16)
    now = _now() if now is None else now
    token_hash = hash_credential(token)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # Retain recent terminal results for retries without growing the bootstrap ledger indefinitely.
        cutoff = now - INVITATION_TTL_SECONDS
        conn.execute("DELETE FROM relay_renewals WHERE token_hash IN "
                     "(SELECT token_hash FROM relay_invitations WHERE expires_at<?)", (cutoff,))
        conn.execute("DELETE FROM relay_invitation_attempts WHERE token_hash IN "
                     "(SELECT token_hash FROM relay_invitations WHERE expires_at<?)", (cutoff,))
        conn.execute("DELETE FROM relay_invitations WHERE expires_at<?", (cutoff,))
        conn.execute("INSERT INTO relay_invitations (token_hash,expires_at,created_at) VALUES (?,?,?)",
                     (token_hash, now + ttl_seconds, now))
        conn.execute("INSERT INTO relay_invitation_attempts (pairing_id,token_hash) VALUES (?,?)",
                     (pairing_id, token_hash))
        if renew_member is not None:
            node = read_node(conn, renew_member)
            if not node or node['direction'] != 'listen' or node['switch_state']:
                raise InvitationConflict('请选择连接方向为「云端连本机」且方向切换已完成的节点')
            _cancel_renewals(conn, renew_member)
            conn.execute("INSERT INTO relay_renewals(token_hash,target_member) VALUES (?,?)",
                         (token_hash, renew_member))
    return {"invite_token": token, "pairing_id": pairing_id, "expires_at": now + ttl_seconds}


def invitation_open(conn) -> bool:
    """Only live, unconsumed invitations open the unbound WS bootstrap path."""
    return conn.execute(
        "SELECT 1 FROM relay_invitations i JOIN relay_invitation_attempts a USING(token_hash) "
        "WHERE NOT EXISTS (SELECT 1 FROM relay_renewals r WHERE r.token_hash=i.token_hash) "
        "AND i.redeemed=0 AND a.cancelled=0 AND a.failure_stage IS NULL AND i.expires_at>=? LIMIT 1",
        (_now(),)).fetchone() is not None


def redeem_invitation(conn, token: str, member: str, *, now=None,
                      commit: bool = True) -> bool:
    """Atomically consume an unexpired invitation; caller may defer commit."""
    if not token:
        return False
    now = _now() if now is None else now
    changed = _redeem_invitation_hash(conn, hash_credential(token), member, now)
    if commit:
        conn.commit()
    return changed


def _redeem_invitation_hash(conn, token_hash, member, now):
    return conn.execute(
        "UPDATE relay_invitations SET redeemed=1,redeemed_by=?,redeemed_at=? "
        "WHERE token_hash=? AND redeemed=0 AND expires_at>=? AND NOT EXISTS "
        "(SELECT 1 FROM relay_invitation_attempts a WHERE a.token_hash=? "
        "AND (a.cancelled=1 OR a.failure_stage IS NOT NULL))",
        (member, now, token_hash, now, token_hash)).rowcount == 1


_INVITATION = ("SELECT a.*, i.expires_at, i.redeemed, r.target_member, COALESCE(r.target_member,n.member) "
               "AS node_member FROM relay_invitation_attempts a JOIN relay_invitations i USING(token_hash) "
               "LEFT JOIN relay_renewals r USING(token_hash) LEFT JOIN relay_node_identities n USING(pairing_id) WHERE ")


class InvitationConflict(ValueError):
    pass


def _invitation_view(row, now):
    if row is None:
        return None
    state = ('cancelled' if row['cancelled'] else 'confirmed' if row['redeemed'] else
             'expired' if row['expires_at'] < now else 'failed' if row['failure_stage'] else
             'pending_confirmation' if row['member'] else 'waiting')
    visible = state in ('pending_confirmation', 'confirmed', 'failed')
    view = {'pairing_id': row['pairing_id'], 'state': state, 'direction': 'listen',
            'expires_at': row['expires_at'], 'member': row['member'] if visible else None,
            'peer_ip': row['peer_ip'] if visible else None}
    if state == 'failed':
        view.update(stage=row['failure_stage'], reason='name_conflict')
    if state == 'confirmed':
        view['node_member'] = row['node_member'] or row['member']
    return view


def _refuse_invitation(conn, pairing_id, member, peer_ip, stage):
    conn.execute('UPDATE relay_invitation_attempts SET member=?,peer_ip=?,failure_stage=?, '
                 'proof_hash=NULL,credential_commitment=NULL WHERE pairing_id=?',
                 (member, peer_ip, stage, pairing_id))


def read_invitation(conn, pairing_id):
    row = conn.execute(_INVITATION + 'a.pairing_id=?', (pairing_id,)).fetchone()
    view = _invitation_view(row, _now())
    if view is not None and view['state'] == 'confirmed':
        node = read_node(conn, view['node_member'])
        view['connected'] = bool(node and node['state'] == 'online' and not node['credential_revoked'])
    return view


def offer_invitation(conn, token, member, peer_ip, proof, commitment):
    """Record immutable handshake identity; the verifier grants no access until confirmation."""
    hex64 = r'[0-9a-f]{64}'
    if not (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', member)
            and re.fullmatch(hex64, proof) and re.fullmatch(hex64, commitment)):
        return None
    token_hash, proof_hash = hash_credential(token), hash_credential(proof)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        now = _now()
        row = conn.execute(_INVITATION + 'a.token_hash=?', (token_hash,)).fetchone()
        if row is None or row['cancelled'] or row['failure_stage'] or row['expires_at'] < now:
            return None
        if row['member'] is not None:
            if (row['member'] != member or row['peer_ip'] != peer_ip
                    or not hmac.compare_digest(row['proof_hash'], proof_hash)
                    or not hmac.compare_digest(row['credential_commitment'], commitment)):
                return None
            if row['redeemed']:
                node = read_node(conn, row['node_member'] or member)
                if not node or node['credential_revoked'] or not hmac.compare_digest(node['credential_hash'], commitment):
                    return None
            return _invitation_view(row, now)
        if row['redeemed']:
            return None
        target = row['target_member']
        if target:
            from . import ip_allowlist
            node = read_node(conn, target)
            if (not node or node['host_profile'] != member or node['direction'] != 'listen'
                    or node['switch_state'] or not ip_allowlist.member_allowed(conn, target, peer_ip)
                    or paired_device(conn, commitment)):
                return None
            if not _redeem_invitation_hash(conn, token_hash, target, now):
                return None
            conn.execute("UPDATE relay_nodes SET credential_hash=?,credential_revoked=0,updated_at=? WHERE member=?",
                         (commitment, now, target))
        elif db.is_builtin_user(member) or paired_device(conn, commitment):
            _refuse_invitation(conn, row['pairing_id'], member, peer_ip, 'identity')
            return None
        conn.execute('UPDATE relay_invitation_attempts SET member=?,peer_ip=?,proof_hash=?,credential_commitment=? '
                     'WHERE pairing_id=?', (member, peer_ip, proof_hash, commitment, row['pairing_id']))
        return _invitation_view(conn.execute(_INVITATION + 'a.token_hash=?', (token_hash,)).fetchone(), now)


def decide_invitation(conn, pairing_id, *, confirm):
    """Serialize admin decisions and promote exactly one verified offer."""
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        now = _now()
        row = conn.execute(_INVITATION + 'a.pairing_id=?', (pairing_id,)).fetchone()
        view = _invitation_view(row, now)
        if view is None:
            return None
        state = view['state']
        if confirm:
            if state == 'confirmed' or state == 'failed':
                return view
            if state != 'pending_confirmation':
                raise InvitationConflict('邀请未到达待确认状态或已失效')
            if db.is_builtin_user(row['member']) or paired_device(conn, row['credential_commitment']):
                _refuse_invitation(conn, pairing_id, row['member'], row['peer_ip'], 'confirmation')
                return _invitation_view(conn.execute(_INVITATION + 'a.pairing_id=?', (pairing_id,)).fetchone(), now)
            if not _redeem_invitation_hash(conn, row['token_hash'], row['member'], now):
                raise InvitationConflict('邀请已使用或已失效')
            register_paired_node(conn, pairing_id, row['member'], row['credential_commitment'],
                                 device_key=row['credential_commitment'], direction='listen', peer_ip=row['peer_ip'])
        else:
            if state in ('cancelled', 'expired'):
                return view
            if state == 'confirmed':
                raise InvitationConflict('节点已登记，不能取消邀请；请在节点列表管理凭据')
            conn.execute('UPDATE relay_invitation_attempts SET cancelled=1,member=NULL,peer_ip=NULL,proof_hash=NULL, '
                         'credential_commitment=NULL WHERE pairing_id=?', (pairing_id,))
        return _invitation_view(conn.execute(_INVITATION + 'a.pairing_id=?', (pairing_id,)).fetchone(), now)


def remote_member_conflict(conn, member: str) -> bool:
    """Protect both engine routing identities and existing host profiles."""
    return member == "user" or conn.execute(
        "SELECT 1 FROM relay_nodes WHERE member=? "
        "UNION ALL SELECT 1 FROM members WHERE profile=? "
        "UNION ALL SELECT 1 FROM bots WHERE profile=? "
        "UNION ALL SELECT 1 FROM project_members WHERE profile=? LIMIT 1",
        (member, member, member, member)).fetchone() is not None


def paired_device(conn, device_key):
    return conn.execute('SELECT 1 FROM relay_node_identities WHERE device_key=? '
                        'UNION ALL SELECT 1 FROM relay_nodes WHERE credential_hash=? LIMIT 1',
                        (device_key, device_key)).fetchone() is not None


def register_paired_node(conn, pairing_id, profile, credential_hash, *, device_key, direction, peer_ip='', endpoint=None):
    """Allocate the member handle inside the same write transaction as confirmation."""
    from . import discovery
    base = 'remote-' + profile[:48]
    member = base
    number = 1
    while remote_member_conflict(conn, member):
        number += 1
        member = f'{base}-{number}'
    label = f'{profile}（远程）' if number == 1 else f'{profile}（远程 {number}）'
    _register_remote_node_hash(conn, member, credential_hash, direction=direction, peer_ip=peer_ip,
                               endpoint=endpoint, commit=False)
    conn.execute('UPDATE relay_nodes SET display_name=? WHERE member=?', (label, member))
    conn.execute('INSERT INTO relay_node_identities(member,host_profile,pairing_id,device_key) VALUES(?,?,?,?)',
                 (member, profile, pairing_id, device_key))
    bot = discovery.insert_bot(conn, 'relay', profile, member, 'hermes', label)
    conn.execute("INSERT INTO members(profile,display_name,bot_id,source,created_at) VALUES(?,?,?,'api',?)",
                 (member, label, bot['bot_id'], db.now_iso()))


def authenticated_member(conn, profile, credential):
    """Resolve a wire profile only in combination with its active credential."""
    if not profile or not credential:
        return None
    rows = conn.execute('SELECT n.member FROM relay_nodes n LEFT JOIN relay_node_identities i USING(member) '
                        'WHERE (i.host_profile=? OR n.member=?) AND n.credential_revoked=0 AND n.credential_hash=?',
                        (profile, profile, hash_credential(credential))).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def register_remote_node(conn, member: str, credential: str, *, direction: str,
                         peer_ip: str = "", endpoint: dict | None = None,
                         commit: bool = True, now=None) -> dict | None:
    """Register after authentication. The caller may include invitation redemption
    in the same transaction. A name conflict never updates an existing identity.
    """
    return _register_remote_node_hash(conn, member, hash_credential(credential), direction=direction,
                                      peer_ip=peer_ip, endpoint=endpoint, commit=commit, now=now)


def _register_remote_node_hash(conn, member, credential_hash, *, direction, peer_ip='', endpoint=None,
                               commit=True, now=None):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if remote_member_conflict(conn, member):
        if commit:
            conn.rollback()
        return None
    now = _now() if now is None else now
    conn.execute(
        "INSERT INTO relay_nodes (member, credential_hash, direction, dial_endpoint, mcp_config, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (member, credential_hash, direction, json.dumps(endpoint) if endpoint else "",
         '{"_auto_from_snapshot":true}', now, now))
    if peer_ip:
        from . import ip_allowlist
        if not ip_allowlist._is_loopback(peer_ip):
            ip_allowlist.add_binding(conn, member, peer_ip, commit=False)
    if commit:
        conn.commit()
    return read_node(conn, member)


def mcp_fingerprint(mcp_config: dict) -> str:
    """引擎侧指纹：对渲染产物（命令/env 规范 JSON）的 sha256——即对随包 MCP
    入口内容摘要的计算口径；插件上报其包内 MCP 入口的内容 hash，一致才算兼容。"""
    canonical = json.dumps(mcp_config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def register_node(conn, member: str, credential: str, *, display_name: str = "",
                  mcp_config: dict | None = None, rotate: bool = False, now=None) -> dict:
    """注册/更新节点：凭据只存 hash。mcp_config 的 _package_sha 字段是
    随包 MCP 入口内容摘要（管理员记录的期望指纹），握手时与插件上报比对。

    req#170 返修（manager node2011 契约）：
    - 节点不存在：创建，凭据由引擎生成（credential 为空时）并一次性下发；
    - 已存在 + rotate=false（首次登记防重复/刷新）：完全不动——不轮换凭据
      （credential_hash 不变）、不覆盖 mcp_config/expected_mcp_sha、
      响应不含新凭据；
    - 已存在 + rotate=true（重签）：仅轮换凭据（引擎生成下发）、
      credential_revoked=0；mcp_config 缺省 None 时保留原配置与原期望摘要
      （防 UI 缺省 {} 清空），显式传入则按传入更新。
    """
    now = _now() if now is None else now
    existing = read_node(conn, member)
    if existing is not None and not rotate:
        return {"member": member, "credential": None, "created": False,
                "node": existing}
    if existing is not None:
        # 重签：仅轮换凭据；缺省 mcp_config 保留原配置与原期望摘要
        cred = credential or new_credential()
        config = mcp_config if mcp_config is not None else json.loads(existing["mcp_config"] or "{}")
        config_json = json.dumps(config, sort_keys=True, ensure_ascii=False)
        expected = str(config.get("_package_sha") or "")
        _cancel_renewals(conn, member)
        conn.execute(
            "UPDATE relay_nodes SET credential_hash=?, credential_revoked=0, "
            "mcp_config=?, expected_mcp_sha=?, updated_at=? WHERE member=?",
            (hash_credential(cred), config_json, expected, now, member))
        conn.commit()
        return {"member": member, "credential": cred, "created": False,
                "node": read_node(conn, member)}
    cred = credential or new_credential()
    config = mcp_config or {}
    config_json = json.dumps(config, sort_keys=True, ensure_ascii=False)
    expected = str(config.get("_package_sha") or "")
    conn.execute(
        "INSERT INTO relay_nodes(member,display_name,credential_hash,generation,state,"
        "mcp_config,expected_mcp_sha,created_at,updated_at) "
        "VALUES(?,?,?,1,'offline',?,?,?,?)",
        (member, display_name, hash_credential(cred), config_json, expected, now, now))
    conn.commit()
    return {"member": member, "credential": cred, "created": True,
            "node": read_node(conn, member)}


def revoke_credential(conn, member: str, *, now=None) -> None:
    """单独吊销某成员连接凭据：现连立即失效，需管理员重发凭据。"""
    now = _now() if now is None else now
    _cancel_renewals(conn, member)
    conn.execute("UPDATE relay_nodes SET credential_revoked=1,updated_at=? WHERE member=?",
                 (now, member))
    conn.commit()


def verify_credential(conn, member: str, credential: str) -> bool:
    """常量时间比较：凭据绑定成员、未吊销才通过。"""
    row = read_node(conn, member)
    if row is None or row["credential_revoked"] or not row["credential_hash"]:
        return False
    return hmac.compare_digest(row["credential_hash"], hash_credential(credential))


_NODE_SELECT = ('SELECT n.*, COALESCE(i.host_profile,n.member) AS host_profile FROM relay_nodes n '
                'LEFT JOIN relay_node_identities i USING(member)')


def read_node(conn, member: str):
    return conn.execute(_NODE_SELECT + ' WHERE n.member=?', (member,)).fetchone()


def list_nodes(conn):
    return conn.execute(_NODE_SELECT + ' ORDER BY n.member').fetchall()


def set_mcp_verify(conn, member: str, *, state: str, error: str = "",
                   generation: int | None = None) -> None:
    """MCP 只读验证结果落库（manager node2102）：state=ok/failed、error 脱敏
    失败原因；带代条件写（旧连接迟到的验证结果不得覆盖新代状态）。"""
    if state not in ("ok", "failed"):
        raise ValueError("验证结果必须是 ok 或 failed")
    if generation is None:
        conn.execute(
            "UPDATE relay_nodes SET mcp_verify_state=?,mcp_verify_error=?,"
            "updated_at=? WHERE member=?",
            (state, str(error)[:300], _now(), member))
    else:
        conn.execute(
            "UPDATE relay_nodes SET mcp_verify_state=?,mcp_verify_error=?,"
            "updated_at=? WHERE member=? AND generation=?",
            (state, str(error)[:300], _now(), member, generation))
    conn.commit()


def generation_advance(conn, member: str, *, now=None) -> int:
    """重连后连接代 +1（旧代帧从此拒收）。manager node2102：换代即新连接
    ——上一代的 MCP 只读验证结果一并作废（置回待验，页面不得沿用旧成功）。"""
    now = _now() if now is None else now
    conn.execute(
        "UPDATE relay_nodes SET generation=(generation % ?) + 1, updated_at=?,"
        "mcp_verify_state='',mcp_verify_error='' WHERE member=?",
        (MAX_GENERATION, now, member))
    conn.commit()
    row = read_node(conn, member)
    return row["generation"] if row else 1


def mark_online(conn, member: str, *, code_version: str, mcp_fingerprint: str,
                generation: int, now=None) -> None:
    """新连接握手成功：写库带连接代条件——接替瞬间旧代写入不覆盖新代状态
    （manager node1981 ②：状态 SQL 以当前连接代为条件）。"""
    conn.execute(
        "UPDATE relay_nodes SET state='online',code_version=?,mcp_fingerprint=?,"
        "generation=?,last_heartbeat=?,updated_at=? WHERE member=? AND generation=?",
        (code_version, mcp_fingerprint, generation, _now() if now is None else now,
         _now() if now is None else now, member, generation))
    conn.commit()


def heartbeat(conn, member: str, *, generation: int, now=None) -> None:
    """心跳只更新存活槽；绝不据此判任务结束（设计 4 章）。带当前代条件：
    旧连接迟到心跳（代不匹配）不覆盖新代状态（manager node1981 ②）。"""
    conn.execute("UPDATE relay_nodes SET last_heartbeat=?,state='online' "
                 "WHERE member=? AND generation=?",
                 (_now() if now is None else now, member, generation))
    conn.commit()


def mark_offline(conn, member: str, *, generation=None, now=None) -> None:
    """断链标离线。generation 给定时按代条件写（旧代清理不覆盖新代 online）；
    不给定时按当前行代写（TTL 过期场景，与读到的行同代）。"""
    row = read_node(conn, member)
    if row is None:
        return
    target_gen = row["generation"] if generation is None else generation
    conn.execute("UPDATE relay_nodes SET state='offline',updated_at=? "
                 "WHERE member=? AND generation=?",
                 (_now() if now is None else now, member, target_gen))
    conn.commit()


def is_online(conn, member: str) -> bool:
    row = read_node(conn, member)
    if row is None:
        return False
    # 心跳 TTL：90s 内无心跳即离线（只判存活）。
    if row["last_heartbeat"] is not None and time.time() - row["last_heartbeat"] > HEARTBEAT_TTL_SECONDS:
        mark_offline(conn, member, generation=row["generation"])
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
    # manager node2084：方向 B（引擎拨号、插件监听）的随包 MCP 进程
    # 在 ECS 本机——GROUP_ENGINE_URL 渲染为插件 loopback 代理实际端口
    # （请求沿已认证通道回引擎按成员权限执行）；方向 A 用引擎直连地址。
    render_url = engine_url
    if row["direction"] == "dial" and row["proxy_port"]:
        render_url = f"http://127.0.0.1:{row['proxy_port']}"
    rendered = _substitute(config, {"engine_url": render_url})
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


def mcp_report(conn, member: str, fingerprint: str, *, generation: int | None = None,
               proxy_port: int = 0, now=None) -> None:
    """连接存续中插件换包后上报的新摘要（manager node1977 ③）：
    更新为 configure 前复核用的远端实际摘要。带代条件防旧代迟到上报
    覆盖新代（manager node1981 ②）。proxy_port 为插件 loopback MCP
    代理端口（manager node2084：方向 B 随包 MCP 的 GROUP_ENGINE_URL
    指向它，0=本次会话未启代理）。"""
    updates = "mcp_fingerprint=?"
    args: list = [fingerprint]
    if proxy_port:
        updates += ",proxy_port=?"
        args.append(proxy_port)
    args.append(_now() if now is None else now)
    if generation is None:
        conn.execute(f"UPDATE relay_nodes SET {updates},updated_at=? "
                     "WHERE member=?", (*args, member))
    else:
        conn.execute(f"UPDATE relay_nodes SET {updates},updated_at=? "
                     "WHERE member=? AND generation=?",
                     (*args, member, generation))
    conn.commit()


def attach_submission(conn, *, message_ids: list[int], member: str,
                      submission_id: str, now=None) -> int:
    """投递未决（admitted=False）时持久关联实际注入的目标点名与 submission：
    保持 offline_waiting，submission_id 供后续权威回执结算；收口（expire_mentions
    只标 submission_id IS NULL）跳过已尝试但未决者（manager node1981 ①）。
    幂等：重复调用不改已绑定/已提交行。"""
    now = _now() if now is None else now
    if not message_ids or not submission_id:
        return 0
    placeholders = ",".join("?" * len(message_ids))
    cur = conn.execute(
        "UPDATE relay_offline_mentions SET submission_id=?,updated_at=? "
        "WHERE member=? AND state='offline_waiting' AND submission_id IS NULL "
        f"AND message_id IN ({placeholders})",
        (submission_id, now, member, *message_ids))
    conn.commit()
    return cur.rowcount


def settle_by_submission(conn, *, member: str, submission_id: str, now=None) -> int:
    """权威回执（apply_receipt 确认受理）结算：把该 submission 关联的
    offline_waiting 点名标 submitted（manager node1981 ①：恢复回执有入口补记）。"""
    now = _now() if now is None else now
    if not submission_id:
        return 0
    cur = conn.execute(
        "UPDATE relay_offline_mentions SET state=?,updated_at=? "
        "WHERE member=? AND submission_id=? AND state='offline_waiting'",
        (MENTION_SUBMITTED, now, member, submission_id))
    conn.commit()
    return cur.rowcount


def reconcile_switch_resume(conn, member: str, *, now=None, client=None) -> dict:
    """切换恢复的首次在途核对（manager node2064 ③，两握手入口共用）：
    通道可用后按原 submission_id 核对——已知回执（execution 已 terminal）
    立即应用结算；未知时先尝试一次网络核对（client 给定时按 session.resume
    口径走 ledger.reconcile——无 admission_id 的 DB 短路同样生效），仍未知
    则持久保留 offline_waiting 且禁止原提交重派（收口跳过 submission_id
    非空者），之后恢复普通派发。

    核对对象两部分：
    1. 未决点名（relay_offline_mentions 的 offline_waiting+submission_id）
       ——已知回执 settle 标 submitted、未知保留待核对；
    2. 该成员未终态的 hermes_executions（无未决点名也核对——manager 附录
       口径：execution unknown 经切换恢复也要首次核对 session.resume）。

    持续的网络核对由能持续处理帧的位置承担：GatewayClient 会话循环按
    execution 生命周期持续调用 ledger.reconcile（hermes_gateway 既有链路）。
    返回 {applied, kept, reconciled} 计数供日志。"""
    now = _now() if now is None else now
    applied = 0
    kept = 0
    reconciled = 0
    from . import hermes_gateway_ledger as ledger

    def _try_reconcile(execution: dict) -> bool:
        """首次核对尝试：返回 True=已核对出终态（回执已知）。"""
        nonlocal reconciled
        try:
            ledger.reconcile(client, conn, execution["execution_id"], now=now)
            reconciled += 1
            refreshed = conn.execute(
                "SELECT state FROM hermes_executions WHERE execution_id=?",
                (execution["execution_id"],)).fetchone()
            return refreshed is not None and refreshed["state"] == "terminal"
        except Exception:  # noqa: BLE001 RPC 不可用按未知保留
            return False

    # 1) 未决点名按原 submission_id 核对
    pending = conn.execute(
        "SELECT DISTINCT submission_id FROM relay_offline_mentions "
        "WHERE member=? AND state='offline_waiting' AND submission_id IS NOT NULL",
        (member,)).fetchall()
    for row in pending:
        submission_id = row["submission_id"]
        execution = conn.execute(
            "SELECT execution_id, state FROM hermes_executions "
            "WHERE submission_id=? AND profile=?",
            (submission_id, member)).fetchone()
        if execution is not None and execution["state"] == "terminal":
            # 已知回执：立即应用（点名标 submitted，不再保留待核对）
            applied += settle_by_submission(conn, member=member,
                                            submission_id=submission_id, now=now)
        elif execution is not None and client is not None and _try_reconcile(execution):
            applied += settle_by_submission(conn, member=member,
                                            submission_id=submission_id, now=now)
        else:
            # 未知：持久保留 offline_waiting，禁止原提交重派
            kept += 1
    # 2) 该成员未终态执行记录做首次核对（manager node2064 附录口径：
    #    execution unknown 经切换恢复也要核对，无未决点名同样生效）。
    #    client 给定时对全部未终态行做 session.resume 网络问询；client
    #    为 None 时只对未受理（admission_id 空）行做 DB 级核对
    #    （ledger.reconcile 短路标 uncertain，无网络、无 client 触碰；
    #    已受理行的真实 client 问询由 restore_switch_after_handshake
    #    从新链 lease 承担，避免 None.rpc 抛错与重复问询）。
    if client is not None:
        in_flight = conn.execute(
            "SELECT execution_id, state FROM hermes_executions "
            "WHERE profile=? AND state NOT IN ('terminal')", (member,)).fetchall()
    else:
        in_flight = conn.execute(
            "SELECT execution_id, state FROM hermes_executions "
            "WHERE profile=? AND state NOT IN ('terminal') "
            "AND admission_id IS NULL", (member,)).fetchall()
    for execution in in_flight:
        _try_reconcile(execution)
    return {"applied": applied, "kept": kept, "reconciled": reconciled}


def _reported_mcp_config(conn, node, report, engine_url):
    """Use an authenticated first-join snapshot only on that remote runtime."""
    from pathlib import PurePosixPath, PureWindowsPath
    from urllib.parse import urlsplit
    from . import config, ip_allowlist, listen_control
    from .hermes_gateway_client import GatewayRefusal

    if (node['state'] != 'online' or node['credential_revoked'] or not report
            or report.get('generation') != node['generation']):
        raise GatewayRefusal('mcp_snapshot_unavailable')
    command, entry = report.get('mcp_python'), report.get('mcp_entry')
    for path in (command, entry):
        if (not isinstance(path, str) or not 0 < len(path) <= 4096 or any(ord(c) < 32 for c in path)
                or not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute())):
            raise GatewayRefusal('mcp_config_missing')
    if node['direction'] == 'dial':
        port = report.get('proxy_port')
        if type(port) is not int or not 0 < port < 65536:
            raise GatewayRefusal('mcp_config_missing')
        url = f'http://127.0.0.1:{port}'
    else:
        url = engine_url
        host = urlsplit(url).hostname
        address = listen_control.read_config(conn) if host and (host == 'localhost' or ip_allowlist._is_loopback(host)) else None
        if address:
            host = f'[{address}]' if ':' in address else address
            url = f'http://{host}:{config.port()}'
    return {'command': command, 'args': [entry], 'env': {'GROUP_ENGINE_URL': url}}


def merge_mcp_spec(conn, member: str, spec: dict, *, engine_url: str = "",
                   fetch_report=None) -> dict:
    """远程节点的随包 MCP 配置合并（manager node1977 ③ + node1981 ③）：

    - configure 前取得「本次配置的远端实际摘要」复核：fetch_report 给定时
      向当前连接同步问询（mcp_snapshot 请求帧）并以回复为准，不只用握手/
      异步 mcp_report 缓存——覆盖「同连接原地换包、配置早于异步上报」时序；
      无 fetch 能力（本机连接/测试）时用库内最新摘要；
    - 渲染后缺 command 时拒绝（绝不回落本机配置——本机 MCP 命令对远端无效）；
    - 可达 URL（GROUP_ENGINE_URL）以远端渲染值为准（本机会话 env 不覆盖）；
    - 身份令牌与上下文仍由引擎注入（会话级 env 其余项覆盖合并）。

    返回合并后 spec。节点不存在（本机成员）时原样返回 spec（本机路径不动）。
    """
    from .hermes_gateway_client import GatewayRefusal
    row = read_node(conn, member)
    if row is None:
        return spec
    report = None
    if fetch_report is not None:
        report = fetch_report()
        if not report or not report.get("mcp_fingerprint"):
            # 问询失败/无响应：取不到本次实际摘要即拒绝，绝不回落握手缓存、
            # 绝不把库内摘要清空后放行（reviewer 返修②）。
            raise GatewayRefusal("mcp_snapshot_unavailable")
        # 写库带代条件：旧代迟到的问询响应不得覆盖新代摘要（reviewer 返修①）。
        # 报告无代字段（防御/模拟调用）时按库内当前代写。
        _row = read_node(conn, member)
        row_gen = _row["generation"] if _row is not None else None
        write_gen = report.get("generation") if report.get("generation") is not None else row_gen
        mcp_report(conn, member, report["mcp_fingerprint"], generation=write_gen)
        # manager node2084：快照带本次 loopback 代理实际端口（含 0=未启代理，
        # 换包/停代理后旧值必须清零，否则 GROUP_ENGINE_URL 残留旧端口）
        if isinstance(report.get("proxy_port"), int):
            conn.execute("UPDATE relay_nodes SET proxy_port=? "
                         "WHERE member=? AND generation=?",
                         (report["proxy_port"], member, write_gen))
            conn.commit()
        if report.get("code_version"):
            conn.execute("UPDATE relay_nodes SET code_version=? "
                         "WHERE member=? AND generation=?",
                         (report["code_version"], member, write_gen))
            conn.commit()
    remote = render_mcp_config(conn, member, engine_url=engine_url)
    node = read_node(conn, member)
    if node["expected_mcp_sha"] and node["mcp_fingerprint"] != node["expected_mcp_sha"]:
        raise GatewayRefusal("mcp_package_mismatch")
    if not remote.get('command'):
        try:
            stored = json.loads(node['mcp_config'] or '{}')
        except (TypeError, ValueError):
            stored = None
        if isinstance(stored, dict) and stored.get('_auto_from_snapshot') is True:
            remote = _reported_mcp_config(conn, node, report, engine_url)
        else:
            raise GatewayRefusal('mcp_config_missing')
    merged = {**spec, **{key: value for key, value in remote.items()
                        if key in ("command", "args")}}
    merged_env = dict(remote.get("env") or {})
    for key, value in (spec.get("env") or {}).items():
        if key == "GROUP_ENGINE_URL":
            continue  # 可达 URL 以远端渲染为准（manager node1977 ③）
        merged_env[key] = value
    merged["env"] = merged_env
    return merged


def new_credential() -> str:
    return secrets.token_urlsafe(32)


# ---------------- req#173 方向与切换状态机（先断后建，动作驱动） ----------------

def offline_reason(conn, member: str) -> str:
    """节点 offline 的原因（动作驱动，不设计时器）：
    switch_state='switching' → 'switch'（切换中断链，不触发任务重试/告警）；
    否则 → 'unexpected'（意外掉线，走普通重连/重试）。"""
    row = read_node(conn, member)
    if row is None:
        return ""
    if row["state"] != "offline":
        return ""
    return "switch" if row["switch_state"] == "switching" else "unexpected"


def begin_switch(conn, member: str, target: str, *, now=None) -> None:
    """记录切换意图+目标方向（随节点持久化，重启可读回）并置切换中：
    切换期间派发闸门 hold（暂停派发）、offline 记为 switch 原因（不告警）。"""
    if target not in ("listen", "dial"):
        raise ValueError("目标方向必须是 listen 或 dial")
    row = read_node(conn, member)
    if row is None:
        raise ValueError("节点不存在")
    conn.execute(
        "UPDATE relay_nodes SET switch_state='switching',switch_target=?,"
        "switch_error='',updated_at=? WHERE member=?",
        (target, _now() if now is None else now, member))
    conn.commit()


def complete_switch(conn, member: str, *, now=None, client=None) -> None:
    """切换完成（新方向认证成功/用户切回/重启后目标已达成）：先按原
    submission_id 做首次在途核对（manager node2064 ③——两握手入口共用
    同一恢复步骤，不再直接清切换态），再清切换态、更新 direction 为目标
    方向，恢复普通派发。client 给定时首次核对走 session.resume 网络
    问询（GatewayClient 会话循环之外的握手入口可传）。"""
    row = read_node(conn, member)
    if row is None:
        return
    target = row["switch_target"] or row["direction"]
    # 首次在途核对：已知回执立即应用，未知持久保留禁止原提交重派
    reconcile_switch_resume(conn, member, now=now, client=client)
    conn.execute(
        "UPDATE relay_nodes SET switch_state='',switch_target='',"
        "direction=?,updated_at=? WHERE member=?",
        (target, _now() if now is None else now, member))
    conn.commit()


def restore_switch_after_handshake(member: str, *, target: str,
                                   generation: int) -> None:
    """新链注册并能收发帧后的异步恢复（manager node2073）：独立工作线程
    + 自有数据库连接，从新链取得 relay lease 对已受理执行做首次问询
    （prompt.receipt 走真实 client——握手线程无 client，同步核对会
    None.rpc 抛错），首次问询完成后按仍有效的本地代与目标清切换态、
    恢复派发。未知结果持久保留、禁止原提交重派。

    时序：握手先注册 link 再启动本线程；问询期间 switch_state 仍为
    switching → 闸门 hold；完成后 complete_switch → 恢复普通派发。"""
    import threading

    def _worker() -> None:
        conn = db.get_connection()
        lease = None
        try:
            row = read_node(conn, member)
            if row is None or row["switch_state"] != "switching" \
                    or row["switch_target"] != target:
                return  # 新切换意图已接管或状态已清：本链恢复放弃
            from . import relay_server
            link = relay_server.active_link(member)
            if link is None or relay_server._db_generation(link) != generation:
                return  # 本地代已失效：活动链已换新代
            from . import hermes_gateway as gateway
            lease = gateway._relay_lease(member)
            from . import hermes_gateway_ledger as ledger
            # 首次问询：该成员已受理（admission_id 非空）的未终态执行
            # 逐个 session.resume 口径核对；问询失败/未知均保留待核对。
            pending = conn.execute(
                "SELECT execution_id FROM hermes_executions "
                "WHERE profile=? AND state NOT IN ('terminal') "
                "AND admission_id IS NOT NULL", (member,)).fetchall()
            for execution in pending:
                try:
                    ledger.reconcile(lease, conn, execution["execution_id"])
                except Exception:  # noqa: BLE001 问询失败保留待核对
                    logger.warning("relay restore receipt query failed "
                                   "member=%s execution=%s",
                                   member, execution["execution_id"])
            # 首次问询完成：仍有效的本地代与目标才恢复派发
            link = relay_server.active_link(member)
            row = read_node(conn, member)
            if link is not None \
                    and relay_server._db_generation(link) == generation \
                    and row is not None and row["switch_state"] == "switching" \
                    and row["switch_target"] == target:
                complete_switch(conn, member)
        except Exception:  # noqa: BLE001 恢复失败保持切换态待下轮
            logger.exception("relay switch restore worker failed member=%s",
                             member)
        finally:
            if lease is not None:
                lease.close()
            conn.close()

    threading.Thread(target=_worker, daemon=True,
                     name=f"relay-switch-restore-{member}").start()


def set_direction(conn, member: str, direction: str, *, now=None) -> None:
    """管理员直接设定方向（无切换动作时），切换动作请走 begin_switch。"""
    if direction not in ("listen", "dial"):
        raise ValueError("方向必须是 listen 或 dial")
    conn.execute(
        "UPDATE relay_nodes SET direction=?,updated_at=? WHERE member=?",
        (direction, _now() if now is None else now, member))
    conn.commit()


def update_dial_pairing_token(conn, member: str, token: str) -> None:
    """Persist the long-lived pairing credential for subsequent reconnects."""
    endpoint = read_dial_endpoint(conn, member)
    if endpoint is None:
        return
    endpoint["pairing_token"] = token
    conn.execute(
        "UPDATE relay_nodes SET dial_endpoint=?,updated_at=? WHERE member=?",
        (json.dumps(endpoint), time.time(), member))
    conn.commit()


def discard_dial_activation_ticket(conn, member, endpoint, expected):
    """Drop the one-time ticket without overwriting a concurrently edited endpoint."""
    clean = {k: v for k, v in endpoint.items() if k not in ('activation_ticket', 'activation_expires_at')}
    with conn:
        conn.execute('UPDATE relay_nodes SET dial_endpoint=?,updated_at=? '
                     'WHERE member=? AND dial_endpoint=?', (json.dumps(clean), _now(), member, expected))


def set_dial_endpoint(conn, member: str, endpoint: dict, *, now=None) -> None:
    """保存插件监听端点（配对记录）：{host, port, tls, fingerprint, pairing_token}。
    认证收紧（manager node2052 ①）：TLS 与证书指纹必填（自托管配对
    必有证书，公开配置不接受明文 WS/空指纹）；一次性配对凭据随端点保存。
    """
    row = read_node(conn, member)
    if row is None:
        raise ValueError("节点不存在")
    host = str(endpoint.get("host") or "").strip()
    if not host:
        raise ValueError("dial endpoint 必填 host")
    try:
        port = int(endpoint.get("port") or 0)
    except (TypeError, ValueError):
        raise ValueError("dial endpoint port 必须是整数") from None
    if not 1 <= port <= 65535:
        raise ValueError("dial endpoint port 越界")
    if not endpoint.get("tls", True):
        raise ValueError("拨号端点必须启用 TLS（不接受明文 WS）")
    fingerprint = str(endpoint.get("fingerprint") or "").strip().lower().replace(":", "")
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise ValueError("拨号端点必须提供 64 位十六进制证书指纹")
    payload = {
        "host": host,
        "port": port,
        "tls": True,
        "fingerprint": fingerprint,
        # 一次性配对凭据（插件 listen 签发）：拨号 hello 出示，插件侧兑换
        "pairing_token": str(endpoint.get("pairing_token") or "").strip(),
    }
    conn.execute(
        "UPDATE relay_nodes SET dial_endpoint=?,updated_at=? WHERE member=?",
        (json.dumps(payload, ensure_ascii=False), _now() if now is None else now, member))
    conn.commit()


def read_dial_endpoint(conn, member: str) -> dict | None:
    """解析保存的插件监听端点；未配置返回 None。"""
    row = read_node(conn, member)
    if row is None or not row["dial_endpoint"]:
        return None
    try:
        payload = json.loads(row["dial_endpoint"])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not payload.get("host"):
        return None
    return payload


def record_switch_error(conn, member: str, message: str, *, now=None) -> None:
    """记录切换/拨号失败原因（页面显示失败原因与重试/切回入口）。"""
    conn.execute(
        "UPDATE relay_nodes SET switch_error=?,updated_at=? WHERE member=?",
        (str(message)[:500], _now() if now is None else now, member))
    conn.commit()
