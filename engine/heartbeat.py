"""心跳归属：进程内 TTL 表 + 引擎侧 chat 窗口关联。

事实源修正（2026-09-08，hermes 查证 HERMES_WAKE_THREAD_ID 被
gateway _build_safe_env 过滤——与 HERMES_CHAT_MODE 同一个坑）：
MCP 进程里的 HERMES_WAKE_THREAD_ID 恒为空，环境变量带不出 thread_id。

会话心跳按 thread_id 隔离：
正确来源 = MCP 进程启动时从父进程命令行 -c {profile}-{project}-chat-{tid}
解析出自己的 thread_id（_is_chat_process 已经在反查这条命令行，扩成解析出
tid），心跳/inbox 显式带上。

心跳表 _beats 从 profile->{pid:ts} 升级为 profile->{pid:{ts,thread_id}}，
按「话题会话」分槽：chat 进程心跳带自己的 thread_id，超时判活跃只认本话题，
work 心跳（thread_id=None）不再替 chat 话题背书（req#50 遗留的 profile 级混槽）。

work/点对点心跳 thread_id=None（本就只在总视图显示，无归属需求）。
"""
from __future__ import annotations

import threading
import time

_TTL_SECONDS = 90          # 心跳过期：MCP 续租 30s，90s 无续租视为进程已退
_lock = threading.Lock()
# 引用计数模型：work/chat 进程可以并行，
# profile 单值心跳会在 chat 退出时误清仍在执行的
# work 心跳（思考条闪灭 30s）。改为 pid 集合：beat 记 pid，on_exit(pid)
# 只减退出的那个进程，集合空才下线。
# req#51 task#93：pid 槽升级为 {ts, thread_id}——按话题会话分槽，超时判活跃
# 只认「本话题」的心跳，work 心跳不再替 chat 话题背书。
_beats: dict[str, dict[int, dict]] = {}   # profile -> {pid: {"ts": float, "thread_id": int|None}}


def on_exit(profile: str, pid: int | None = None, thread_id: int | None = None) -> None:
    """子进程退出事件（wake._spawn 的 reaper 线程调）：只清该 pid/该话题的心跳。

    req#51 task#93 会话语义：thread_id 非 None 时只清「该话题」的心跳槽
    （chat 会话退出，不影响并行中的 work/其他话题）；thread_id=None 且
    pid=None 时清整个 profile（保守语义，work 全退兜底，仅旧行为）。
    集合空 profile 才真正下线，chat 退出不影响并行中的 work 灯。
    """
    with _lock:
        pids = _beats.get(profile)
        if pids is not None:
            if thread_id is not None:
                # 只清该话题的心跳槽（req#51：chat 退出按会话精确，不误伤 work/他话题）
                for k in [k for k, v in pids.items() if v.get("thread_id") == thread_id]:
                    pids.pop(k, None)
            elif pid is not None:
                pids.pop(pid, None)
            else:
                pids.clear()  # 旧兜底：清整个 profile
            if not pids:
                _beats.pop(profile, None)
    # SSE 思考条（2026-09-10）：心跳下线即推（reaper 线程调，publish 内部 threadsafe）。
    # 两个分支都必须推送；pid=None 分支用于 reaper 未传 pid 的情况，
    # 之前 return 提前退出漏推，导致「退出即消失」退化 60s 兜底——publish 提到锁外、两分支都走。
    from . import events
    events.bus.publish("thinking.changed", {"profile": profile})
    # Only a topic execution's exit can advance that topic's turn.
    from . import wake
    if not wake._shutting_down.is_set():
        _advance_turn_on_exit(profile, thread_id)


def _advance_turn_on_exit(profile: str, thread_id: int | None = None) -> None:
    """bot 进程退出（醒来就死）→ 若正卡在话题 turn 上，立即弃权 + 唤醒下一个。

    复用 router.advance_timed_out_turns 的弃权逻辑，但按 profile 精确触发、不等超时。
    树模型（2026-09-11）：弃权单位 = 发言位（turn_parent_id 区分 @ 边 / 轮次发言）。
    thread_id=None 表示 work 或缺少话题身份，只清理心跳，保留话题轮次。
    """
    from . import db, wake
    from . import router

    if thread_id is None:
        return
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, project, turn_parent_id, round FROM threads "
            "WHERE status='open' AND turn_profile=? AND id=?",
            (profile, thread_id),
        ).fetchall()
        for r in rows:
            if conn.execute(
                "SELECT 1 FROM wake_jobs WHERE profile=? AND thread_id=? AND status IN ('queued','running') LIMIT 1",
                (profile, r["id"]),
            ).fetchone():
                continue
            import json
            skipped = router._skipped(conn, r["id"])
            if r["turn_parent_id"] is not None:
                slot = f"e:{r['turn_parent_id']}:{profile}"
            else:
                slot = f"r:{r['round']}:{profile}"
            skipped.add(slot)
            conn.execute(
                "UPDATE threads SET turn_skipped=?, turn_profile='', turn_parent_id=NULL, turn_wake_at='' WHERE id=?",
                (json.dumps(sorted(skipped), ensure_ascii=False), r["id"]),
            )
            nxt = router._next_speaker(conn, r["id"])
            if nxt is not None:
                router._set_turn(conn, r["id"], nxt["profile"], nxt["parent_id"], nxt["round"])
                wake.enqueue(conn, nxt["profile"], "chat", "scan:group", thread_id=r["id"], project=r["project"])
            else:
                router.settle_thread(conn, r["id"])
        conn.commit()
        if rows:
            from . import events
            events.bus.publish("thread.updated", {})
    finally:
        conn.close()


