"""唤醒器：纯信号注入 + wake_jobs 入队。

唤醒通过 subprocess 启动独立宿主进程：
- subprocess hermes -p <profile> chat --in ~ -c <会话名> --create-if-missing -Q -q "<纯信号>"
- 会话名按方案 B 三层派生（session_name()）：{profile}-{project}-{type}[-{thread_id}]

信号是纯信号，不带消息内容——bot 醒后自己去群里读（GET /inbox / GET /thread）。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import db, process_query

_session_lock = threading.RLock()
_running_sessions: set[str] = set()
_running_wakes: dict[str, dict] = {}
_native_procs: dict[str, tuple] = {}
_intentional_stops: set[str] = set()
_shutting_down = threading.Event()


def active_runs() -> list[dict]:
    """Snapshot of spawned executions, including those awaiting their first heartbeat."""
    with _session_lock:
        return [dict(run) for run in _running_wakes.values()]


def mark_intentional_stop(profile: str, session: str) -> None:
    """Record stop intent before killing children so their reapers cannot auto-block."""
    with _session_lock:
        if session in _running_sessions:
            _intentional_stops.add(session)


def _handle_task_exit(profile, project, kind, session, started_at, ended_at, exit_code,
                      event_floor=None, task_id=None, error="", job_id=None):
    from . import task_retry
    with _session_lock:
        intentional = session in _intentional_stops
        if kind == 'work' and not intentional and not _shutting_down.is_set():
            reason = error or (f'process exited with code {exit_code}' if exit_code else
                               'process exited without task completion or handoff')
            from .execution_errors import MESSAGES, classify
            reason = MESSAGES[classify(reason)]
            return task_retry.finish(profile, project, task_id, f'{session}:{started_at}', reason, job_id, event_floor)
    return {"blocked": []}


def _report_execution_error(profile, project, thread_id, session, attempt, detail, **kwargs):
    with _session_lock:
        if session in _intentional_stops or _shutting_down.is_set():
            return
    try:
        from .execution_errors import report
        return report(profile, project, thread_id, attempt, detail, **kwargs)
    except Exception:
        logging.getLogger(__name__).error('Could not persist execution failure for %s', profile)


def session_busy(profile, kind, thread_id, project):
    session = session_name(profile, kind, thread_id, project)
    with _session_lock:
        if session in _running_sessions:
            return True
    return find_bot_processes(profile, session=session) != []


def topic_busy(conn, thread_id, project):
    """Protect a topic while any of its executions is alive, including after reply."""
    profiles = {r[0] for r in conn.execute(
        'SELECT profile FROM wake_runs WHERE thread_id=? AND project=? AND ended_at IS NULL AND profile IS NOT NULL',
        (thread_id, project))}
    profiles.update(r[0] for r in conn.execute(
        "SELECT profile FROM session_bindings WHERE kind='chat' AND thread_id=? AND project=?",
        (thread_id, project)))
    row = conn.execute('SELECT turn_profile FROM threads WHERE id=? AND project=?', (thread_id, project)).fetchone()
    if row and row['turn_profile']:
        profiles.add(row['turn_profile'])
    with _session_lock:
        profiles.update(r['profile'] for r in _running_wakes.values()
                        if r.get('thread_id') == thread_id and r.get('project') == project)
    return any(session_busy(profile, 'chat', thread_id, project) for profile in profiles)

# Console child processes must remain hidden when the engine runs under pythonw.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# uv trampoline 的包装进程会很快退出；实际 Bot 进程通过
# find_bot_processes 按命令行实时匹配，供停止流程定位。

def find_bot_processes(profile: str, session: str | None = None) -> "list[int] | None":
    """Match live Bot arguments; None means unknown and [] proves absence.

    Hermes launchers may wrap Python children. Match the physical profile and
    exact session title or persisted runtime ID to retain stop/restart scope.
    """
    native = _find_codex_processes(profile, session)
    if native is not False:
        return native
    physical_profile = profile
    if profile.startswith('bot-'):
        from . import bot_identity
        conn = db.get_connection()
        try:
            bot = bot_identity.global_bot(conn, profile)
            if bot:
                physical_profile = bot['profile']
        finally:
            conn.close()
    processes = process_query.list_processes({'python.exe', 'pythonw.exe'}, scope=profile)
    if processes is None:
        return None
    pids = []
    session_ids = None
    for pid, argv in processes:
        # Limit identity checks to CLI arguments before the user prompt.
        args = argv[:next((i for i, arg in enumerate(argv) if arg in ('-q', '--query', '--')), len(argv))]
        if (any('hermes' in arg.lower() for arg in args[:3]) and 'chat' in args
                and process_query.option(args, '-p', '--profile') == physical_profile):
            actual_session = process_query.option(args, '-c', '--continue')
            if session is not None and actual_session != session:
                if session_ids is None:
                    conn = db.get_connection()
                    try:
                        session_ids = {r[0] for r in conn.execute(
                            'SELECT session_id FROM session_bindings WHERE profile=? AND title=?',
                            (profile, session))}
                        session_ids.update(r[0] for r in conn.execute(
                            'SELECT runtime_session_id FROM wake_runs WHERE session=? AND runtime_session_id IS NOT NULL',
                            (session,)))
                    except sqlite3.Error as exc:
                        process_query.diagnostic(profile, 'session_bindings', exc)
                        return None
                    finally:
                        conn.close()
                if not actual_session or actual_session not in session_ids:
                    continue
            pids.append(pid)
    process_query.diagnostic(profile, 'session_bindings')
    return pids


def _find_codex_processes(profile, session):
    """Return scoped native PIDs, None on query failure, False for Hermes."""
    conn = db.get_connection()
    try:
        member = conn.execute('SELECT b.runtime FROM members m JOIN bots b ON b.id=m.bot_id '
                              'WHERE m.profile=?', (profile,)).fetchone()
        rows = conn.execute("SELECT session,started_at,pid,project FROM wake_runs WHERE profile=? "
                            "AND runtime='codex' AND (ended_at IS NULL OR exit_code=-1) "
                            + ('AND session=?' if session else ''),
                            (profile, session) if session else (profile,)).fetchall()
        if not rows and (not member or member['runtime'] != 'codex'):
            return False
    except sqlite3.Error as exc:
        process_query.diagnostic(profile, 'codex_records', exc)
        return None
    finally:
        conn.close()
    process_query.diagnostic(profile, 'codex_records')
    pids = []
    for run in rows:
        with _session_lock:
            owned = _native_procs.get(run['session'])
        if owned and owned[1] == run['started_at']:
            if owned[0].poll() is None:
                pids.append(owned[0].pid)
            continue
        if run['pid'] is None:
            process_query.diagnostic(profile, 'codex_pid', ValueError())
            return None
        argv = process_query.command_line(run['pid'], scope=profile)
        if argv is None:
            return None
        if _matches_codex_execution(argv, profile, run):
            pids.append(run['pid'])
    process_query.diagnostic(profile, 'codex_pid')
    return pids


def _matches_codex_execution(argv, profile, run):
    """Validate the exact inline execution identity before trusting a persisted PID."""
    import json
    import tomlkit

    if 'exec' not in argv or not any('codex' in arg.lower() for arg in argv[:argv.index('exec')]):
        return False
    for index, arg in enumerate(argv[:-1]):
        if arg not in ('-c', '--config') or not argv[index + 1].startswith('mcp_servers.task_queue='):
            continue
        try:
            server = tomlkit.parse(argv[index + 1])['mcp_servers']['task_queue']
            context = json.loads(server['env']['XDG_GROUP_ENGINE_EXECUTION'])
            return (isinstance(context, dict) and context.get('profile') == profile
                    and context.get('project') == run['project']
                    and context.get('session') == run['session']
                    and context.get('wake_started_at') == run['started_at'])
        except (ValueError, TypeError, KeyError):
            return False
    return False


# 信号模板（纯信号，不带消息内容，不带拉取策略——拉取策略是工具内部的事）。
# 协作规则通过 HERMES_EPHEMERAL_SYSTEM_PROMPT 注入 system prompt，
# agents.md 走 --in 项目root 由 Hermes 原生加载进 system prompt——信号瘦身到只剩 per-wake 动态指令。
# req#45（task#80）：三个信号开头统一加「[系统唤醒信号]」前缀——bot 醒来一眼分清
# 「系统指令」与「用户消息」，保持唤醒信号的来源清晰。
# work 信号：只留「有新消息」这个动态事实
_WORK_SIGNAL = "[系统唤醒信号] 你有新消息（点对点派活），调 group_read_inbox() 看未读。"
# chat 信号：只留「有新消息」这个动态事实
_CHAT_SIGNAL = "[系统唤醒信号] 你有新消息（话题讨论），调 group_read_inbox() 看未读。"
# approved 信号在用户批准后触发，携带动态 task_id，供执行者定位获批任务。
_APPROVED_SIGNAL = (
    "[系统唤醒信号] 任务 #{task_id} 已获批准，请读取该任务与批准结果后继续执行。"
)

# `<整数>` 是说明占位符，调用时填写实际数字。source_ref 沿用当前的 thread_id、message_id 两项格式。
_COMMON_SYSTEM_PROMPT = (
    "## 需求、任务链与节点（三层结构）\n"
    "任务队列是协作状态与交付记录的唯一事实源，使用 requirement → task → node 三层结构：\n"
    "```text\n"
    "requirement（需求：本轮目标、范围和验收标准）\n"
    "├─ task A（可独立验收的业务目标或模块，拆解时创建）\n"
    "│  ├─ node 1（拆解记录）\n"
    "│  ├─ node 2（执行成员完成本环节后的记录）\n"
    "│  └─ node …（评审、返修、复审、验收等实际发生的记录）\n"
    "└─ task B（另一独立目标，有自己的完整节点链）\n"
    "```\n"
    "一个 task 从拆解到验收始终保持同一个 task_id，按项目流程经过多个环节和成员。"
    "任务按业务目标、模块或可独立验收的成果范围拆分；同一目标的设计、执行、评审、返修和验收归入原 task 的节点链。"
    "每条任务链按真实依赖与资源条件独立推进，验收完成即可关闭；requirement 在其全部任务完成后关闭。\n"
    "node 是协作过程中实际产生的环节日志，每次记录都证明该成员完成了什么、依据什么交付、留下什么问题。"
    "task_create 创建 task 时生成拆解 node；后续成员按实际执行通过 task_log 追加 node，返修和复审继续追加到原 task。"
    "后续 node 随工作发生逐条产生，规划时在任务说明中写明流程与验收要求。\n"
    "拆解者调用 task_create 创建任务时，通过 next_bot 指定第一个接手人；该任务随后由多个成员按流程接力。"
    "next_bot 表示原 task 当前等待接手的成员，随交接变化。上游完成本环节后，调用 task_log，"
    "type 填本次已完成的环节，next_bot 与 expected_role_id 指定下一成员及其角色；"
    "成功后原 task 回到 pending 并转交下一成员，再通过 group_send 点对点 @ 该成员通知交接。"
    "下一成员在 work 中通过 task_claim 领取同一个 task，读取已有 node 后执行自己负责的环节。"
    "评审员的工作资格通过接手原 task 获得，独立评审由独立成员在该任务链中完成。\n"
    "本环节完成后通过 task_log 交接，整条任务链达到验收条件后，由获授权的验收成员在原 task 待其处理的 pending 状态"
    "调用 task_complete，写入验收 node 并将 task 置为 done。"
    "prerequisites 表达不同 task 之间的成果依赖；同一 task 的环节顺序通过节点记录和 next_bot 交接推进。\n\n"
    "## 内容归属与通用工具用法\n"
    "1. 内容归属：Bot 人格、表达偏好和私有内容维护在其人格配置中，SOUL.md 用于承载这类内容。\n"
    "2. 成员识别：需要确定协作成员且现有信息不足时，调用 group_list_members 查询。"
    "任务交接前确保已取得接手人的有效 profile 和对应角色信息。\n"
    "3. 消息发送：group_send 只填写 text 和 need_reply；项目与会话由当前执行上下文确定。"
    "请求对方确认、回答或行动结果时填写 need_reply=true；确认回执、收口、状态告知等无需回复的消息填写 need_reply=false。"
    "检查 error 和 message_id（话题跳过检查对应结果），确认发送成功。\n"
    "4. 已读工具：group_mark_seen 的 message_ids 填自己收件箱中已处理的消息 ID；检查 error、seen 数量和 blocked 列表。"
    "blocked 中的消息仍待处理；seen=0 时回读核对。读取和正文注入都保留 pending；has_more=false 仅表示快照分页结束。"
    "确认失败时保留待办并按原因处理，已成功的回复避免重复发送。话题由实际发言或合法跳过推进自己的已读水位。\n"
    "5. 需求创建：依据用户明确的创建指令及授权范围调用 req_create，"
    "填写 title、background、scope、approach、acceptance、undecided、source_ref。"
    "没有待定事项时 undecided 精确填写“无”；source_ref 使用 thread_id=<整数>; message_id=<整数>，"
    "指向本项目未撤回的真实用户指令消息，点对点消息使用 thread_id=0。创建后回读核对内容并报告需求 ID。\n"
    "6. 拆解完成：全部任务说明、设计依据、验收条件和前置依赖补齐后，必须调用 task_create，"
    "为同一 requirement_id 显式传 complete=true 提交拆解完成。可在最后一批创建任务时提交；"
    "分批创建及补全后，可传 tasks=[]、complete=true 独立提交。核对返回结果确认拆解完成；"
    "遇到校验失败时按错误补齐后重试。拆解完成表示任务规划已提交，各任务继续按依赖和协作流程执行。\n"
)

# ---- 协作规则 system prompt（byte-stable 常量，纯静态零动态内容——头部一字不稳整段历史缓存全废） ----
# 通用工具契约与项目 AGENTS.md 中的协作流程共同约束执行。
# work/chat 两版按 kind 选一，per-wake 动态内容一律留信号（user message 在尾部，miss 一条无所谓）。
_WORK_SYSTEM_PROMPT = _COMMON_SYSTEM_PROMPT + (
    "## 协作规则（群聊系统 work 模式）\n"
    "1. 消息驱动+排空 inbox：收到系统唤醒信号按入口自判——approved 续工（信号带 task_id，读该任务与批准结果后继续）；"
    "消息已注入（正文已随信号到达，直接处理）；消息需补读（纯信号，调 group_read_inbox 分页补读、读完统一处理）。"
    "处理完主动拉下一条，读完为止——做完手上的事不许跑，接着做下一件；\n"
    "2. @语义：@你/点你名的必须给出实际回应，null 和 pass 都不允许，除非@你的人明确表示不需要再回复；\n"
    "3. 没@你（任务唤醒）的：可自主决定是否发言；\n"
    "4. 要讨论先建话题：点对点 @ 只用于「叫某人干活/递话」，不用于多人讨论；\n"
    "5. 三层授权升级链：L1 自协商（执行中的小问题，点对点 @ 聊完继续干）→ L2 话题讨论"
    "（需多人讨论，建话题拉人→结论落库→收口）→ L3 项目负责人仲裁（话题僵局/方案分歧/技术障碍→@当前项目负责人协调）；"
    "负责人未配置、不可用，或你本人就是负责人且无法解决时，升级给 user "
    "→ L4 用户决策（范围变更/优先级冲突/费用/生产外部动作→task_block(kind='decision') 进入待审队列）；\n"
    "6. 拆解任务必须填写 description、design_basis、acceptance_criteria 和 prerequisites。"
    "prerequisites 填同项目已有前置任务的 ID，无前置填 []。先创建前置任务取得 ID，再创建后续任务；"
    "也可先保存 draft=true 的草稿，补齐说明和依赖后通过 task_update(publish=true) 发布。"
    "依赖依据实际交付关系填写，前置全部 done 后后续任务才可派发；"
    "scanner 兜底唤醒拆解（scan:req）时先读取该需求已有任务（task_list/task_get）、设计依据与来源消息，"
    "保留已落库成果、补齐剩余任务及依赖；无 task.create 授权成员由项目负责人协调配置。\n"
    "7. 结论落库：任何决策结论必须落库（收口消息或 task_log），不能只口头说；"
    "涉及任务前置依赖、任务说明或接手人调整时，由具有本项目 task.update 权限的成员调用 task_update 落库；\n"
    "8. 逐条闭环：每条消息/任务都要有明确结果，不能半途而废；\n"
    "9. 执行任务先调用 task_claim，以返回的 task.id 和完整上下文为准。"
    "执行前核对任务说明、设计依据、验收标准和前置交付记录，必要时调用 task_get 重读。"
    "当前任务执行者通过 task_log 记录交付，填写成果路径、验证依据、结果和问题；交接时同时填写 next_bot 与 expected_role_id。"
    "next_bot 使用选定成员的 profile；expected_role_id 使用按项目流程选定的期望角色的项目角色 ID。"
    "两者依据当前花名册核对，保持期望角色与目标成员的当前 role_id 一致。"
    "expected_role_id 必填，缺失或角色不匹配时引擎拒绝流转；遭到拒绝后重新核对流程与花名册。"
    "任务关闭由项目流程指定且拥有 task.complete 权限的成员调用 task_complete，其他关闭条件以工具校验为准；\n"
    "10. 干活接力：task_log 成功落库后，通过 group_send 点对点通知选定的下游成员；\n"
    "11. 审批分流：技术障碍 task_block(kind='technical') 后 @当前项目负责人；负责人未配置或不可用时通知 user；"
    "你本人就是负责人时先自行协调，仍无法解决则通知 user。"
    "需用户决策时调用 task_block(kind='decision')，随后退出并等待批准。\n"
    "12. 识别讨论转建话题：点对点从「递活/回执」滑成「澄清/分歧/商量方案」时，"
    "任一 bot 停手、建话题拉人，不继续点对点来回；\n"
    "13. 互@回环断链：点对点互@超过 3 轮（来回 6 条）时，引擎会拒绝第 7 条并报错"
    "「消息发送失败，因为已经互@3轮了。如果还需要继续讨论，请创建话题」——"
    "这是引擎真实兜底不是吓唬，撞到它说明该转话题了；\n"
    "14. 需要回复的点对点消息，先在同项目实质回复并 @ 原发送人，核对 group_send 成功后调用 group_mark_seen；"
    "无需回复的消息处理后可直接确认。message_ids 填本次已处理消息的 ID，核对确认结果后再读取下一批；发送失败时保留待办。\n"
    "15. 工具返回权限拒绝（PERMISSION_DENIED）时，立即结束被拒动作，按返回的原因和下一步指引处理，禁止尝试绕过。"
)
_CHAT_SYSTEM_PROMPT = _COMMON_SYSTEM_PROMPT + (
    "## 协作规则（群聊系统 chat 模式）\n"
    "1. 消息驱动：收到话题唤醒时，正文已注入则直接处理；只有纯信号时调用 group_read_inbox，按返回的分页信息补读，读完统一处理。"
    "处理完主动读取下一批，直至本轮可读消息处理完毕；\n"
    "2. 话题仍开放且当前轮到你时，必须调用 group_send 完成本次发言；\n"
    "3. @语义：@你/点你名的必须给出实际回应，null 和 pass 都不允许，除非@你的人明确表示不需要再回复；\n"
    "4. 没@你的：有话说就发言；无新信息时，调用 group_send，text 传 null 表示静默跳过，或传 pass 表示公开跳过。pass 必须精确匹配，不附加其他文字；\n"
    "5. 要讨论先建话题：点对点 @ 只用于「叫某人干活/递话」，不用于多人讨论；\n"
    "6. 三层授权升级链：L1 自协商（执行中的小问题，点对点 @ 聊完继续干）→ L2 话题讨论"
    "（需多人讨论，建话题拉人→结论落库→收口）→ L3 项目负责人仲裁（话题僵局/方案分歧/技术障碍→@当前项目负责人协调）；"
    "负责人未配置、不可用，或你本人就是负责人且无法解决时，升级给 user "
    "→ L4 用户决策（范围变更/优先级冲突/费用/生产外部动作→task_block(kind='decision') 进入待审队列）；\n"
    "7. 拆解任务必须填写 description、design_basis、acceptance_criteria 和 prerequisites。"
        "prerequisites 填同项目已有前置任务的 ID，无前置填 []。先创建前置任务取得 ID，再创建后续任务；"
        "也可先保存 draft=true 的草稿，补齐说明和依赖后通过 task_update(publish=true) 发布。"
        "依赖依据实际交付关系填写，前置全部 done 后后续任务才可派发。\n"
    "8. 结论落库：任何决策结论必须落库，不能只口头说；\n"
    "9. 逐条闭环：每条消息/任务都要有明确结果，不能半途而废；\n"
    "10. 话题内可讨论方案、创建需求和拆解任务：有 task.create 权限的成员可利用话题上下文直接拆解"
    "（task_create）并补全/发布草稿（task_update）；领取/开发/交付任务仍走 work 进程"
    "（task_claim/task_log/task_complete/task_block 在 chat 被拒）。首次派发或上游交接使原任务的 next_bot 指向你后，"
    "在 work 中领取该任务并执行当前环节；\n"
    "11. 收口表示暂停自动讨论：全员跳过/弃权或静默超时后等待用户；发起者可发送 [收口] 主动暂停。用户新消息会恢复讨论，无需用户压轴；"
    "关闭话题是用户单独操作，关闭时才填写收口结论；\n"
    "12. 收口与关闭分开：已收口时停止主动发言，等待用户新消息；已关闭时禁止发言，须由用户重新打开。普通插话进入当前轮，当前发言继续完成。\n"
    "13. 工具返回权限拒绝（PERMISSION_DENIED）时，立即结束被拒动作，按返回的原因和下一步指引处理，禁止尝试绕过。"
)


def system_prompt_for(kind: str) -> str:
    """按 kind 返回协作规则 system prompt（byte-stable 常量，纯静态零动态内容）。

    缓存约束：ephemeral system prompt 拼在 system prompt 尾部=缓存前缀
    头部，一个字节不稳整段历史缓存全废——这里禁止任何动态内容（task id/thread id/时间戳）。
    """
    return (_CHAT_SYSTEM_PROMPT if kind == "chat" else _WORK_SYSTEM_PROMPT)


def wake_system_prompt(kind: str, project: str, workdir: str) -> str:
    """Static collaboration rules followed by current, project-scoped facts."""
    import json
    from .project_workspaces import project as get_project
    from .task_retry import project_owner
    from . import workspace_resources
    from .project_workspaces import LOCK
    conn = db.get_connection()
    try:
        row = get_project(conn, project)
        owner = project_owner(conn, project)
        with LOCK:
            resources = workspace_resources.prompt_config(conn, project, workdir)
    finally:
        conn.close()
    context = {
        '项目稳定 ID': row['name'],
        '项目显示名': row['display_name'] or row['name'],
        '项目描述': row['description'] or '',
        '项目负责人': owner,
        '项目主目录': str(Path(row['root']).resolve()),
        '目录模式': '共用项目主目录' if Path(workdir).resolve() == Path(row['root']).resolve() else '独立 worktree',
        '当前工作目录': workdir,
        '工作区配置': resources,
    }
    return (system_prompt_for(kind) + '\n\n' + workspace_resources.RULE + '\n\n## 当前项目\n'
            + json.dumps(context, ensure_ascii=False, indent=2)
            + '\n群聊和任务工具的 project 参数使用项目稳定 ID。')


def signal_for(kind: str) -> str:
    """退化纯信号（无注入正文）：bot 醒来自己 read_inbox 拉全量。

    need_reply 参数已废弃（req#82 P1）：回复要求不再由全局信号后缀承载，
    改由注入正文逐条携带（每条消息按 effective_need_reply 标「必须回复/
    无需回复/无要求」）。退化模式下 bot 读 inbox 时按逐条 need_reply 自行判断。
    """
    return _CHAT_SIGNAL if kind == "chat" else _WORK_SIGNAL


# ---- req#82 注入主路径：spawn 前快照读取 + 正文结构化拼装 + 32767 预算 ----

_COMMAND_LINE_BUDGET = 32767   # CreateProcessW 整条命令行物理上限（UTF-16 码元）
_COMMAND_LINE_MARGIN = 64      # 安全余量：终止符 + 参数转义抖动

_TASK_SUFFIX = (
    " 先调用 task_get(project=<当前项目>, task_id={task_id}) 完整读取需求正文、任务说明、设计依据、验收标准及前置交付记录，再执行。"
    " 本次处理任务 #{task_id}；已领取则继续该任务，完成后按流程交接。"
    "执行失败应在本次执行输出中说明错误并结束，由引擎重试及汇总通知，"
    "重试期间无需发群报告；必要时可自行 task_block。"
)


def _snapshot_pending(conn, profile, kind, thread_id, project):
    """读 pending 快照（spawn 前一刻）：work 限 project/profile 点对点，chat 限该 thread。

    按稳定 message_id 正序（chat 的 seq 即 message_id，分页游标统一用它）。
    每条消息附当前接收者的有效回复要求（effective_need_reply 逐条算）。
    """
    from .delivery import effective_need_reply
    if kind == "chat":
        rows = conn.execute(
            "SELECT m.id AS message_id, m.ts, m.sender, m.sender_type, m.content, "
            "m.mentions, m.need_reply, m.thread_id "
            "FROM inbox i JOIN messages m ON m.id = i.message_id "
            "WHERE i.profile=? AND i.status='pending' AND m.thread_id=? AND m.project=? "
            "ORDER BY m.id ASC",
            (profile, thread_id, project),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT m.id AS message_id, m.ts, m.sender, m.sender_type, m.content, "
            "m.mentions, m.need_reply, m.thread_id "
            "FROM inbox i JOIN messages m ON m.id = i.message_id "
            "WHERE i.profile=? AND i.status='pending' AND m.thread_id IS NULL AND m.project=? "
            "ORDER BY m.id ASC",
            (profile, project),
        ).fetchall()
    snapshot = []
    for r in rows:
        d = dict(r)
        d["need_reply"] = effective_need_reply(d, profile)
        snapshot.append(d)
    from . import attachments
    return attachments.enrich(conn, snapshot)


def _compose_injection(messages, kind, trigger_msg_id=None):
    """结构化拼装注入正文：正文+id+发送者+need_reply+seq，分别序列化。

    控制指令、身份元数据与原始正文分开，避免正文中的伪造标记混入引擎控制信息。
    每条消息带当前接收者的有效回复要求（必须回复/无需回复/无要求），
    trigger 标注触发消息。禁止静默截断：正文原样塞入，超预算由调用方整段退化。
    """
    scope = "话题讨论" if kind == "chat" else "点对点派活"
    lines = [
        f"[系统唤醒信号] 你有 {len(messages)} 条未读消息（{scope}），"
        "已全量注入下方正文，直接处理，无需再调 group_read_inbox()。",
    ]
    for m in messages:
        need = m["need_reply"]
        reply = "必须回复" if need == 1 else ("无需回复" if need == 0 else "无要求")
        trigger = "，本条为触发消息" if m["message_id"] == trigger_msg_id else ""
        lines.append("")
        lines.append(
            f"【消息 id={m['message_id']}】发送者={m['sender']}（{m['sender_type']}）"
            f" 回复要求={reply}{trigger}"
        )
        lines.append(f"正文：{m['content']}")
        if m.get('attachments'):
            import json
            lines.append('附件（按 id 调用 group_read_image）：' + json.dumps(m['attachments'], ensure_ascii=False))
    return "\n".join(lines)


def _command_line_utf16_len(cmd):
    """最终 argv 经 Windows 参数转义后的完整命令行 UTF-16 码元数。"""
    line = subprocess.list2cmdline(cmd) if sys.platform == "win32" else " ".join(cmd)
    return len(line.encode("utf-16-le")) // 2


def _fits_command_budget(cmd):
    """注入正文 + 固定参数是否落在命令行预算内（留安全余量）。"""
    return _command_line_utf16_len(cmd) <= _COMMAND_LINE_BUDGET - _COMMAND_LINE_MARGIN


def _requirement_signal(conn, profile, kind, project):
    """Refresh actionable requirements even when work queue triggers coalesce."""
    if kind != "work":
        return ""
    from . import requirement_recovery
    rows, coordinate = requirement_recovery.context(conn, profile, project)
    if not rows:
        return ""
    ids = ", ".join(f"#{r['id']}" for r in rows[:20])
    more = "（另有待拆需求，以实时查询为准）" if len(rows) > 20 else ""
    import json
    project_arg = json.dumps(project, ensure_ascii=False)
    if coordinate:
        return (
            f"\n[系统唤醒信号] 需求恢复配置协调：当前项目 {project_arg} 有待恢复需求 {ids}{more}。"
            "当前缺少有效的 task.create 授权成员；请联系管理员在项目设置中补齐成员及授权。"
            "按项目负责人职责协调配置，授权成员就绪后由扫描器安排拆解恢复。"
        )
    return (
        f"\n[系统唤醒信号] 需求拆解：当前项目 {project_arg} 有待拆需求 {ids}{more}。"
        f"调用 req_list(project={project_arg}, status='open') 读取当前需求，"
        "先调用 task_list/task_get 读取并复用已有任务、设计依据与来源消息，"
        "为尚无任务的需求按项目流程创建任务；已有部分任务时恢复拆解，补齐说明与依赖。"
        "按实时拆解凭据契约续写，最后显式提交 complete=true 完成拆解，避免重复创建任务。"
        "已绑定任务先按本次任务指令处理，再检查待拆需求。"
        "即使 inbox 为空也须检查需求；以实时需求和任务状态为准。"
    )


def _finalize_injection_signal(profile, kind, thread_id, project, trigger_msg_id,
                                bound_task, cmd_without_q):
    """spawn 前一刻读快照拼装注入正文；超预算整段退化纯信号。

    注入 = 未读全量一次性塞进 -q（正文+id+发送者+need_reply+seq）。禁止静默截断、
    禁止「部分注入+补拉剩余」：正文总长超命令行预算时整段退化成纯信号，
    bot 自己 read_inbox 分页拉全量（I2）。退化信号带 upper_id（本次快照水位），
    bot 按 after_id 游标分页读固定上界内仍有效的 pending。返回最终信号文本。
    """
    conn = db.get_connection()
    try:
        snapshot = _snapshot_pending(conn, profile, kind, thread_id, project)
        requirement_signal = _requirement_signal(conn, profile, kind, project)
    finally:
        conn.close()
    # upper_id = 本次快照水位（快照范围内的 max message_id，I1）。快照之后到达的
    # 消息 id > upper_id，不纳入本次补读范围，进入后续 wake/运行中投递（I1）。
    upper_id = max((m["message_id"] for m in snapshot), default=0)
    if snapshot:
        body = _compose_injection(snapshot, kind, trigger_msg_id=trigger_msg_id)
        if bound_task is not None:
            body += _TASK_SUFFIX.format(task_id=bound_task)
        body += requirement_signal
        if _fits_command_budget(cmd_without_q + ["-q", body]):
            return body
    # 退化：纯信号 + upper_id 分页游标（I2/R1）。chat 单 thread 全量现场无需 upper_id。
    if kind == "work" and not snapshot and (bound_task is not None or requirement_signal):
        signal = "[系统唤醒信号] 本次为任务或需求调度，当前点对点收件箱为空。"
    else:
        signal = signal_for(kind)
    if kind == "work" and upper_id:
        signal += f" 本次快照消息上界 upper_id={upper_id}，"
        signal += "调用 group_read_inbox(after_id=<上一页最后一条 id>, upper_id=%d) 分页补读，读完统一处理。" % upper_id
    if bound_task is not None:
        signal += _TASK_SUFFIX.format(task_id=bound_task)
    signal += requirement_signal
    return signal


def _native_hermes_entry(entry: str) -> str:
    """Keep the transport entry point shared with installation/source checks."""
    from . import hermes_runtime
    return hermes_runtime.native_entry(entry)


def _hermes_bin() -> str:
    from . import hermes_runtime
    return hermes_runtime.executable()


def _codex_bin() -> str:
    """codex CLI 定位（req#88 task#152：spawn 分叉的 codex 侧入口）。

    CODEX_BIN > PATH > Windows Codex 桌面应用安装的原生 CLI。
    后台引擎由 VBS 启动时，PATH 经常未包含桌面应用导出的 bin 目录。
    """
    env = os.environ.get("CODEX_BIN", "").strip()
    if env:
        return env
    found = shutil.which("codex")
    if found:
        return found
    if sys.platform == 'win32':
        local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
        candidates = list((local / 'OpenAI' / 'Codex' / 'bin').glob('*/codex.exe'))
        candidates = [p for p in candidates if p.is_file()]
        if candidates:
            return str(max(candidates, key=lambda p: (p.stat().st_mtime_ns, str(p))))
    raise FileNotFoundError('未找到 Codex CLI：请安装 Codex，或将 CODEX_BIN 指向原生可执行文件')


def _mcp_server_config(profile: str, *, context=None, chat=False) -> dict:
    """Connection and execution metadata shared by Codex and Hermes MCP clients."""
    import json as _json
    from .auth import bot_token, execution_token
    server = {
        "command": _MCP_PYTHON,
        "args": [str(Path(__file__).resolve().parent.parent / "mcp" / "server.py")],
        "env": {
            "TASK_QUEUE_ROLE": profile,
            "HERMES_PROFILE": profile,
            "GROUP_ENGINE_URL": _engine_url(),
            "GROUP_ENGINE_TOKEN": execution_token(profile, context) if context is not None else bot_token(profile),
            "HERMES_CHAT_MODE": '1' if chat else '0',
        },
    }
    if context is not None:
        from .message_execution import CONTEXT_ENV
        server['env'][CONTEXT_ENV] = _json.dumps(context)
    return server


def _codex_mcp_config(profile: str, project: str, *, context=None, chat=False) -> list[str]:
    """Pass this execution's MCP connection through Codex command-line configuration."""
    import json as _json
    server = _mcp_server_config(profile, context=context, chat=chat)
    server.update({
        "required": True,
        "startup_timeout_sec": 60,
        "tools": {name: {"approval_mode": "approve"} for name in _MCP_TOOLS},
        "enabled_tools": list(_MCP_TOOLS),
    })

    def _toml(value):
        if isinstance(value, dict):
            return "{" + ",".join(_json.dumps(k) + "=" + _toml(v) for k, v in value.items()) + "}"
        return _json.dumps(value)

    return ["-c", f"mcp_servers.task_queue={_toml(server)}"]


