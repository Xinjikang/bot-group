"""消息路由分发：落 messages + inbox 分发 + 唤醒入队。

树模型规则：
- 话题 = 一棵树：根=开场白，轮次=树的层，独立发言=轮次子节点，@子树=更深层。
- 发言顺序 = DFS 深度优先遍历。
- 轮次：每轮所有成员依次独立发言一次（发起者压轴），全员发言完进下一轮。
- 独立发言树：@N人=N条边=N个子节点，DFS 遍历，深度限制 4 层。
- 收口：某轮全员 pass/弃权，或 30min 静默超时。
- 点对点 @（thread_id 空）：只给被 @ 者插行，即时唤醒（work）。

节点单位 = 发言动作（不是发言人身份）：同一个 bot 发言两次是两个节点，
时间单调递增，天然无环，是树不是 DAG。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

from . import db, wake
from .mentions import parse_mentions
from .delivery import effective_need_reply

logger = logging.getLogger(__name__)


class RouteError(ValueError):
    """路由校验失败（对应 HTTP 400/403/404）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


MAX_DEPTH = 4  # 独立发言树最大深度为 4，根节点深度为 0。

# ---- 互@回环断链（req#47 task#81）----
# 一轮 = 来回 2 条（A@B、B@A）；最多 N=3 轮（6 条严格交替）；第 7 条触发报错断链。
# 只扫 thread_id IS NULL 点对点 + bot↔bot 互@，按 (a,b) 无序对过滤。
PINGPONG_MAX_ROUNDS = 3           # N=3 轮
PINGPONG_LOOKBACK = 6             # 回溯 LIMIT（= 2*N）
PINGPONG_WINDOW_SECONDS = 600     # 时间窗 10 分钟（常量配置）
PINGPONG_ERR_MSG = "消息发送失败，因为已经互@3轮了。如果还需要继续讨论，请创建话题。"
PINGPONG_MARKER = "[互@断链]"  # 断链标记消息 content（engine 落库，作归零点，前端隐藏）


def _ts_seconds(ts_str) -> float:
    """ISO 时间串 → unix 秒（解析失败返回 0）。"""
    try:
        return time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def _pingpong_breach(conn: sqlite3.Connection, sender: str,
                     mentions: list[str], now_ts: str, project: str = "xjk") -> bool:
    """点对点互@回环检测：sender 与被@的第一个 bot 是否已互@满 N 轮（应断链）。

    req#47 task#81：
    - 只扫 thread_id IS NULL 点对点 + bot↔bot（sender 非 user、被@的含 bot）；
    - 按 (a,b) 无序对 + thread_id IS NULL 过滤，回溯最近 LIMIT=6 条（=2*N）；
    - 归零点：报错时落的 engine 标记消息（content=PINGPONG_MARKER）之后才重新计数，
      即只回溯 id > 该对最近标记 id 的消息（触发超限即归零，不残留旧计数）；
    - 从旧到新扫「连续严格交替」：相邻 sender 交替 + 各自 @ 对方 + 时间窗内，
      任一不满足（同向连发 / 超窗 / 非互@）即归零（break），满 6 条=3 轮则触发；
    - 不落持久化计数器：连续数实时从 messages 回溯算。
    """
    if db.is_builtin_user(sender):
        return False
    targets = [p for p in mentions if not db.is_builtin_user(p)]
    if not targets:
        return False
    other = targets[0]
    if sender == other:
        return False
    lo, hi = (sender, other) if sender < other else (other, sender)

    # 归零点：这对 (lo,hi) 在该 project 下最近一次断链标记之后的互@才计数
    marker = conn.execute(
        "SELECT id FROM messages WHERE thread_id IS NULL AND sender_type='engine' "
        "AND content LIKE ? AND mentions LIKE ? AND mentions LIKE ? AND project=? "
        "ORDER BY id DESC LIMIT 1",
        (PINGPONG_MARKER + "%", f'%"{lo}"%', f'%"{hi}"%', project),
    ).fetchone()
    min_id = marker["id"] if marker else 0

    # 过滤（先于交替检查）：按 (a,b) 无序对 + thread_id IS NULL，且这条消息 @ 了对方
    # （sender=lo 时 mentions 含 hi，sender=hi 时 mentions 含 lo）。换对象（a@c）不进序列。
    rows = conn.execute(
        "SELECT sender, mentions, ts FROM messages "
        "WHERE thread_id IS NULL AND sender IN (?,?) AND project=? AND id > ? "
        "AND ((sender=? AND mentions LIKE ?) OR (sender=? AND mentions LIKE ?)) "
        "ORDER BY id DESC LIMIT ?",
        (lo, hi, project, min_id,
         lo, f'%"{hi}"%', hi, f'%"{lo}"%',
         PINGPONG_LOOKBACK),
    ).fetchall()
    if len(rows) < PINGPONG_LOOKBACK:
        return False

    now_s = _ts_seconds(now_ts)
    consecutive = 0
    prev_sender: str | None = None
    for r in reversed(rows):  # 旧 → 新
        s = r["sender"]
        if prev_sender is not None and s == prev_sender:
            break  # 重置①：同向连发 → 归零
        if now_s - _ts_seconds(r["ts"]) > PINGPONG_WINDOW_SECONDS:
            break  # 重置②：超窗 → 归零
        # 重置③（触发超限归零）在 route_message 报错分支落 marker 实现，这里不落。
        consecutive += 1
        prev_sender = s
    return consecutive >= PINGPONG_LOOKBACK


