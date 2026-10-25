"""Persistent execution failures with public categories and private redacted diagnostics."""
import json
import logging
import os
import re

from . import db, events

MESSAGES = {
    'configuration': '配置不完整或无效，请检查配置。',
    'authentication': '身份认证失败，请检查凭证。',
    'permission': '访问被拒绝，请检查权限。',
    'rate_limit': '请求受限，请稍后重试或检查配额。',
    'timeout': '请求超时，请稍后重试或检查超时设置。',
    'network': '连接失败，请检查网络设置。',
    'upstream': '服务暂时异常，请稍后重试。',
    'unknown': '运行异常，请查看日志。',
}
_SECRET_NAME = re.compile(r'key|token|secret|password|credential|authorization', re.I)


def _status(text, code):
    return bool(re.search(r'(?:http(?:/[\d.]+)?|status(?:_code| code)?|error(?: code)?|code|response)'
                          r'''[\s"':=\[\]-]*''' + code + r'\b', text))


def classify(text):
    text = str(text).lower()
    if re.search(r'no usable credentials|missing.{0,40}(?:key|credential|config)|'
                 r'(?:key|credential).{0,40}(?:missing|not set|unset)|'
                 r'configurationerror|config(?:uration)? (?:error|invalid)|'
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
        content = f'{label}：{MESSAGES[category]}'
        cur = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,parent_id,round,depth,need_reply) "
                           "VALUES(?,?,'engine','engine',?,?,?,?,?,?,0)",
                           (project, db.now_iso(), content, json.dumps(['user', profile]), thread_id, parent_id, round_no, depth))
        message_id = cur.lastrowid
        conn.execute('INSERT INTO execution_errors(project,profile,attempt,category,diagnostic,exit_code,message_id) VALUES(?,?,?,?,?,?,?)',
                     (project, profile, attempt, category, diagnostic, exit_code, message_id))
        conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'user','pending')", (message_id,))
        if job_id is not None:
            conn.execute("UPDATE wake_jobs SET status='failed',detail=?,exit_code=?,ended_at=? WHERE id=? AND project=? AND profile=? AND status IN ('running','done')",
                         (MESSAGES[category], exit_code, db.now_iso(), job_id, project, profile))
        conn.commit()
    finally:
        conn.close()
    logging.getLogger(__name__).error('Bot execution failed profile=%s attempt=%s category=%s: %s', profile, attempt, category, diagnostic)
    events.bus.publish('message.new', {'message_id': message_id, 'sender': 'engine', 'thread_id': thread_id, 'project': project})
    return message_id