# codex 内联 MCP 的 Python 解释器：优先 GROUP_MCP_PYTHON 覆盖，否则用当前解释器（引擎
# venv 已随 requirements.lock 装 mcp；pythonw 换 python 保证 stdio MCP 可读写）。
def _default_mcp_python() -> str:
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"
    return exe


_MCP_PYTHON = os.environ.get("GROUP_MCP_PYTHON", "").strip() or _default_mcp_python()

# mcp/server.py 全部工具名（chat 条件注册的执行类工具也在内——enabled_tools 是
# codex 侧白名单，物理注册仍由 HERMES_CHAT_MODE 把关）。
_MCP_TOOLS = (
    "project_add", "project_remove", "project_archive", "project_list",
    "req_create", "req_update", "req_list", "req_status", "req_complete", "req_cancel",
    "req_resume", "task_resume",
    "task_create", "task_update", "task_get", "task_list", "task_cancel",
    "task_claim", "task_log", "task_complete", "task_block",
    "group_send", "group_read_image", "group_read_inbox", "group_mark_seen",
    "group_get_thread", "group_create_thread", "group_task_update",
    "group_list_threads", "group_list_members",
)


def _engine_url() -> str:
    """群聊工具转发的引擎 API 入口（req#122 走统一配置层，与 mcp/server.py 同源）。"""
    from . import config
    return config.engine_url().rstrip("/")