def _thread_members(conn: sqlite3.Connection, thread_id: int) -> list[str]:
    row = conn.execute("SELECT members FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None:
        raise RouteError(f"thread {thread_id} not found", 404)
    try:
        members = json.loads(row["members"])
    except (TypeError, ValueError):
        members = []
    return [str(m) for m in members]


def _thread_status(conn: sqlite3.Connection, thread_id: int) -> str:
    row = conn.execute("SELECT status FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None:
        raise RouteError(f"thread {thread_id} not found", 404)
    return row["status"]


def _thread_creator(conn: sqlite3.Connection, thread_id: int) -> str:
    row = conn.execute("SELECT creator FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return row["creator"] if row else ""


def _thread_round(conn: sqlite3.Connection, thread_id: int) -> int:
    row = conn.execute("SELECT round FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return row["round"] if row else 0


def _insert_inbox_rows(
    conn: sqlite3.Connection, message_id: int, profiles: list[str], project: str = ""
) -> None:
    """给这些 profile 插 pending 未读行。

    按项目的 project_members.active 状态筛选，避免为停用成员积压未读消息。
    """
    for p in _active_profiles(conn, profiles, project):
        conn.execute(
            "INSERT INTO inbox (message_id, profile, status) VALUES (?, ?, 'pending')",
            (message_id, p),
        )


def _is_ack_message(text: str) -> bool:
    """通过白名单与长度阈值判定 pass 或简短确认消息。

    标准：去空白后 ≤ 24 字符，且命中回显关键词白名单（pass 无视大小写、
    无意见/无新增/同意/收到/无补充/+1 等），或以 pass 开头（长 pass 说明
    也视为表态的，如「pass。id=61 已给出结论」——回显性质不是提问）。
    红线：引擎不删消息（messages 只 INSERT），只免分发免唤醒。

    「好」字用子串匹配会把
    「我觉得方案A好」「挺好」误判成 pass，直接影响「实质发言 vs pass」的收口
    判定。改为整词匹配——「好」只在单独成词（好/好的/好。/好，/好！）才算 pass。
    🔴 2026-09-11 二次收紧（architect 抓）：「同意」子串匹配会把「不同意」
    「我不同意这个方案」误判成 pass。统一加否定前置检查——含否定词直接判非 pass，
    覆盖所有 ack 词的反义形式（不同意/不收到/不无意见…），不用逐个词排除。
    """
    t = text.strip()
    if not t:
        return False
    if t.lower().startswith("pass") or t.startswith("(pass"):
        return True
    compact = "".join(t.split())  # 去所有空白后比长短
    if len(compact) > 24:
        return False
    # 固定肯定短语（含否定字但语义是同意/确认）：先于否定词检查，消除
    # 「没意见」和「无意见」的分类差异。裸短语精确匹配，带主语
    # 的变体（「我没意见」）不处理——方向保守无害，不值得加复杂度。
    if t in ("没问题", "没意见", "没毛病", "没异议", "没补充", "没新增"):
        return True
    # 否定前置检查：含否定词 = 表达反对/异议，不是 pass（覆盖「不同意」「不赞成」等）
    if any(neg in t for neg in ("不", "没", "别", "否", "反对", "拒绝")):
        return False
    # 整词匹配纯语气词和确认短语，
    # 允许可选主语前缀（我/我们/本人/咱/咱们）+ 标点后缀。彻底消除子串匹配尾巴：
    # 「嗯」不再误伤「嗯，我补充一点」、「ok」不再误伤「look/book/token」、
    # 「+1」不再误伤「+10」、「同意」不再误伤「我同意这个方案」。
    # 否定词已在上一步拦截（「不同意」「没收到」），这里只做正向整词匹配。
    _ack_re = re.compile(
        r"^(?:我|我们|本人|咱|咱们)?"
        r"(?:好[的]?|嗯+|ok(?:ay)?|\+1|同意|收到|知道了|无意见|无新增|无补充)"
        r"[。，！？~～]*$",
        re.IGNORECASE,
    )
    return bool(_ack_re.fullmatch(compact))


# ---------------------------------------------------------------------------
# 树模型：弃权集合 + 发言位判定
# ---------------------------------------------------------------------------

def _skipped(conn: sqlite3.Connection, thread_id: int) -> set[str]:
    """该话题已弃权的发言位集合。

    发言位标识（判定单位 = 发言位，不是 bot）：
    - 轮次独立发言位：`r:{round}:{profile}`
    - @ 边发言位：`e:{msg_id}:{profile}`
    """
    row = conn.execute("SELECT turn_skipped FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None:
        return set()
    try:
        return {str(x) for x in json.loads(row["turn_skipped"] or "[]")}
    except (TypeError, ValueError):
        return set()


def _profile_active(conn: sqlite3.Connection, project: str, profile: str) -> bool:
    """该 profile 在该 project 下是否启用（req#27 核心，ENGINEERING 28.2）。

    项目成员必须显式启用：
    - `user` 恒 True（人不在启停语义里）；
    - `project_members` 无记录 → False；
    - 有记录看 `active == 1`；
    - 查询异常 → False。
    """
    if db.is_builtin_user(profile):
        return True
    try:
        row = conn.execute(
            "SELECT pm.active FROM project_members pm JOIN members m ON m.profile=pm.profile "
            "WHERE pm.project=? AND pm.profile=?",
            ((project or "").strip(), profile),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    return row["active"] == 1


def _active_profiles(
    conn: sqlite3.Connection, profiles: list[str], project: str = ""
) -> list[str]:
    """保留项目中显式启用的成员与 user。

    停用状态按项目维度处理：
    - 停用成员不参与轮次、不被插 inbox 行、不被唤醒；
    - `threads.members` 与历史发言一律不动（停用≠除名，随时可追溯）；
    - `user` 天然放行（人不是 bot，不在启停语义里）；
    - 无成员记录或 active=0 时跳过。
    """
    if not profiles:
        return []
    names = [str(p) for p in profiles]
    bots = [p for p in names if not db.is_builtin_user(p)]
    if not bots:
        return names  # 只有 user：人天然放行，永远不滤
    return [
        p for p in names
        if db.is_builtin_user(p) or _profile_active(conn, project, p)
    ]


def _round_members(conn: sqlite3.Connection, thread_id: int) -> list[str]:
    """轮次发言顺序：按 members 顺序，发起者最后发言。

    停用成员不进轮次（跳过不唤醒）；creator 自己被停用则自然退出轮次——
    压轴逻辑照旧，只是过滤后 creator 可能已不在列表里。
    """
    thread = conn.execute(
        "SELECT project, members FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    project = thread["project"] if thread else ""
    members = _active_profiles(conn, _thread_members(conn, thread_id), project)
    creator = _thread_creator(conn, thread_id)
    others = [m for m in members if m != creator]
    if creator in members:
        others.append(creator)
    return others


def _round_spoken(conn: sqlite3.Connection, thread_id: int, round_no: int, profile: str) -> bool:
    """profile 在第 round_no 轮是否已独立发言（parent_id IS NULL 且 round=round_no）。"""
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE thread_id=? AND sender=? AND round=? AND parent_id IS NULL",
        (thread_id, profile, round_no),
    ).fetchone()["n"]
    return n > 0


def _edge_responded(conn: sqlite3.Connection, msg_id: int, profile: str) -> bool:
    """@ 边 (msg_id→profile) 是否已回应：存在 parent_id=msg_id 且 sender=profile 的消息。"""
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE parent_id=? AND sender=?",
        (msg_id, profile),
    ).fetchone()["n"]
    return n > 0


def _round_all_pass(conn: sqlite3.Connection, thread_id: int, round_no: int, members: list[str]) -> bool:
    """本轮所有 bot 成员都弃权或独立发言是 pass（user 不算，发起者不参与判定）。"""
    for p in members:
        if db.is_builtin_user(p):
            continue
        slot = f"r:{round_no}:{p}"
        if slot in _skipped(conn, thread_id):
            continue  # 弃权算 pass
        msg = conn.execute(
            "SELECT content FROM messages "
            "WHERE thread_id=? AND sender=? AND round=? AND parent_id IS NULL ORDER BY id LIMIT 1",
            (thread_id, p, round_no),
        ).fetchone()
        if msg is None:
            return False  # 还没发言，不算全员 pass
        if not _is_ack_message(msg["content"]):
            return False  # 有实质发言，不算全员 pass
    return True


def _dfs_subtree(
    conn: sqlite3.Connection,
    thread_id: int,
    msg_id: int,
    round_no: int,
    depth: int,
    skipped: set[str],
) -> dict | None:
    """从 msg 的 @ 边 DFS 找第一个未回应的接话位。

    depth = 当前消息的深度（独立发言=0，接话=1..4）。depth >= MAX_DEPTH 剪枝。
    返回 {profile, parent_id, round, depth} 或 None。
    """
    if depth >= MAX_DEPTH:
        return None  # 深度到顶，剪枝
    msg = conn.execute("SELECT mentions, need_reply FROM messages WHERE id=?", (msg_id,)).fetchone()
    if msg is None:
        return None
    try:
        mentions = [str(m) for m in json.loads(msg["mentions"] or "[]")]
    except (TypeError, ValueError):
        mentions = []
    members = set(_thread_members(conn, thread_id))
    for x in mentions:
        if db.is_builtin_user(x) or x not in members:
            continue  # 引擎不唤醒 user
        slot = f"e:{msg_id}:{x}"
        if slot in skipped:
            continue
        if not _edge_responded(conn, msg_id, x):
            # req#56 点2：原 @ 者声明「无需回复」（need_reply=0）→ 该 @ 边不产生
            # 发言位，被 @ 者不发言也不阻塞 turn（自动跳过，不等超时）。
            # need_reply=1（默认）仍产生发言位，被 @ 者必须实质回应。
            if effective_need_reply(msg, x) in (0, None):
                continue
            return {"profile": x, "parent_id": msg_id, "round": round_no, "depth": depth + 1}
        # x 已接话，DFS x 的接话消息的子树
        x_msg = conn.execute(
            "SELECT id FROM messages WHERE parent_id=? AND sender=? ORDER BY id LIMIT 1",
            (msg_id, x),
        ).fetchone()
        if x_msg:
            sub = _dfs_subtree(conn, thread_id, x_msg["id"], round_no, depth + 1, skipped)
            if sub:
                return sub
    return None


def settle_thread(conn, thread_id):
    """Pause automatic discussion; keep the topic available for user messages."""
    conn.execute(
        "UPDATE threads SET status='settled',turn_profile='',turn_parent_id=NULL,turn_wake_at='' "
        "WHERE id=? AND status!='closed'",
        (thread_id,),
    )
    conn.execute("UPDATE inbox SET status='expired' WHERE status='pending' AND message_id IN "
                 "(SELECT id FROM messages WHERE thread_id=?)", (thread_id,))
    conn.execute("UPDATE wake_jobs SET status='done',ended_at=?,detail='topic settled' "
                 "WHERE thread_id=? AND kind='chat' AND status='queued'", (db.now_iso(), thread_id))


def _next_speaker(conn: sqlite3.Connection, thread_id: int) -> dict | None:
    """从开场白 DFS，找第一个「该发言但还没发言」的发言位。

    返回 {profile, parent_id, round, depth} 或 None（None=收口：全员 pass/弃权）。
    纯查询，不写库——遍历状态完全由 messages（append-only）+ turn_skipped 推导。
    """
    if _thread_status(conn, thread_id) != "open":
        return None
    members = _round_members(conn, thread_id)
    if not members:
        return None
    skipped = _skipped(conn, thread_id)
    cur_round = _thread_round(conn, thread_id)

    current = conn.execute("SELECT turn_profile,turn_parent_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    if current and current["turn_profile"]:
        p, parent = current["turn_profile"], current["turn_parent_id"]
        slot = f"e:{parent}:{p}" if parent is not None else f"r:{cur_round}:{p}"
        responded = _edge_responded(conn, parent, p) if parent is not None else _round_spoken(conn, thread_id, cur_round, p)
        if p in members and not db.is_builtin_user(p) and slot not in skipped and not responded:
            depth = 0
            if parent is not None:
                row = conn.execute("SELECT depth FROM messages WHERE id=?", (parent,)).fetchone()
                depth = (row["depth"] if row else 0) + 1
            return {"profile": p, "parent_id": parent, "round": cur_round, "depth": depth}

    round_no = max(1, cur_round)
    while round_no <= cur_round + 200:  # 防死循环上限
        # User interjections are handled after the active turn, before resuming members.
        for message in conn.execute(
            "SELECT id,depth FROM messages WHERE thread_id=? AND sender='user' AND round=? ORDER BY id",
            (thread_id, round_no),
        ).fetchall():
            reply = _dfs_subtree(conn, thread_id, message["id"], round_no, message["depth"], skipped)
            if reply:
                return reply
        for p in members:
            slot = f"r:{round_no}:{p}"
            if slot in skipped:
                continue
            if not _round_spoken(conn, thread_id, round_no, p):
                if db.is_builtin_user(p):
                    continue  # 不唤醒 user（人主动发言，不阻塞轮次）
                return {"profile": p, "parent_id": None, "round": round_no, "depth": 0}
            # p 已独立发言，DFS 它的子树（@ 边）
            p_msg = conn.execute(
                "SELECT id FROM messages "
                "WHERE thread_id=? AND sender=? AND round=? AND parent_id IS NULL ORDER BY id LIMIT 1",
                (thread_id, p, round_no),
            ).fetchone()
            if p_msg:
                sub = _dfs_subtree(conn, thread_id, p_msg["id"], round_no, 0, skipped)
                if sub:
                    return sub
        # 本轮所有成员独立发言完（或 user 跳过）
        if _round_all_pass(conn, thread_id, round_no, members):
            return None  # 全员 pass/弃权 → 收口
        round_no += 1
    return None


def _set_turn(
    conn: sqlite3.Connection, thread_id: int, profile: str, parent_id: int | None, round_no: int
) -> None:
    """记录「当前轮到谁 + parent_id + 轮次 + 唤醒时间戳」（超时跳过用）。"""
    current = conn.execute("SELECT turn_profile,turn_parent_id,round FROM threads WHERE id=?", (thread_id,)).fetchone()
    if current and (current["turn_profile"], current["turn_parent_id"], current["round"]) == (profile, parent_id, round_no):
        return
    conn.execute(
        "UPDATE threads SET turn_profile=?, turn_parent_id=?, turn_wake_at=?, round=? WHERE id=?",
        (profile, parent_id, db.now_iso(), round_no, thread_id),
    )


def _clear_turn(conn: sqlite3.Connection, thread_id: int) -> None:
    """清空「轮到谁」（表态落地后，下一条消息重新算下一个）。保留 round。"""
    conn.execute(
        "UPDATE threads SET turn_profile='', turn_parent_id=NULL, turn_wake_at='' WHERE id=?",
        (thread_id,),
    )


TURN_TIMEOUT_SECONDS = 180  # 顺序唤醒超时：轮到谁 3 分钟没表态 → 弃权跳过（对齐扫描器周期，req#30：300→180）


def advance_past_profile(
    conn: sqlite3.Connection, profile: str, project: str = "", *, commit: bool = True
) -> int:
    """把卡在该 profile 身上的 turn 立即推进（弃权该发言位 → 唤醒下一个）。

    用途：成员被停用（active=0）时，对它正占着的 open 话题立刻推进——不等
    TURN_TIMEOUT_SECONDS（req bot-group#23 规格 27.3 第 4 点）。
    req#27：带 project 作用域——只推进该项目里它占着的 turn，不跨项目误推进。

    与 advance_timed_out_turns / heartbeat._advance_turn_on_exit 复用同一套
    弃权语义：弃权单位 = 发言位（turn_parent_id 区分 @ 边 / 轮次发言）。
    返回推进的话题数。
    commit=False 时，话题推进与后续唤醒入队由调用方统一提交。
    """
    if project:
        rows = conn.execute(
            "SELECT id, project, turn_profile, turn_parent_id, round FROM threads "
            "WHERE status='open' AND turn_profile=? AND project=?",
            (profile, project),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, project, turn_profile, turn_parent_id, round FROM threads "
            "WHERE status='open' AND turn_profile=?",
            (profile,),
        ).fetchall()
    advanced = 0
    for r in rows:
        skipped = _skipped(conn, r["id"])
        if r["turn_parent_id"] is not None:
            slot = f"e:{r['turn_parent_id']}:{profile}"
        else:
            slot = f"r:{r['round']}:{profile}"
        skipped.add(slot)
        conn.execute(
            "UPDATE threads SET turn_skipped=?, turn_profile='', turn_parent_id=NULL, "
            "turn_wake_at='' WHERE id=?",
            (json.dumps(sorted(skipped), ensure_ascii=False), r["id"]),
        )
        nxt = _next_speaker(conn, r["id"])
        if nxt is not None:
            _set_turn(conn, r["id"], nxt["profile"], nxt["parent_id"], nxt["round"])
            wake.enqueue(conn, nxt["profile"], "chat", "scan:group",
                         thread_id=r["id"], project=r["project"], commit=False)
        else:
            settle_thread(conn, r["id"])
        advanced += 1
    if commit:
        conn.commit()
    return advanced


def advance_timed_out_turns(conn: sqlite3.Connection) -> int:
    from .session_adapter import _SWITCH_LOCK
    # Match spawn's lock-before-write ordering.
    conn.commit()
    with _SWITCH_LOCK:
        return _advance_timed_out_turns(conn)


def _advance_timed_out_turns(conn: sqlite3.Connection) -> int:
    """顺序唤醒超时兜底：轮到谁超时没表态 → 弃权该发言位 + 唤醒下一个。

    沉默（读了不回）和崩溃（醒来就死）都会让 turn 卡住。这里把超时的
    发言位标记弃权（turn_skipped），唤醒下一个。返回推进的话题数。

    req#50 task#87 收口判定纳入活跃状态：超时弃权只作用于「读都没读」的 bot——
    已读/正在思考的 bot 不该被超时抢发言位。判定事实源=心跳表（heartbeat.pids_of，
    MCP group_read_inbox 时起心跳续租）：有心跳=活跃，跳过不弃权；无心跳=真没读，
    才按超时弃权。
    """
    import time as _time
    from . import heartbeat as _hb

    rows = conn.execute(
        "SELECT id, project, turn_profile, turn_parent_id, turn_wake_at, round FROM threads "
        "WHERE status='open' AND turn_profile != '' AND turn_wake_at != ''"
    ).fetchall()
    advanced = 0
    now = _time.time()
    for r in rows:
        from . import project_readiness
        if not project_readiness.is_ready(conn, r['project']):
            continue
        try:
            t = _time.mktime(_time.strptime(r["turn_wake_at"], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if now - t < TURN_TIMEOUT_SECONDS:
            continue
        if wake.topic_busy(conn, r['id'], r['project']):
            continue
        recovery = f"recover:chat:{r['round']}:{r['turn_parent_id'] if r['turn_parent_id'] is not None else 'root'}"
        if conn.execute(
            "SELECT 1 FROM wake_jobs WHERE profile=? AND project=? AND kind='chat' "
            "AND thread_id=? AND trigger=? AND status IN ('queued','running') LIMIT 1",
            (r["turn_profile"], r["project"], r["id"], recovery),
        ).fetchone():
            continue
        # req#50 task#87：该 bot 还活跃（TTL 内有心跳=已读/正在思考）→ 不弃权，等它回。
        # 用 pids_alive（TTL 感知）而非 pids_of——pids_of 不过滤过期心跳，崩溃 bot 的
        # 残留会被误判活跃并阻止超时弃权。
        # req#51 task#93 会话语义：按「本话题」判活跃（thread_id=r["id"]）——同一
        # profile 的 work 进程在别的活、它这个 chat 话题进程已死，不再被 work 心跳误判
        # 活跃并阻止该话题超时推进。
        if _hb.pids_alive(r["turn_profile"], thread_id=r["id"]):
            continue
        # 超时：弃权当前 turn 的发言位（判定单位 = 发言位，不是 bot）
        skipped = _skipped(conn, r["id"])
        if r["turn_parent_id"] is not None:
            slot = f"e:{r['turn_parent_id']}:{r['turn_profile']}"
        else:
            slot = f"r:{r['round']}:{r['turn_profile']}"
        skipped.add(slot)
        conn.execute(
            "UPDATE threads SET turn_skipped=?, turn_profile='', turn_parent_id=NULL, turn_wake_at='' WHERE id=?",
            (json.dumps(sorted(skipped), ensure_ascii=False), r["id"]),
        )
        nxt = _next_speaker(conn, r["id"])
        if nxt is not None:
            _set_turn(conn, r["id"], nxt["profile"], nxt["parent_id"], nxt["round"])
            wake.enqueue(conn, nxt["profile"], "chat", "scan:group", thread_id=r["id"], project=r["project"])
        advanced += 1
    conn.commit()
    return advanced


def validate_mention_scope(conn: sqlite3.Connection, text: str | None,
                          project: str, members: list[str] | None = None) -> list[str]:
    """Reject out-of-scope bot mentions before writing messages or deliveries."""
    mentions = parse_mentions(text or "", conn)
    for profile in mentions:
        if db.is_builtin_user(profile):
            continue
        if members is not None and profile not in members:
            raise RouteError(f"无法 @{profile}：该 bot 不在当前话题成员名单中，请选择话题内成员。", 400)
        if not _profile_active(conn, project, profile):
            raise RouteError(f"无法 @{profile}：该 bot 未在项目 {project} 中启用，请选择当前项目成员。", 400)
    return mentions


def route_message(
    conn: sqlite3.Connection,
    sender: str,
    text: str | None,
    sender_type: str,
    thread_id: int | None,
    project: str = "xjk",
    attachment_ids: list[str] | None = None,
    need_reply: int = 1,
    execution: dict | None = None,
) -> dict:
    """落一条消息并完成 inbox 分发 + 唤醒入队。

    返回 {message_id, mentions, inbox_profiles, wake: [(profile, kind, trigger)]}。
    project：消息归属项目（点对点由前端传；话题消息由调用方从 thread 取）。
    need_reply（req#55）：1=必须回复（窄闸门照旧），0=原发送者声明不需要回复，
    被 @ 者可静默标已读、窄闸门豁免。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    silent = text is None
    skip = silent or (not attachment_ids and _is_ack_message(text))
    if silent and (sender_type != "bot" or thread_id is None):
        raise RouteError("null 仅用于 bot 的话题独立发言位。", 400)
    if thread_id is not None:
        thread = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if thread is None or thread["status"] == "closed":
            raise RouteError("话题不存在或已关闭。", 400)
        project = thread["project"]
        members = _thread_members(conn, thread_id)
        mentions = validate_mention_scope(conn, text, project, members)
        if thread["status"] == "settled":
            if sender != "user":
                raise RouteError("话题已收口，等待用户新消息恢复讨论。", 409)
            conn.execute("UPDATE threads SET status='open',round=round+1,turn_profile='',"
                         "turn_parent_id=NULL,turn_wake_at='' WHERE id=?", (thread_id,))
        if sender != "user" and sender not in _thread_members(conn, thread_id):
            raise RouteError("你不是该话题成员。", 400)
        if sender_type == "bot":
            if thread["turn_profile"] != sender:
                raise RouteError("请等待你的发言位。", 409)
            if skip and thread["turn_parent_id"] is not None:
                raise RouteError("被 @ 必须给出实际回应，不能传 null 或 pass。", 400)
    else:
        mentions = validate_mention_scope(conn, text, project)

    # 无 @ 且无 thread_id → 400 拒绝
    if not mentions and thread_id is None:
        raise RouteError("无 @ 且无话题归属的消息不允许存在（400）", 400)

    # 保留发送方的回复意图；接收者的有效要求由 delivery 统一计算。
    if sender != "user":
        mentions = [m for m in mentions if m != sender]

    is_ack = skip

    ts = db.now_iso()

    # 点对点互@达到上限时终止投递，仅写入 engine.log。
    # 🔴 触发超限归零和报错同一分支一起做：落一条 engine 标记消息（作归零点），
    # 否则被拒不落库 → 旧 6 条永远满 → 这对 bot 永久锁死（验收③：报错后第 1 条必须能发）。
    if thread_id is None and sender != "user" and _pingpong_breach(conn, sender, mentions, ts, project):
        other = next((p for p in mentions if p != "user"), "?")
        marker_mentions = json.dumps(sorted([sender, other]), ensure_ascii=False)
        conn.execute(
            "INSERT INTO messages (project, ts, sender, sender_type, content, mentions, thread_id, round, depth) "
            "VALUES (?, ?, 'engine', 'engine', ?, ?, NULL, 0, 0)",
            (project, ts, PINGPONG_MARKER, marker_mentions),
        )
        # 🔴 归零点必须先 commit 再报错：报错会跳过末尾 commit 触发事务回滚，
        # 若不显式提交，marker 不落库 → 归零失效 → 这对 bot 永久锁死。
        conn.commit()
        logger.info("互@回环断链：%s 与 %s 已互@满 %d 轮，拒绝第 7 条消息（不投递不通知用户，已归零）",
                    sender, other, PINGPONG_MAX_ROUNDS)
        raise RouteError(PINGPONG_ERR_MSG, 400)

    # 确定 parent_id/round/depth（话题消息）
    parent_id = None
    round_no = 0
    depth = 0
    if thread_id is not None:
        turn = conn.execute(
            "SELECT turn_profile, turn_parent_id FROM threads WHERE id=?", (thread_id,)
        ).fetchone()
        if turn and turn["turn_profile"] == sender and turn["turn_parent_id"] is not None:
            # 子树接话：parent_id = 唤醒它的消息，round/depth 从父消息继承
            parent_id = turn["turn_parent_id"]
            parent = conn.execute(
                "SELECT round, depth FROM messages WHERE id=?", (parent_id,)
            ).fetchone()
            round_no = parent["round"] if parent else 0
            depth = (parent["depth"] if parent else 0) + 1
        else:
            # 轮次独立发言（或 user 主动发言）
            round_no = _thread_round(conn, thread_id)
            depth = 0

    if silent:
        skipped = _skipped(conn, thread_id)
        skipped.add(f"r:{round_no}:{sender}")
        conn.execute("UPDATE threads SET turn_skipped=? WHERE id=?",
                     (json.dumps(sorted(skipped)), thread_id))
        message_id = None
    else:
        cur = conn.execute(
            "INSERT INTO messages (project, ts, sender, sender_type, content, mentions, thread_id, parent_id, round, depth, need_reply) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project, ts, sender, sender_type, text, json.dumps(mentions, ensure_ascii=False), thread_id, parent_id, round_no, depth, need_reply),
        )
        message_id = cur.lastrowid

        from .message_execution import record
        record(conn, message_id, execution)

        if attachment_ids:
            from . import attachments
            attachments.bind(conn, attachment_ids, message_id, project, sender)

    # req#63：实际发言（含 pass）时清本话题、本人发言时点以前的 pending。
    # route_message 持有写事务，以消息 ID 固定边界；随后插入的消息保持未读。
    if sender_type == "bot" and thread_id is not None:
        wm = conn.execute(
            "SELECT MAX(id) AS wm FROM messages WHERE thread_id=?", (thread_id,)
        ).fetchone()["wm"]
        if wm is not None:
            conn.execute(
                "UPDATE inbox SET status='seen', seen_at=? "
                "WHERE profile=? AND status='pending' AND message_id IN "
                "(SELECT id FROM messages WHERE thread_id=? AND id <= ?)",
                (ts, sender, thread_id, wm),
            )
        _clear_turn(conn, thread_id)

    wake_list: list[tuple[str, str, str]] = []  # (profile, kind, trigger)

    if thread_id is None:
        # 点对点 @：只给被 @ 者插行，即时唤醒（work）
        # req#57 根因C（self-mention 自污染）：bot 正文 @ 了自己时，mentions 会含 sender
        # 自己——必须排除，否则给自己插 pending + 唤醒自己，静默回执型消息自我唤醒成永动机
        # （话题路径本就在 recipients 排除 sender，点对点这里漏了，对齐）。
        recipients = [m for m in mentions if m != sender]
        _insert_inbox_rows(conn, message_id, recipients, project)
        for p in recipients:
            wake_list.append((p, "work", "@msg"))
    else:
        # 话题消息
        if _thread_status(conn, thread_id) == "closed":
            raise RouteError(f"话题 {thread_id} 已关闭，不能再发言（400）", 400)
        members = _thread_members(conn, thread_id)
        # 发言权按话题成员表校验，内置 user 具有额外访问权限。
        if sender != "user" and sender not in members:
            raise RouteError(
                f"{sender} 不是话题 {thread_id} 的成员，不能发言（400）", 400
            )
        # pass 不插 inbox 行（免分发，防死循环）；实质发言给其他成员插行
        if not is_ack:
            recipients = [m for m in members if m != sender]
            _insert_inbox_rows(conn, message_id, recipients, project)

        # 更新 last_active_at + 隐藏话题有新消息自动展示（hidden 置回 0）
        conn.execute(
            "UPDATE threads SET last_active_at = ?, hidden = 0 WHERE id = ?", (ts, thread_id)
        )

        # 树模型：计算下一个发言者（DFS），唤醒一个
        nxt = _next_speaker(conn, thread_id)
        if nxt is not None:
            _set_turn(conn, thread_id, nxt["profile"], nxt["parent_id"], nxt["round"])
            wake_list.append((nxt["profile"], "chat", "thread-open"))
        else:
            # 某轮全员 pass/弃权后暂停自动讨论，等待用户新消息。
            settle_thread(conn, thread_id)

    conn.commit()

    # 入队唤醒（去重由 partial unique index 保证）
    enqueued = []
    for p, kind, trigger in wake_list:
        if wake.enqueue(conn, p, kind, trigger, message_id, thread_id=thread_id, project=project):
            enqueued.append((p, kind, trigger))

    return {
        "message_id": message_id,
        "mentions": mentions,
        "inbox_profiles": [p for p, _, _ in wake_list],
        "wake": enqueued,
    }