def pids_of(profile: str) -> list[int]:
    """该 profile 当前心跳记录的 pid 列表（叫停兜底第二源）。"""
    with _lock:
        pids = _beats.get(profile)
        if not pids:
            return []
        return [k for k in pids.keys() if k != -1]


def pids_alive(profile: str, thread_id: int | None = None) -> list[int]:
    """TTL 感知的活跃 pid 列表——只返回 now-ts ≤ _TTL_SECONDS 的 pid。

    advance_timed_out_turns
    的活跃判定必须用这个，不能再用 pids_of——pids_of 不过滤 TTL，崩溃 bot 的
    过期心跳残留会被误判活跃，吞掉超时弃权、话题发言位无限卡住。
    顺带清理过期槽（等价 active_entries 的清理语义，但按单 profile 精确）。

    req#51 task#93 会话语义：thread_id 非 None 时只返回「该话题」的活跃 pid
    （chat 进程心跳带自己的 thread_id），work 心跳（thread_id=None 槽）不再替
    chat 话题背书——同一 profile 的 work 进程在别的活、它那个话题进程已死，
    不再误判活跃吞掉弃权。thread_id=None（缺省）= 不过滤，返回所有活跃 pid
    （兼容旧调用 / 测试）。
    """
    now = time.time()
    with _lock:
        pids = _beats.get(profile)
        if not pids:
            return []
        alive = []
        for k, v in list(pids.items()):
            if k == -1:  # 兼容旧 MCP 的单槽，不参与 TTL 判定（无真实 pid）
                continue
            if now - v["ts"] <= _TTL_SECONDS:
                if thread_id is None or v.get("thread_id") == thread_id:
                    alive.append(k)
            else:
                pids.pop(k, None)  # 过期槽清理
        if not pids:
            _beats.pop(profile, None)
        return alive


def beat(profile: str, thread_id=None, pid: int | None = None) -> None:
    """MCP 心跳续租。pid 从 /heartbeat 的 X-Hermes-Ppid 或 body 传来；
    thread_id 是 MCP 进程自己解析的会话话题（req#51 task#93）。

    pid 缺失时（兼容旧 MCP），退化为单槽（key=-1）——旧模式下并行场景
    仍有闪灭可能，但 MCP 升级后自动获得精确计数。
    """
    key = pid if pid is not None else -1
    with _lock:
        _beats.setdefault(profile, {})[key] = {"ts": time.time(), "thread_id": thread_id}
    # SSE 思考条：心跳上线时推送事件，前端据此更新状态。
    # 每个 beat 都推一次（30s 周期），前端 loadThinking 幂等，重复刷新无害；
    # 事件频次低（bot 数量级），不做去重。
    from . import events
    events.bus.publish("thinking.changed", {"profile": profile})


def active_entries(*, include_pid: bool = False) -> list[dict]:
    """[{profile, thread_id}]——TTL 内活跃的心跳，按会话（心跳槽）粒度输出。

    req#58 会话级判活：thread_id 直接取自心跳槽（beat 时 MCP 报的 thread_id），
    不再靠 DB last_wake 关联。旧实现（_thread_of_chat_window）是 profile 级单一
    归属——同 profile 双话题并行时，A 话题的心跳会被「最新一次唤醒的 thread_id」
    替 B 话题气泡背书，前端判活拿错 thread_id → 假阴性误判 inactive。
    现在每个活跃 pid 槽独立产出：chat 槽带自己的 thread_id、work 槽 thread_id=None，
    前端按会话精确判活（req#58 验收③「同 profile A 活 B 死判 B 正确」）。

    引用计数语义不变：过期 pid 逐个剔除，全部过期才下线（并行的 chat/work 互不误伤）。
    key=-1（旧 MCP 退化单槽）不参与 TTL 判定，与 pids_alive 对齐。
    """
    now = time.time()
    out: list[dict] = []
    with _lock:
        for p in list(_beats.keys()):
            pids = _beats[p]
            for k in [k for k, v in pids.items() if now - v["ts"] > _TTL_SECONDS]:
                pids.pop(k, None)
            if not pids:
                _beats.pop(p, None)
                continue
            for k, v in pids.items():
                if k == -1:  # 旧 MCP 单槽，不参与 TTL 判定（与 pids_alive 对齐）
                    continue
                entry = {"profile": p, "thread_id": v.get("thread_id")}
                if include_pid:
                    entry['pid'] = k
                out.append(entry)
    return out