def _bot_runtime(conn: sqlite3.Connection, project: str, profile: str) -> str:
    """按 Bot 身份解析 runtime（req#88 task#152：spawn 分叉判据）。

    定位链（设计稿第三节：分叉按 Bot 的 runtime 类型）：
    1. project_members.bot_id → bots.runtime（Bot 身份层正路）；
    2. 兼容期 bot_id 为空（#147 迁移窗口）：按 profile 反推 bots 行——命中唯一行
       用其 runtime，命中多行（同名 profile 跨宿主二义）保守回退 hermes（当前
       全部存量成员是 hermes，不误伤；codex Bot 成员由 #150/#151 注册接口产生，
       注册即写 bot_id，走第 1 层）；
    3. 完全无 bots 行：默认 hermes（存量行为不变）。
    """
    from . import bot_identity
    row = bot_identity.resolve(conn, project, profile, active_only=True)
    if row is not None:
        return row["runtime"] or "hermes"
    rows = conn.execute(
        "SELECT runtime, COUNT(*) AS n FROM bots WHERE profile=? GROUP BY runtime",
        (profile,),
    ).fetchall()
    if len(rows) == 1 and rows[0]['n'] == 1:
        return rows[0]["runtime"] or "hermes"
    if rows:
        raise ValueError('ambiguous Bot runtime')
    return "hermes"


