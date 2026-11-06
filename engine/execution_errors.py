"""Persistent execution failures with public categories and private redacted diagnostics."""
import json
import logging
import os
import re

from . import db, events, hermes_profile_names

MESSAGES = {
    'session_state': '会话状态持续查询失败，工作唤醒已暂缓；请检查宿主会话及引擎诊断。',
    'configuration': '配置不完整或无效，请检查配置。',
    'authentication': '身份认证失败，请检查凭证。',
    'missing_credential': '缺少模型凭据，请检查凭据配置或环境变量。',
    'init': '初始化失败，请检查宿主配置、bundle 与版本。',
    'mcp_initialization': '任务工具连接初始化失败，请重试；详细原因已记录日志。',
    'refused': '模型拒绝了本次请求，请调整内容后重试。',
    'cancelled': '本轮执行已被取消。',
    'corrupt': '执行输出解析异常，请查看诊断日志。',
    'permission': '访问被拒绝，请检查权限。',
    'rate_limit': '请求受限，请稍后重试或检查配额。',
    'capacity': '所选模型容量已满，请稍后重试或切换模型。',
    'incomplete': '本轮执行已结束，任务尚未完成或交接，请检查任务记录。',
    'auto_unavailable': 'DSH Auto 审批能力不可用（会话未进入 auto 预设），已阻止执行继续。',
    'timeout': '请求超时，请稍后重试或检查超时设置。',
    'network': '连接失败，请检查网络设置。',
    'upstream': '服务暂时异常，请稍后重试。',
    'unknown': '运行异常，请查看日志。',
}
_SECRET_NAME = re.compile(r'key|token|secret|password|credential|authorization', re.I)


def _status(text, code):
    return bool(re.search(r'(?:http(?:/[\d.]+)?|status(?:_code| code)?|error(?: code)?|code|response)'
                          r'''[\s"':=\[\]-]*''' + code + r'\b', text))


def _failure_text(detail):
    """Prefer terminal JSONL failures over source code and tool output in the tail."""
    text = str(detail)
    last_error = None
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'turn.failed':
            error = event.get('error')
            message = error.get('message') if isinstance(error, dict) else error
            if isinstance(message, str) and message.strip():
                return message
        elif kind == 'error' and last_error is None:
            message = event.get('message')
            if isinstance(message, str) and message.strip():
                last_error = message
    return last_error if last_error is not None else text


def classify(text):
    # req#137 task#268：Auto 审批 fail-closed 标记是引擎作者文本，须在 _failure_text
    # 的「最后一个 JSON 错误事件优先」提取之前判定——否则被 'adapter broken' 之类
    # 终端错误事件吞掉，落 unknown。
    raw = str(text).lower()
    if raw.startswith('session state query failed: '):
        return 'session_state'
    if 'auto review capability unavailable' in raw or 'preset not confirmed' in raw:
        return 'auto_unavailable'
    text = _failure_text(text).lower()
    if 'acp_mcp_discovery_failed' in text or 'mcp_initialization_unavailable' in text:
        return 'mcp_initialization'
    if hermes_profile_names.failure_message(text) or '被 hermes 保留' in text:
        return 'configuration'
    if re.search(r'\bmodel\b[^\n.]{0,80}\bat capacity\b', text):
        return 'capacity'
    # req#137 task#266 验收 5：结果判读六类失败细分。init 词表与
    # dsh_auth.classify_auth_error 的 init 分支一致（initialization/load_failed/
    # failed to parse overlay 等）；凭据缺失按 DSH MISSING_CREDENTIAL 词
    # （'no usable credentials' 保持 configuration，不改既有语义）；损坏 JSON 由
    # dsh reader 计数后注入 'unparseable' 诊断。init/missing_credential 先于缺 final
    # 判读：初始化失败时归因 init（可行动因），缺 final 只是伴随症状。
    if re.search(r'missing_credential|missing credential|no api.?key for', text):
        return 'missing_credential'
    if re.search(r'initialization|initialize|load_failed|failed to load|failed to initialize'
                 r'|failed to parse (?:overlay|patch|profile|config)', text):
        return 'init'
    if re.search(r'\brefusal\b|\bdeclined\b|content policy|guardrail|policy violation'
                 r'|(?:model|assistant|request)\s+refused', text):
        return 'refused'
    if re.search(r'\bcancell?ed\b|\baborted\b|terminated by user|用户取消|手动停止', text):
        return 'cancelled'
    if re.search(r'unparseable|invalid json|malformed json|json(?:decode|parse) error', text):
        return 'corrupt'
    if 'dsh run ended without a final event' in text:
        return 'incomplete'
    if re.search(r'no usable credentials|missing.{0,40}(?:key|credential|config)|'
                 r'(?:key|credential).{0,40}(?:missing|not set|unset)|'
                 r'hermescompatibilityerror|configurationerror|config(?:uration)? (?:error|invalid)|'
                 r'no such file|filenotfounderror|missing executable|缺少|配置无效', text):
        return 'configuration'
    for code, kind in (('401', 'authentication'), ('403', 'permission'), ('429', 'rate_limit')):
        if _status(text, code):
            return kind
    if re.search(r'unauthorized|authenticationerror|invalid api.?key|invalid token|'
                 r'(?:credential|token).{0,30}expired|凭[据证].{0,20}过期', text):
        return 'authentication'
    if re.search(r'forbidden|permissionerror|permission denied|access denied', text):
        return 'permission'
    if re.search(r'rate.?limit|too many requests|quota exceeded|insufficient_quota', text):
        return 'rate_limit'
    if re.search(r'timeout|timed out|超时', text):
        return 'timeout'
    if _status(text, r'5\d\d') or re.search(r'service unavailable|bad gateway|internal server error', text):
        return 'upstream'
    if re.search(r'connection(?:error|refused|reset)|connection (?:failed|refused|reset)|'
                 r'connecterror|network|name resolution|dns|sslerror|certificate verify', text):
        return 'network'
    return 'unknown'


