"""周期扫描器（180s，req#30：300→180）——五来源，产出 wake_jobs（同队列、同去重）。

1. 群消息：inbox 有 pending 未读的成员（点对点与话题消息都算）。
2. 任务池（只读不写）：可领（pending + next_bot 匹配 + 前置依赖均 done）/
   催办（claim 后 2h 无 node）/ 挂死重投。饥饿 >24h 只标红（不打扰人）。
3. 话题静默：open 话题 last_active_at 超时（默认 30min）→ 唤醒发起者评估收口。
4. 话题全员弃权：open 话题某轮全员 pass/弃权 → 暂停自动讨论。
5. 需求拆解：status=open 且待拆（decompose_state=pending）或拆解中超时（decomposing 且
   decompose_expires_at 过期）的需求 → 按项目确定性选一名 task.create 授权成员唤醒；
   无候选时写 gate_reason 并唤醒项目负责人。已拆完（done）/占用未超时的需求不扫。

扫描器只读任务池、只写 wake_jobs，不碰 inbox 的 seen（红线：只有 bot 自己能标 seen）。
"""

from __future__ import annotations

import json
import logging
import time

from . import db, events, router, wake
from .delivery import effective_need_reply

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 180
THREAD_IDLE_SECONDS = 30 * 60  # 话题静默超时，默认 30min
P2P_STALE_SECONDS = 30 * 60  # req#52 根因A：点对点 pending 超 30min 未读不再作为唤醒源
CLAIM_STALE_SECONDS = 2 * 3600  # claim 后 2h 无 node → 催办
STARVE_SECONDS = 24 * 3600  # 可领超 24h → 只标红


def _parse_ts(iso: str) -> float | None:
    try:
        return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _finalize_stale_group(conn, profile: str, project: str) -> int:
    """req#82 R2：整组陈旧 → 显式 failed 终态 + 通知（不静默丢）。

    有界重投语义：pending 在 P2P_STALE_SECONDS 内由扫描器周期重投（唤醒），
    超时即停止重投。这里把「静默停止」改为「显式 failed 终态 + 通知」——
    判定粒度沿用 latest_by_recipient 的 MAX（整组，非逐条），动作终态化，
    原始消息保留（append-only 红线）。失败按 (message_id, recipient_profile)
    隔离（inbox.status='failed'），转 failed 后不再 pending，天然仅通知一次。

    返回转为 failed 的行数。
    """
    cur = conn.execute(
        "UPDATE inbox SET status='failed' "
        "WHERE profile=? AND status='pending' AND message_id IN "
        "(SELECT id FROM messages WHERE thread_id IS NULL AND project=?)",
        (profile, project),
    )
    conn.commit()
    n = cur.rowcount
    if n == 0:
        return 0
    from . import task_retry
    targets = task_retry.notification_targets(conn, project, profile)
    mentions = " ".join("@" + t for t in targets)
    report = router.route_message(
        conn,
        'engine',
        f"{mentions} {profile} 有 {n} 条点对点消息超过 30 分钟未读，"
        f"已按整组陈旧转为显式失败终态（inbox.status='failed'），不再重投。",
        "engine",
        None,
        project=project,
        need_reply=None,
    )
    events.bus.publish("message.new", {"message_id": report["message_id"],
                                       "sender": "engine", "thread_id": None, "project": project})
    return n