def _project_root(project: str) -> str:
    """查 projects 表 root（主目录，agents.md 所在）。文件存在校验在调用方。"""
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT root FROM projects WHERE name=?", (project,)).fetchone()
        return row["root"] if row else ""
    finally:
        conn.close()


def _workdir_for(profile: str, project: str) -> str:
    """按项目目录模式解析并校验唤醒 cwd。

    共用模式和 default 使用项目主目录；独立模式下其他成员使用已登记的
    worktree，兼容旧 worktree_root/profile 映射。目录无效时阻止唤醒。
    """
    conn = db.get_connection()
    try:
        from .project_workspaces import launch_directory
        return launch_directory(conn, project, profile)
    finally:
        conn.close()


# （原 _agents_md_path 已删：方案B终案后 agents.md 走 --in 原生加载，引擎不再需要路径）


def enqueue(
    conn: sqlite3.Connection,
    profile: str,
    kind: str,
    trigger: str,
    trigger_msg_id: int | None = None,
    thread_id: int | None = None,
    project: str = "xjk",
    *, commit: bool = True,
) -> bool:
    """入队一个唤醒 job。同 profile 同 kind 已在 queued/running 则不重复入队。

    返回 True 表示新入队，False 表示已存在（去重命中）或跳过（user 不唤醒）。
    commit=False 时保留调用方事务，入队与相关状态变更统一提交。
    """
    # 内置 user 代表用户，排除在 Bot 唤醒范围之外。
    if db.is_builtin_user(profile):
        return False
    from . import project_readiness
    if not project_readiness.is_ready(conn, project):
        return False
    from .router import _profile_active
    if not _profile_active(conn, project, profile):
        return False
    try:
        cur = conn.execute(
            "INSERT INTO wake_jobs (profile, project, kind, trigger, trigger_msg_id, thread_id, status, enqueued_at) "
            "SELECT ?, ?, ?, ?, ?, ?, 'queued', ? WHERE EXISTS "
            "(SELECT 1 FROM projects WHERE name=? AND archived=0 AND readiness_status='ready') AND EXISTS "
            "(SELECT 1 FROM project_members pm JOIN members m ON m.profile=pm.profile "
            "WHERE pm.project=? AND pm.profile=? AND pm.active=1)",
            (profile, project, kind, trigger, trigger_msg_id, thread_id, db.now_iso(), project, project, profile),
        )
        if commit:
            conn.commit()
        return cur.rowcount == 1
    except sqlite3.IntegrityError:
        # partial unique index 命中：同 profile 同 kind 已在队列
        return False