def redact(text, secrets=()):
    text = str(text)
    for secret in sorted({str(v) for v in secrets if v}, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s,;\"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'''(?ix)([\w-]*(?:key|token|secret|password|credential|authorization)[\w-]*["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)https?://[^\s\"\'<>]+', '[URL]', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return text[-4000:]


def _secrets(conn, profile, project, env):
    values = [v for k, v in (env or os.environ).items() if _SECRET_NAME.search(k)]
    try:
        from . import bot_identity, profile_config, discovery
        from dotenv import dotenv_values
        from pathlib import Path
        bot = bot_identity.resolve(conn, project, profile)
        root = Path(bot['config_root']) if bot else Path(discovery._hermes_root())
        codex = bot and bot.get('runtime') == 'codex'
        home = root if codex else bot_identity.hermes_home(profile, root)
        dotenv = dotenv_values(home / '.env', interpolate=False)
        values.extend(v for k, v in dotenv.items() if v and _SECRET_NAME.search(k))
        def collect(item):
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in ('key_env', 'api_key_env') and isinstance(value, str):
                        values.append(dotenv.get(value) or (env or os.environ).get(value))
                    elif _SECRET_NAME.search(str(key)) and isinstance(value, str):
                        values.append(value)
                    collect(value)
            elif isinstance(item, list):
                for value in item:
                    collect(value)
        if codex:
            import tomlkit
            content = profile_config._snapshot(home, ['auth.json', 'config.toml'])
            collect(json.loads(content['auth.json']) if content['auth.json'] else {})
            collect(tomlkit.parse(content['config.toml'].decode('utf-8')) if content['config.toml'] else {})
        else:
            collect(profile_config._yaml(profile_config._snapshot(home, ['config.yaml'])['config.yaml'])[1])
    except Exception:
        # The generic redactor still applies when a profile has disappeared.
        pass
    return values


def report(profile, project, thread_id, attempt, detail, *, exit_code=1, parent_id=None, job_id=None, env=None):
    """Insert one engine notification per execution without routing new Bot work."""
    category = classify(detail)
    conn = db.get_connection()
    try:
        diagnostic = redact(detail, _secrets(conn, profile, project, env))
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT message_id FROM execution_errors WHERE project=? AND profile=? AND attempt=?',
                                (project, profile, attempt)).fetchone()
        if previous:
            return previous['message_id']
        member = conn.execute('SELECT display_name FROM members WHERE profile=?', (profile,)).fetchone()
        label = member['display_name'] if member and member['display_name'] else profile
        round_no, depth = 0, 0
        if thread_id is not None:
            thread = conn.execute('SELECT round FROM threads WHERE id=? AND project=?', (thread_id, project)).fetchone()
            if not thread:
                thread_id = None
            else:
                round_no = thread['round']
                conn.execute('UPDATE threads SET hidden=0 WHERE id=?', (thread_id,))
        parent = conn.execute('SELECT id,round,depth FROM messages WHERE id=? AND project=? AND thread_id IS ?',
                              (parent_id, project, thread_id)).fetchone() if parent_id else None
        parent_id = parent['id'] if parent else None
        if parent:
            round_no, depth = parent['round'], parent['depth'] + 1
        public_message = MESSAGES[category]
        if category == 'session_state':
            reason = str(detail).partition(': ')[2]
            if reason.isidentifier():
                public_message = f'会话状态持续查询失败（{reason}），唤醒已暂缓；任务保持原状态，请检查宿主会话。'
        failure = _failure_text(detail)
        reserved_message = hermes_profile_names.failure_message(failure)
        if reserved_message:
            public_message = reserved_message
        elif 'HermesConfigurationError' in failure:
            public_message = ('Hermes 入口与源码配置异常，请检查 HERMES_BIN、HERMES_HOME，并在修正后重启引擎。'
                              'Windows 原生入口通常位于 <Hermes安装根>/hermes-agent/venv/Scripts/hermes.exe；'
                              '具体原因可查看安装检查输出或引擎日志。')
        elif 'HermesCompatibilityError' in failure:
            public_message = 'Hermes 会话接口不兼容，请更新或修复 Hermes 后重启引擎。'
        content = f'{label}：{public_message}'
        cur = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,parent_id,round,depth,need_reply) "
                           "VALUES(?,?,'engine','engine',?,?,?,?,?,?,0)",
                           (project, db.now_iso(), content, json.dumps(['user', profile]), thread_id, parent_id, round_no, depth))
        message_id = cur.lastrowid
        conn.execute('INSERT INTO execution_errors(project,profile,attempt,category,diagnostic,exit_code,message_id) VALUES(?,?,?,?,?,?,?)',
                     (project, profile, attempt, category, diagnostic, exit_code, message_id))
        conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'user','pending')", (message_id,))
        if job_id is not None:
            conn.execute("UPDATE wake_jobs SET status='failed',detail=?,exit_code=?,ended_at=? WHERE id=? AND project=? AND profile=? AND status IN ('running','done')",
                         (public_message, exit_code, db.now_iso(), job_id, project, profile))
        conn.commit()
    finally:
        conn.close()
    logging.getLogger(__name__).error('Bot execution failed profile=%s attempt=%s category=%s: %s', profile, attempt, category, diagnostic)
    events.bus.publish('message.new', {'message_id': message_id, 'sender': 'engine', 'thread_id': thread_id, 'project': project})
    return message_id