def scan_inbox(conn) -> int:
    """来源1：inbox 有 pending 未读的成员 → 入队唤醒。

    树模型：话题消息（thread_id 非空）只唤醒该话题
    「下一个该发言的发言位」（DFS 遍历），不是所有 pending 成员——否则扫描器
    每个周期（180s）把全体叫醒，破坏「一次只醒一个」的顺序。点对点消息（thread_id 空）
    照旧逐个唤醒被 @ 者（work）。

    只唤醒 members 表里的 bot（active=1）——user/非成员积压是死数据。
    """
    from . import router
    from . import project_readiness

    enqueued = 0
    now_ts = time.time()

    # 点对点：pending 且 thread_id 空 → 逐个唤醒（work）
    # req#27：启停按项目维度（project_members），在 Python 侧逐行判（行数少，可接受）
    # req#52 根因A：点对点 pending 加时效——但判据必须是「存在未超时效的 pending 才唤醒」
    # 使用 MAX(m.ts) 读取最新时间；MIN 在混合场景（陈旧 40min + 新鲜 1min 并存）
    # 会取到陈旧时间并跳过整组，
    # 新鲜 @ 被陈旧死行永久压制、陈旧行不唤醒又永远读不掉 → 死锁。正确语义：只要还有
    # 新鲜 @ 就唤醒（bot 上线一次拉回全部未读，陈旧行随之自然清理）；全陈旧才不唤醒
    # （陈旧行由 bot 因其他原因上线时兜底读，不让扫描器为它专打枪成永动机）。
    p2p = conn.execute(
        """
        SELECT i.profile, m.project, m.ts, m.mentions, m.need_reply
        FROM inbox i
        JOIN messages m ON m.id = i.message_id
        WHERE i.status = 'pending' AND m.thread_id IS NULL
          AND i.profile IN (SELECT profile FROM members WHERE profile != ?)
        """,
        (db.BUILTIN_USER_PROFILE,),
    ).fetchall()
    latest_by_recipient = {}
    for message in p2p:
        if effective_need_reply(message, message["profile"]) is None:
            continue
        key = (message["profile"], message["project"])
        latest_by_recipient[key] = max(latest_by_recipient.get(key, ""), message["ts"])
    for (profile, project), latest_ts in latest_by_recipient.items():
        if not project_readiness.is_ready(conn, project):
            continue
        r = {"profile": profile, "project": project, "latest_ts": latest_ts}
        if not router._profile_active(conn, r["project"], r["profile"]):
            continue  # 该项目内停用 → 不唤醒（req#27）
        # req#52 根因A：最新一条 pending 都超 30min（=全陈旧）→ 不唤醒；否则有新鲜 @ 就唤醒
        latest = _parse_ts(r["latest_ts"]) if r["latest_ts"] else None
        if latest is not None and now_ts - latest > P2P_STALE_SECONDS:
            # req#82 R2：整组陈旧 → 显式 failed 终态 + 通知（不静默丢）。
            from .session_adapter import _SWITCH_LOCK
            with _SWITCH_LOCK:
                if wake.session_busy(profile, 'work', None, project):
                    continue
                _finalize_stale_group(conn, profile, project)
            continue
        if wake.enqueue(conn, r["profile"], "work", "scan:group", project=r["project"]):
            enqueued += 1

    # 话题：按 thread 分组，每个 thread 只唤醒「下一个该发言的发言位」（chat）
    threads = conn.execute(
        """
        SELECT DISTINCT m.thread_id, m.project
        FROM inbox i
        JOIN messages m ON m.id = i.message_id
        WHERE i.status = 'pending' AND m.thread_id IS NOT NULL
        """
    ).fetchall()
    for t in threads:
        if not project_readiness.is_ready(conn, t['project']):
            continue
        tid = t["thread_id"]
        row = conn.execute("SELECT status, turn_profile FROM threads WHERE id=?", (tid,)).fetchone()
        if row is None or row["status"] != "open":
            continue
        # req#59 修法（chat 空转根因）：turn 已被占着时，scanner 不再拿「该话题还有
        # pending」当理由去空转唤醒 turn_profile。chat 的推进纯靠「发言即推进 + 超时
        # 弃权」两个驱动（route_message 的 _next_speaker+_set_turn / advance_timed_out_turns），
        # 与 pending 无关。turn_profile 自己 pending 已空（已实质回应过）时，这里再
        # enqueue 只会让它醒来读空、空转（req#59 空转 bug 的机制）。只有 turn 为空
        # （发言推进链断了）才轮到 scanner 兜底 _next_speaker 重算。
        if row["turn_profile"]:
            continue
        nxt = router._next_speaker(conn, tid)
        if nxt is not None:
            router._set_turn(conn, tid, nxt["profile"], nxt["parent_id"], nxt["round"])
            if wake.enqueue(conn, nxt["profile"], "chat", "scan:group", thread_id=tid, project=t["project"]):
                enqueued += 1

    return enqueued