def session_name(profile: str, kind: str, thread_id: int | None = None,
                  project: str = "xjk") -> str:
    """派生逻辑会话名，供子进程 -c 参数使用。

    点对点(work)：{profile}-{project}-work（随项目走，同项目派活连续）
    话题(chat)：{profile}-{project}-chat-{thread_id}（每话题独立，收口即弃）
    """
    if kind == "chat" and thread_id is not None:
        return f"{profile}-{project}-chat-{thread_id}"
    return f"{profile}-{project}-work"


def deliver(profile: str, kind: str, thread_id: int | None = None,
            project: str = "xjk", trigger_msg_id: int | None = None, job_id=None, task_id=None) -> dict:
    """通过 subprocess 执行一次唤醒。

    chat 唤醒防误开工：引擎侧 chat_context_until 运行时校验（queue.py 投递成功
    写 600s 窗口，MCP 执行类工具经 /wake/check 查询）——唯一防线，勿删。
    会话名按方案 B 三层派生（session_name()），断点续=重触发复用同一会话名。

    req#82 注入主路径：signal 传 None，由 _spawn_in_workspace 在 spawn 前一刻
    读 pending 快照拼装注入正文（正文+id+发送者+need_reply+seq），超预算整段
    退化纯信号。need_reply 参数废弃——回复要求逐条进注入正文。trigger_msg_id
    只做注入正文里的触发消息标注。
    返回 {ok, channel, detail}。
    """
    return _spawn(profile, kind, None, thread_id, project,
                  job_id=job_id, task_id=task_id, trigger_msg_id=trigger_msg_id)


def deliver_approved(profile: str, task_id: int, project: str = "xjk") -> dict:
    """用户批准后重触发：approved 信号 + 同一 work 会话名（断点续）。

    信号标明审批结果，执行者读取对应的 blocked 任务后继续执行。
    req#82 P2：approved 独立入口，信号带 task_id 读该任务与批准结果，
    不走通用注入快照（避免把积压 pending 带进续工干扰）。
    """
    signal = _APPROVED_SIGNAL.format(task_id=task_id)
    return _spawn(profile, "work", signal, None, project)


