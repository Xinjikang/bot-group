"""Shared, read-only requirement decomposition checks (req#99, req#111).

req#111：source_ref 简化去 confirm——从 `thread_id=…; message_id=…; confirm=decompose`
简化为 `thread_id=…; message_id=…`。授权依据为用户创建指令消息，引擎校验
来源可追溯且引用有效（格式 + 消息由用户发出 + 未撤回 + 属本项目），
创建意图由 agent 判断，不再由 confirm 自填语义充当授权凭据。
"""
import re

FIELDS = ('background_goal', 'scope', 'approach_basis', 'acceptance', 'undecided', 'source_ref')
# req#111：confirm 尾巴仅作旧数据兼容（可选匹配、废弃其语义），不再区分 direction/decompose。
SOURCE = re.compile(
    r'\s*thread_id\s*=\s*([0-9]{1,19})\s*;\s*message_id\s*=\s*([0-9]{1,19})'
    r'\s*(?:;\s*confirm\s*=\s*(?:direction|decompose))?\s*'
)


def check_source(conn, project, source_ref):
    """来源可追溯/引用有效校验，返回错误文案或 None（调用方拥有事务）。"""
    match = SOURCE.fullmatch(source_ref or '')
    if not match:
        return ('来源信息无效：缺少 thread_id 或 message_id，'
                '格式应为 thread_id=<int>; message_id=<int>（点对点 thread_id=0）')
    thread_id, message_id = int(match[1]), int(match[2])
    # Bound IDs before SQLite binding to reject oversized integers explicitly.
    if max(thread_id, message_id) > 2**63 - 1 or message_id == 0:
        return '来源信息无效：来源 ID 超出有效范围，请补有效来源消息'
    message = conn.execute(
        'SELECT project, sender_type, retract_by, thread_id FROM messages WHERE id=?',
        (message_id,),
    ).fetchone()
    thread = conn.execute('SELECT project FROM threads WHERE id=?', (thread_id,)).fetchone() if thread_id else None
    if not message or message['project'] != project or message['sender_type'] != 'user' or message['retract_by'] is not None:
        return '来源信息待补充：来源消息须由本项目用户发出且仍有效'
    if thread_id and (not thread or thread['project'] != project):
        return '来源信息待补充：来源话题须存在且属于本项目'
    if (message['thread_id'] or 0) != thread_id:
        return '来源信息待补充：来源消息与 thread_id 不匹配'
    return None


def check(conn, requirement):
    """Return a field-specific error or None; caller owns the transaction."""
    req = dict(requirement)
    if all(req.get(field, '') == '' for field in FIELDS) and req.get('description'):
        return None
    errors = []
    for field in FIELDS[:4]:
        if not (req.get(field) or '').strip():
            errors.append({'field': field, 'message': f'请通过 req_update 补齐 {field} 后再拆解'})
    undecided = (req.get('undecided') or '').strip()
    if undecided != '无':
        errors.append({'field': 'undecided', 'message':
            '存在未决事项，请回话题澄清解决，再由需求维护者通过 req_update 改为『无』'
            if undecided else 'undecided 为空，请填『无』或逐条记录未决事项'})
    source_error = check_source(conn, req.get('project'), req.get('source_ref'))
    if source_error:
        errors.append({'field': 'source_ref', 'message': source_error})
    if errors:
        return {'error': '；'.join(e['message'] for e in errors),
                'code': 'requirement_decomposition_blocked', 'requirement_id': req['id'],
                'field_errors': errors}
    return None