def scan_tasks(conn) -> dict:
    """来源2：任务池只读扫描（方案B：单库 group.db，遍历所有非归档项目）。

    返回 {claimable, stale, starved} 计数。
    """
    now = time.time()

    # 所有非归档项目
    projects = [r["name"] for r in conn.execute(
        "SELECT name FROM projects WHERE archived=0"
    ).fetchall()]
    if not projects:
        return {"claimable": 0, "stale": 0, "starved": 0}

    enqueued = 0
    stale_enqueued = 0
    starved = 0

    for project in projects:
        from . import project_readiness
        if not project_readiness.is_ready(conn, project):
            continue
        # 可领：pending 且 next_bot 非空
        claimable = conn.execute(
            "SELECT t.* FROM tasks t JOIN requirements r ON r.id=t.requirement_id "
            "WHERE t.project=? AND t.status='pending' AND t.next_bot != '' "
            "AND r.status IN ('open','blocked') AND r.archived=0",
            (project,),
        ).fetchall()

        # 催办：in_progress 且 claimed_at 超 2h
        stale = conn.execute(
            "SELECT t.* FROM tasks t JOIN requirements r ON r.id=t.requirement_id "
            "WHERE t.project=? AND t.status='in_progress' "
            "AND r.status IN ('open','blocked') AND r.archived=0",
            (project,),
        ).fetchall()

        # 饥饿：pending 超 24h（只标红，不唤醒）
        for c in claimable:
            t = _parse_ts(c["created_at"])
            if t and now - t > STARVE_SECONDS:
                starved += 1

        # Exact assignee only. Roles help Bots select teammates from the roster;
        # the scheduler dispatches the selected concrete member.
        for c in claimable:
            from .task_brief import eligible
            if not eligible(conn, c):
                continue
            member = c["next_bot"]
            if not member or not router._profile_active(conn, project, member):
                continue
            if wake.enqueue(conn, member, "work", "scan:task", project=project):
                enqueued += 1

        # 催办只唤醒实际领取人。
        for st in stale:
            from .task_brief import dependency_blockers
            if dependency_blockers(conn, st):
                continue
            t = _parse_ts(st["claimed_at"])
            if not (t and now - t > CLAIM_STALE_SECONDS and st["claimed_by"]):
                continue
            member = st['claimed_by']
            if router._profile_active(conn, project, member) and wake.enqueue(
                    conn, member, "work", "scan:task", project=project):
                stale_enqueued += 1

    return {
        "claimable": enqueued,
        "stale": stale_enqueued,
        "starved": starved,
    }


def scan_thread_idle(conn) -> int:
    from .session_adapter import _SWITCH_LOCK
    # Earlier scan phases may retain a writer after a duplicate enqueue.
    conn.commit()
    with _SWITCH_LOCK:
        return _scan_thread_idle(conn)