def _spawn(profile: str, kind: str, signal: str | None, thread_id: int | None,
           project: str, job_id=None, task_id=None, trigger_msg_id: int | None = None) -> dict:
    from .project_workspaces import LOCK
    from .session_adapter import _SWITCH_LOCK
    with LOCK, _SWITCH_LOCK:
        if _shutting_down.is_set():
            return {'ok': False, 'busy': True, 'detail': 'engine is shutting down'}
        try:
            if job_id is not None:
                conn = db.get_connection()
                try:
                    job = conn.execute('SELECT status FROM wake_jobs WHERE id=?', (job_id,)).fetchone()
                    if job is None or job['status'] != 'running':
                        return {'ok': False, 'detail': 'wake job is no longer running'}
                finally:
                    conn.close()
            result = _spawn_in_workspace(profile, kind, signal, thread_id, project,
                                         job_id, task_id, trigger_msg_id)
            if not result.get('ok') and not result.get('busy') and not result.get('failure_handled'):
                import uuid
                _report_execution_error(profile, project, thread_id, session_name(profile, kind, thread_id, project),
                                        f'queue:{job_id}' if job_id is not None else uuid.uuid4().hex,
                                        result.get('detail', ''), job_id=job_id, parent_id=trigger_msg_id)
                from .execution_errors import MESSAGES, classify
                result['detail'] = MESSAGES[classify(result.get('detail', ''))]
            return result
        except Exception as exc:
            import uuid
            detail = f'{type(exc).__name__}: {getattr(exc, "detail", exc)}'
            _report_execution_error(profile, project, thread_id, session_name(profile, kind, thread_id, project),
                                    f'queue:{job_id}' if job_id is not None else uuid.uuid4().hex,
                                    detail, job_id=job_id, parent_id=trigger_msg_id)
            from .execution_errors import MESSAGES, classify
            return {'ok': False, 'detail': MESSAGES[classify(detail)]}


def _spawn_in_workspace(profile: str, kind: str, signal: str | None, thread_id: int | None,
                        project: str, job_id=None, task_id=None, trigger_msg_id: int | None = None) -> dict:
    """subprocess 唤醒的公共实现。

    项目规则与协作提示词的加载方式：
    - `--in <项目root>`：cwd 指项目目录，Hermes 原生加载项目 agents.md 进 system prompt
      （byte-stable、缓存命中），替代「信号带路径 + bot 每次 read_file 堆历史」；
    - HERMES_EPHEMERAL_SYSTEM_PROMPT：协作规则 env 注入（subprocess 通道每次 spawn
      重新注入，无 gateway safe_env 过滤）。静态协作规则后追加当前项目配置，格式和字段顺序固定。
    """
    session = session_name(profile, kind, thread_id, project)
    # --in 按 profile 派生：default 落主目录，其他 bot 落各自 worktree（不踩主目录红线）
    from . import task_retry
    conn = db.get_connection()
    try:
        from .router import _profile_active
        from . import project_readiness
        if project_readiness.check(conn, project):
            return {'ok': False, 'busy': True, 'detail': '项目文件尚未就绪，请恢复初始化'}
        if not conn.execute('SELECT 1 FROM projects WHERE name=? AND archived=0', (project,)).fetchone():
            return {'ok': False, 'detail': '项目已归档或不存在'}
        if not _profile_active(conn, project, profile):
            return {'ok': False, 'detail': '成员尚未加入项目或已停用'}
        bound_task = task_retry.select_task(conn, profile, project, task_id) if kind == 'work' else None
    finally:
        conn.close()
    if task_id is not None and bound_task is None:
        return {'ok': True, 'detail': 'retry task already handed off or terminal'}
    if kind == 'chat' and thread_id is not None:
        conn = db.get_connection()
        try:
            if topic_busy(conn, thread_id, project):
                return {'ok': False, 'busy': True, 'detail': 'topic execution still running'}
            turn = conn.execute('SELECT status,turn_profile,project FROM threads WHERE id=?', (thread_id,)).fetchone()
            if not turn or (turn['status'], turn['turn_profile'], turn['project']) != ('open', profile, project):
                return {'ok': False, 'detail': 'topic turn changed before spawn'}
        finally:
            conn.close()
    workdir = _workdir_for(profile, project)
    from . import project_rules
    project_rules.sync(project, workdir)
    # req#83：查/建会话绑定，拿真实 Hermes session_id。spawn 传真实 session_id（断点续，
    # U1：-c 精确 ID 匹配优先 + 自动 compression tip 跟随），去掉 --create-if-missing
    # （首建由引擎显式 create_session 完成，恢复与首建分开——绑定失效明确失败，不静默重建）。
    # 引擎根环境（HERMES_HOME 含 hermes-agent/）才走绑定；bot/测试进程的 HERMES_HOME 是
    # profile 目录，会话绑定不可用，降级旧行为（-c <title> --create-if-missing）。
    from . import session_adapter
    # req#88 task#152：按 Bot runtime 分叉（设计稿第三节——分叉点唯一=拼命令这段）。
    # codex：跳过 Hermes 会话绑定（codex 无「会话创建」动作，绑定是执行后回填，
    # 第四节第 5 条）；resume thread_id 从 session_bindings 读，无绑定行=首建。
    conn = db.get_connection()
    try:
        runtime = _bot_runtime(conn, project, profile)
        from . import bot_identity
        bot = bot_identity.resolve(conn, project, profile)
        from . import project_models
        model_args = project_models.arguments(conn, project, profile)
        codex_thread_id = None
        codex_spawn_gen = None
        codex_bot_id = None
        if runtime == "codex":
            # req#88 task#153：读绑定改 runtime 列逐行校验（第四节第 1/3 条）——
            # 只认 runtime='codex' 的行，防 hermes 行残留 session_id 被误当 codex
            # thread_id 去 resume。同时取 generation（spawn 时刻快照），供回填时
            # 旧运行迟到回写守卫（codex_save_thread 条件 UPDATE）。
            codex_bot_id = session_adapter._codex_bot_id(conn, project, profile)
            if codex_bot_id is not None:
                row = conn.execute(
                    "SELECT session_id, generation FROM session_bindings "
                    "WHERE project=? AND bot_id=? AND kind=? AND thread_id=? AND runtime='codex'",
                    (project, codex_bot_id, kind, thread_id or 0),
                ).fetchone()
                if row is not None:
                    codex_thread_id = row["session_id"] or None
                    codex_spawn_gen = row["generation"]
    finally:
        conn.close()
    use_binding = runtime != "codex" and session_adapter.available()
    if use_binding:
        conn = db.get_connection()
        try:
            try:
                sid = session_adapter.resolve_or_create(conn, project, profile, kind, thread_id, session)
            except session_adapter.SessionBindingInvalid as exc:
                return {'ok': False, 'detail': str(exc)}
        finally:
            conn.close()
    else:
        sid = session
    # req#82 注入主路径：signal=None 时，spawn 前一刻读 pending 快照拼装注入正文
    # （正文+id+发送者+need_reply+seq），超预算整段退化纯信号（I2）。固定参数先
    # 单独成串用于精确预算；approved/退化等非注入信号（signal 非 None）直接走原路径。
    if runtime == "codex":
        # codex exec 命令（spike SPIKE-CODEX-SUBPROCESS-MCP-2026-09-29 实证参数）：
        # 使用所选 CODEX_HOME 与命名配置；--json 出 JSONL 事件流。
        # MCP 内联配置显式包含准确执行上下文。
        # --skip-git-repo-check 引擎 cwd 是项目目录而非 codex 仓库。
        # 首建无 resume，续 resume <thread_id>。
        wake_started_at = time.time()
        context = {'profile': profile, 'project': project, 'thread_id': thread_id,
                   'session': session, 'wake_started_at': wake_started_at}
        codex_home, config_profile = bot_identity.codex_settings(bot)
        from . import codex_auth
        try:
            codex_auth.sync(codex_home)
        except codex_auth.AuthSyncError as exc:
            return {'ok': False, 'detail': str(exc)}
        cmd_without_q = [
            _codex_bin(), "exec", "--json",
            "--skip-git-repo-check",
        ] + _codex_mcp_config(profile, project, context=context, chat=kind == 'chat')
        if config_profile:
            cmd_without_q += ['--profile', config_profile]
        cmd_without_q += model_args
        # Keep stable collaboration/project context in the developer layer.
        import json
        cmd_without_q += ['-c', 'developer_instructions=' + json.dumps(
            wake_system_prompt(kind, project, workdir), ensure_ascii=False)]
        if codex_thread_id:
            cmd_without_q += ["resume", codex_thread_id]
        # wake_runs.runtime_session_id：codex 行 spawn 前存 NULL（#148 契约），
        # 首建 thread_id 执行后才可知，由 #153 的 thread.started 回填写入。
        sid_for_run = None if not codex_thread_id else codex_thread_id
    else:
        cmd_without_q = [
            _hermes_bin(), "-p", bot['profile'] if bot else profile, "chat",
            "--in", workdir, "-c", sid, "-Q",
        ] + model_args
        if not use_binding:
            cmd_without_q.insert(cmd_without_q.index("-Q"), "--create-if-missing")
        sid_for_run = sid
    if signal is None:
        signal = _finalize_injection_signal(profile, kind, thread_id, project,
                                            trigger_msg_id, bound_task, cmd_without_q)
    elif bound_task is not None:
        signal += _TASK_SUFFIX.format(task_id=bound_task)
    # codex 的 prompt 是末位裸参数（无 -q 包装）；hermes 仍是 -q。
    cmd = cmd_without_q + (["-q", signal] if runtime != "codex" else [signal])
    env = os.environ.copy()
    # Database configuration belongs to the engine process.
    for key in ("GROUP_DB", "GROUP_DATA_DIR", "DATA_DIR", "GROUP_ATTACHMENTS_DIR"):
        env.pop(key, None)
    mcp_overlay = None
    # Publish the effective address to subprocesses as well as explicit MCP settings.
    # Hermes preserves XDG_* variables in its filtered MCP subprocess environment.
    env["XDG_GROUP_ENGINE_URL"] = _engine_url()
    from .auth import execution_token
    for key in ("GROUP_ADMIN_PASSWORD", "GROUP_SECRET", "GROUP_ENGINE_KEY", "GROUP_ENGINE_TOKEN"):
        env.pop(key, None)
    if runtime == "codex":
        env['CODEX_HOME'] = codex_home
        # chat 话题内禁任务工具的唯一限制（设计稿第六节）：同 hermes 通道，靠
        # HERMES_CHAT_MODE 物理注册层把关，XDG 上下文给 MCP 精确 thread_id。
        # 协作规则是 Hermes gateway 机制，codex 无此通道——继承自父环境的
        # HERMES_EPHEMERAL_SYSTEM_PROMPT 对 codex 无意义且误导，主动清掉。
        env.pop("HERMES_EPHEMERAL_SYSTEM_PROMPT", None)
        env['HERMES_CHAT_MODE'] = '1' if kind == 'chat' else '0'
    else:
        if bot:
            env['HERMES_HOME'] = bot['config_root']
        env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = wake_system_prompt(kind, project, workdir)
    # HERMES_WAKE_THREAD_ID 注入已删：gateway _build_safe_env 过滤自定义变量，
    # MCP server 恒读不到（与 HERMES_CHAT_MODE 同坑，hermes 2026-09-08 查证）。
    # 思考归属由引擎侧 wake_jobs.chat_context_until 窗口关联（heartbeat.py）。
    try:
        if runtime != 'codex':
            wake_started_at = time.time()
        # Hermes preserves XDG_* variables for its MCP subprocesses. This is
        # execution metadata, not a credential; /msg validates it against DB.
        import json
        from .message_execution import CONTEXT_ENV
        env[CONTEXT_ENV] = json.dumps({
            "profile": profile, "project": project, "thread_id": thread_id,
            "session": session, "wake_started_at": wake_started_at,
        })
        env["XDG_GROUP_ENGINE_TOKEN"] = execution_token(profile, json.loads(env[CONTEXT_ENV]))
        if runtime != "codex":
            from .mcp_runtime import cleanup_overlay, hermes_overlay
            hermes_server = _mcp_server_config(profile, context=json.loads(env[CONTEXT_ENV]), chat=kind == "chat")
            hermes_server["tools"] = {"include": list(_MCP_TOOLS)}
            mcp_overlay = hermes_overlay(hermes_server, env,
                profile_home=bot_identity.hermes_home(profile, Path(env.get("HERMES_HOME") or db._hermes_root())))
        conn = db.get_connection()
        try:
            event_floor = conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        finally:
            conn.close()
        with _session_lock:
            if session_busy(profile, kind, thread_id, project):
                if mcp_overlay is not None:
                    cleanup_overlay(mcp_overlay)
                return {"ok": False, "busy": True, "detail": "session still running"}
            _running_sessions.add(session)
            _intentional_stops.discard(session)
        import tempfile
        output = tempfile.TemporaryFile()
        # Publish the execution record before a child can send its first message.
        conn = db.get_connection()
        try:
            conn.execute("INSERT INTO wake_runs(session,started_at,project,profile,thread_id,runtime_session_id,bot_id,runtime) VALUES(?,?,?,?,?,?,?,?)",
                         (session, wake_started_at, project, profile, thread_id, sid_for_run,
                          bot['id'] if bot else None, runtime))
            from . import browser_resources
            browser_resources.register(conn, env, project=project, profile=profile, session=session,
                                       started_at=wake_started_at, thread_id=thread_id, task_id=bound_task)
            conn.commit()
        finally:
            conn.close()
        # req#88 task#153：codex 通道用 stdout=PIPE 流式读 JSONL，及时捕获
        # thread.started 的 thread_id 回填绑定（第四节第 4 条「收到即保存」）。
        # hermes 通道保持 TemporaryFile（一次性读 tail，无需流式）。
        popen_kwargs = dict(
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=workdir,
            env=env,
            creationflags=_NO_WINDOW,
        )
        codex_reader = None
        codex_tail = None  # codex 通道滚动尾部文本（供退出诊断），hermes 用 TemporaryFile
        if runtime == "codex":
            popen_kwargs["stdout"] = subprocess.PIPE
            popen_kwargs["text"] = True
            popen_kwargs["encoding"] = "utf-8"
            popen_kwargs["errors"] = "replace"
        proc = subprocess.Popen(cmd, **popen_kwargs)
        if runtime == 'codex':
            with _session_lock:
                _native_procs[session] = (proc, wake_started_at)
        import threading as _th
        # req#88 task#153：codex 流式 reader——逐行 parse JSONL，捕获 thread.started
        # 的 thread_id 及时回填绑定与 wake_runs.runtime_session_id。回填走
        # codex_save_thread 的 generation 条件守卫，旧运行迟到回写被拒（第四节第 6 条）。
        if runtime == "codex":
            codex_tail = []  # reader 与 watch 共享：滚动保留尾部文本用于退出诊断
            def _codex_reader():
                import json as _json
                auth_checked = False
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    codex_tail.append(line)
                    if len(codex_tail) > 60:
                        del codex_tail[:-60]
                    try:
                        ev = _json.loads(line)
                    except ValueError:
                        continue
                    from . import codex_reasoning
                    try:
                        codex_reasoning.record(session, wake_started_at, ev)
                    except Exception:
                        logging.getLogger(__name__).exception('Cannot persist Codex execution event for %s', session)
                    if not isinstance(ev, dict):
                        continue
                    if not auth_checked and codex_auth.is_auth_error(ev):
                        auth_checked = True
                        try:
                            if codex_auth.sync(codex_home, unauthorized=True):
                                logging.getLogger(__name__).info(
                                    'Updated Codex file credentials after authentication failure for %s', profile)
                        except codex_auth.AuthSyncError as exc:
                            logging.getLogger(__name__).warning('Codex authentication sync: %s', exc)
                    if ev.get("type") == "thread.started":
                        tid_val = ev.get("thread_id")
                        if tid_val:
                            c2 = db.get_connection()
                            try:
                                saved = session_adapter.codex_save_thread(
                                    c2, project, profile, kind, thread_id, tid_val,
                                    bot_id=codex_bot_id, spawn_gen=codex_spawn_gen)
                                if saved:
                                    c2.execute(
                                        "UPDATE wake_runs SET runtime_session_id=? "
                                        "WHERE session=? AND started_at=?",
                                        (tid_val, session, wake_started_at))
                                    c2.commit()
                            finally:
                                c2.close()
            codex_reader = _th.Thread(target=_codex_reader, daemon=True, name=f"codex-{profile}")
            codex_reader.start()
        # 进程退出事件：后台线程 wait()
        # 子进程，退出即清心跳+唤醒归属——心跳从「90s 超时推断」升级为「真退出事件」。
        # 90s TTL 保留为兜底（wait 线程意外死亡/进程被强杀时仍能超时清）。
        import threading as _th
        from . import heartbeat as _hb

        def _watch_session():
            # req#39：改用 proc.wait() 判退出——实测 chat 模式下壳进程（hermes.exe
            # uv trampoline）会 wait 它的真 python 子进程，壳 poll 归零 = 整条进程链
            # 退完，无需再每 0.5s wmic 全表扫命令行模糊匹配（那个每 tick 卡顿的根源）。
            # 旧 find_bot_processes(profile, session=...) 循环退役。
            error = ''
            try:
                if bound_task is not None:
                    proc.wait(timeout=3600)
                else:
                    proc.wait()
            except subprocess.TimeoutExpired:
                from .stop import _kill_process_tree
                if not _kill_process_tree(proc.pid):
                    _report_execution_error(profile, project, thread_id, session, f'{session}:{wake_started_at}',
                                            'execution timeout; process tree termination failed', job_id=job_id,
                                            parent_id=trigger_msg_id, env=env)
                    events.bus.publish('execution.failed', {'profile': profile, 'project': project,
                        'task_id': bound_task, 'error': 'execution timeout; process tree termination failed; manual stop required'})
                    output.close()
                    return
                proc.wait()
                error = 'execution timed out after 3600 seconds'
            if mcp_overlay is not None:
                cleanup_overlay(mcp_overlay)
            output.seek(0, 2)
            length = output.tell()
            output.seek(max(0, length - 2048))
            tail = output.read().decode('utf-8', errors='replace').strip()
            output.close()
            if runtime == "codex":
                # codex 的 stdout 走 PIPE（reader 消费），退出诊断用滚动尾部文本
                if codex_reader is not None:
                    codex_reader.join(timeout=5)
                tail = "\n".join(codex_tail or []) if codex_tail else tail
            if proc.returncode or bound_task is not None:
                error = (error or f'process exit={proc.returncode}; task not handed off/completed') + (f'\n{tail}' if tail else '')
            ended_at = time.time()
            if proc.returncode or error.startswith('execution timed out'):
                _report_execution_error(profile, project, thread_id, session, f'{session}:{wake_started_at}',
                                        error, exit_code=proc.returncode, job_id=job_id,
                                        parent_id=trigger_msg_id or parent_id, env=env)
            try:
                _handle_task_exit(profile, project, kind, session, wake_started_at, ended_at,
                                  proc.returncode, event_floor, task_id=bound_task, job_id=job_id, error=error)
            except Exception:
                logging.getLogger(__name__).exception("Task exit handling failed for %s", session)
            conn = db.get_connection()
            try:
                conn.execute("UPDATE wake_runs SET ended_at=?,exit_code=? WHERE session=? AND started_at=?",
                             (ended_at, proc.returncode, session, wake_started_at))
                conn.commit()
                from . import steer_ledger
                steer_ledger.reconcile_targets(conn)
            finally:
                conn.close()
                try:
                    browser_resources.finish_run(session, wake_started_at)
                except Exception:
                    logging.getLogger(__name__).exception('Browser cleanup failed for %s', session)
                with _session_lock:
                    _running_sessions.discard(session)
                    _running_wakes.pop(session, None)
                    _native_procs.pop(session, None)
                    _intentional_stops.discard(session)
            events.bus.publish("wake.finished", {"session": session, "wake_started_at": wake_started_at,
                                                  "ended_at": ended_at})
            # 精确清理已退出的进程，避免清掉立即重试或其他会话的新心跳。
            _hb.on_exit(profile, pid=proc.pid, thread_id=thread_id)
        # SSE「已唤醒」事件在 spawn 后推送，表示进程已经启动；
        # 「思考中」由心跳上报，与进程启动状态分别展示。
        # session 字段：前端思考气泡用（session_name() 派生，精确锁定会话读 reasoning）
        from . import events
        # 树模型：气泡按 parent_id 挂到所回应的消息后，
        # spawn 时从 threads 查当前 turn 的 parent_id/round，随事件带给前端
        conn = db.get_connection()
        try:
            conn.execute("UPDATE wake_runs SET pid=? WHERE session=? AND started_at=?",
                         (proc.pid, session, wake_started_at))
            conn.commit()
        finally:
            conn.close()
        parent_id = None
        round_no = None
        if kind == "chat" and thread_id is not None:
            try:
                conn = db.get_connection()
                try:
                    row = conn.execute(
                        "SELECT turn_parent_id, round FROM threads WHERE id=?", (thread_id,)
                    ).fetchone()
                    if row is not None:
                        parent_id = row["turn_parent_id"]
                        round_no = row["round"] if "round" in row.keys() else None
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass  # 查不到就退化为时间锚（旧行为）
        run = {
            "profile": profile, "kind": kind,
            "thread_id": thread_id, "project": project,
            "session": session,
            "wake_started_at": wake_started_at,
            "spawned_at": db.now_iso(),  # 思考气泡在消息时间流中的插入锚点
            "parent_id": parent_id,      # 树模型：气泡挂到所回应的消息节点后
            "round": round_no,
        }
        with _session_lock:
            _running_wakes[session] = run
        events.bus.publish("wake.spawned", run)
        _th.Thread(target=_watch_session, daemon=True, name=f"session-{profile}").start()
        return {"ok": True, "channel": "subprocess", "detail": f"pid {proc.pid}"}
    except Exception as exc:  # noqa: BLE001
        if 'proc' in locals() and proc.poll() is None:
            from .stop import _kill_process_tree
            if not _kill_process_tree(proc.pid):
                logging.getLogger(__name__).exception('Failed to stop partially initialized execution %s', session)
                return {'ok': False, 'busy': True, 'detail': str(exc)}
            proc.wait()
        if 'output' in locals():
            output.close()
        if mcp_overlay is not None:
            cleanup_overlay(mcp_overlay)
        conn = db.get_connection()
        try:
            conn.execute("UPDATE wake_runs SET ended_at=?,exit_code=1 WHERE session=? AND started_at=?",
                         (time.time(), session, wake_started_at))
            conn.commit()
        finally:
            conn.close()
        with _session_lock:
            _running_sessions.discard(session)
            _running_wakes.pop(session, None)
            _native_procs.pop(session, None)
        _handle_task_exit(profile, project, kind, session, wake_started_at, time.time(),
                          1, event_floor=locals().get('event_floor'), task_id=bound_task, error=str(exc), job_id=job_id)
        detail = f'{type(exc).__name__}: {exc}'
        _report_execution_error(profile, project, thread_id, session, f'{session}:{wake_started_at}', detail,
                                job_id=job_id, parent_id=trigger_msg_id, env=env)
        from .execution_errors import MESSAGES, classify
        return {"ok": False, "channel": "subprocess", "detail": MESSAGES[classify(detail)], "failure_handled": True}