def _scan_thread_idle(conn) -> int:
    """来源3：讨论静默超时后暂停自动发言，等待用户恢复。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        """
        SELECT t.id, t.creator, t.last_active_at, t.project
        FROM threads t
        WHERE t.status='open'
        """
    ).fetchall()
    now = time.time()
    enqueued = 0
    for r in rows:
        from . import project_readiness
        if not project_readiness.is_ready(conn, r['project']):
            continue
        t = _parse_ts(r["last_active_at"])
        if t and now - t > THREAD_IDLE_SECONDS:
            from . import router
            if wake.topic_busy(conn, r['id'], r['project']):
                continue
            router.settle_thread(conn, r["id"])
            enqueued += 1
    conn.commit()
    if enqueued:
        events.bus.publish("thread.updated", {})
    return enqueued


def scan_thread_all_responded(conn) -> int:
    from .session_adapter import _SWITCH_LOCK
    conn.commit()
    with _SWITCH_LOCK:
        return _scan_thread_all_responded(conn)


def _scan_thread_all_responded(conn) -> int:
    """来源4：open 话题某轮全员 pass/弃权 → 暂停自动讨论。

    树模型（2026-09-11）：收口判定 = `_next_speaker` 返回 None（某轮全员
    pass/弃权），不是旧的「全员发过言」。旧语义「发过言=表态」会在 Round 1
    全员实质发言后误判收口，打断第二轮讨论。
    """
    from . import router

    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")

    rows = conn.execute(
        """
        SELECT t.id, t.creator, t.project
        FROM threads t
        WHERE t.status='open'
        """
    ).fetchall()
    enqueued = 0
    for r in rows:
        from . import project_readiness
        if not project_readiness.is_ready(conn, r['project']):
            continue
        if router._next_speaker(conn, r["id"]) is None:
            if wake.topic_busy(conn, r['id'], r['project']):
                continue
            router.settle_thread(conn, r["id"])
            enqueued += 1
    conn.commit()
    if enqueued:
        events.bus.publish("thread.updated", {})
    return enqueued


def _pick_decompose_candidate(conn, project):
    """确定性选一名有 task.create 权限的有效成员（role_id 升序 + profile 字典序，复用 #198）。

    返回 profile 或 None（无候选）。
    """
    from . import project_permissions
    granted = set(project_permissions.granted_profiles(conn, project, "task.create"))
    if not granted:
        return None
    for row in conn.execute(
        "SELECT pm.profile FROM project_members pm "
        "JOIN members m ON m.profile = pm.profile "
        "WHERE pm.project=? AND COALESCE(pm.active,0)=1 AND m.profile<>? "
        "ORDER BY COALESCE(pm.role_id,0) ASC, pm.profile ASC",
        (project, db.BUILTIN_USER_PROFILE),
    ):
        if row["profile"] in granted:
            return row["profile"]
    return None


def scan_requirements(conn) -> int:
    """扫描待拆/超时需求，按项目确定性选一名有效授权成员恢复拆解（不再全部唤醒）。"""
    from . import project_readiness
    from . import requirement_gate
    now = db.now_iso()
    rows = conn.execute(
        """
        SELECT r.*
        FROM requirements r
        JOIN projects p ON p.name = r.project AND p.archived = 0
        WHERE r.status = 'open' AND r.archived = 0
          AND (
                r.decompose_state = 'pending'
                OR (r.decompose_state = 'decomposing'
                    AND (r.decompose_expires_at = '' OR r.decompose_expires_at < ?))
              )
        """,
        (now,),
    ).fetchall()
    enqueued = 0
    for r in rows:
        if not project_readiness.is_ready(conn, r['project']):
            continue
        blocked = requirement_gate.check(conn, r)
        if blocked:
            # req#111 task#192：未过门禁 → 把 check 返回原因写回 gate_reason，
            # 供前端标「待拆解授权 + 原因」，不再静默丢弃 continue、两头拿不到原因。
            reason = blocked['error'] if isinstance(blocked, dict) else str(blocked)
            conn.execute('UPDATE requirements SET gate_reason=? WHERE id=?', (reason, r['id']))
            conn.commit()
            continue
        # 过门禁 → 清空 gate_reason（历史残留不误标）。WHERE gate_reason<>'' 守卫避免每轮无谓写+commit。
        conn.execute("UPDATE requirements SET gate_reason='' WHERE id=? AND gate_reason<>''", (r['id'],))
        conn.commit()
        candidate = _pick_decompose_candidate(conn, r['project'])
        if candidate is None:
            # req#117 task#200：无候选 → 写 gate_reason 明确原因 + 唤醒项目负责人处理配置（不再静默漏拆）。
            reason = ("无 task.create 授权成员：请在项目设置中为至少一名有效成员授予 task.create 权限"
                      "（或为该成员配置持有该权限的角色）后再拆解。")
            conn.execute("UPDATE requirements SET gate_reason=? WHERE id=?", (reason, r['id']))
            conn.commit()
            owner = conn.execute(
                "SELECT value FROM settings WHERE project=? AND key='owner_profile'", (r['project'],)
            ).fetchone()
            if owner and owner['value']:
                if wake.enqueue(conn, owner['value'], "work", "scan:req", project=r['project']):
                    enqueued += 1
            continue
        if wake.enqueue(conn, candidate, "work", "scan:req", project=r["project"]):
            enqueued += 1
    return enqueued


def run_once(conn) -> dict:
    """执行一轮扫描，返回各来源计数。"""
    from . import router
    from . import steer_ledger
    steer_ledger.reconcile_targets(conn)
    steer_ledger.expire_stale(conn)
    result = {
        "inbox": scan_inbox(conn),
        "tasks": scan_tasks(conn),
        "thread_idle": scan_thread_idle(conn),
        "thread_all_responded": scan_thread_all_responded(conn),
        "requirements": scan_requirements(conn),
        "turn_timeout": router.advance_timed_out_turns(conn),
    }
    return result


def run_forever(stop_event) -> None:
    """扫描器主循环（后台线程）。"""
    while not stop_event.is_set():
        if wake._shutting_down.is_set():
            stop_event.wait(0.1)
            continue
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            run_once(conn)
        except Exception:  # noqa: BLE001
            logger.exception("scanner tick error")
        finally:
            conn.close()
        stop_event.wait(SCAN_INTERVAL_SECONDS)