def is_chat_active(profile: str) -> bool:
    """引擎侧运行时校验：该 profile 当前是否处于 chat（话题）执行上下文。

    wake_jobs.status='running' 表示队列
    worker 的信号投递窗口（deliver fire-and-forget，running→done 亚秒完成），
    bot 真正读话题/思考/回复发生在 job 已 done 之后——查 running 恒 False。

    正确事实源：chat 唤醒投递成功时记 chat 上下文到期时刻
    wake_jobs.chat_context_until = now + 执行窗口（CHAT_CONTEXT_SECONDS=600s，量纲=一次话题讨论实际执行时长）。
    MCP 执行类工具调用时查 now < chat_context_until，窗口内一律拒绝——
    覆盖「信号发出 → bot 醒来 → 读 inbox/thread → 思考 → 回复」全过程。
    API server 通道下 bot 由 gateway 管理、引擎拿不到进程退出事件，宽限窗口
    是唯一可跟踪的执行期事实源（work 进程同时跑的场景由 chat/work 并行
    设计保证：work 唤醒是独立 job，不受 chat 窗口影响——但同一 profile 的
    MCP 进程共享，work 调用会被误拒，这是已接受的保守取舍：宁可误拒 work
    一次（下周期重试），不可放进 chat 一次误开工）。
    """
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM wake_jobs "
            "WHERE profile=? AND kind='chat' "
            "AND chat_context_until IS NOT NULL AND chat_context_until > ?",
            (profile, db.now_iso()),
        ).fetchone()
        return bool(row["n"])
    finally:
        conn.close()
