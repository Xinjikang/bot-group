"""Generation-bound approval projections and explicit administrator responses."""
import hashlib
import json
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .hermes_gateway_client import GatewayRefusal, GatewayTransportError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS hermes_approval_snapshots (
    execution_id TEXT PRIMARY KEY,
    generation INTEGER,
    prompts TEXT NOT NULL DEFAULT '[]',
    checked_at REAL NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
'''
_locks = [threading.RLock() for _ in range(32)]
_choices = {'once', 'deny', 'session', 'always'}


def _lock(execution_id):
    return _locks[int(hashlib.sha256(execution_id.encode()).hexdigest(), 16) % len(_locks)]


def _save(conn, row, prompts, error=''):
    conn.execute('INSERT INTO hermes_approval_snapshots VALUES(?,?,?,?,?) '
                 'ON CONFLICT(execution_id) DO UPDATE SET generation=excluded.generation, '
                 'prompts=excluded.prompts, checked_at=excluded.checked_at, error=excluded.error',
                 (row['execution_id'], row['generation'], json.dumps(prompts, ensure_ascii=False), time.time(), error))
    conn.commit()


def _prompts(snapshot, row):
    if snapshot.get('session_id') != row['session_id'] or snapshot.get('execution_generation') != row['generation']:
        raise GatewayRefusal('stale_generation')
    if not isinstance(snapshot.get('prompts'), list):
        raise GatewayRefusal('approval_snapshot_unsupported')
    result = []
    for prompt in snapshot['prompts']:
        if prompt.get('kind') != 'approval' or prompt.get('execution_generation') != row['generation']:
            continue
        if not isinstance(prompt.get('prompt_id'), str) or not prompt['prompt_id']:
            continue
        choices = prompt.get('choices')
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            continue
        result.append({'prompt_id': prompt['prompt_id'], 'generation': row['generation'],
                       'command': str(prompt.get('command', '')), 'description': str(prompt.get('description', '')),
                       'choices': [c for c in choices if c in _choices],
                       'edit': prompt.get('edit') if isinstance(prompt.get('edit'), dict) else None})
    return result


def sync(conn, row, client):
    """Approval availability never changes execution ownership or its outcome."""
    if row['state'] != 'running' or row['generation'] is None:
        return
    with _lock(row['execution_id']):
        try:
            snapshot = client.rpc('session.resume', session_id=row['session_id'], omit_messages=True, timeout=5)
            _save(conn, row, _prompts(snapshot, row))
        except Exception:
            conn.rollback()
            try:
                conn.execute('UPDATE hermes_approval_snapshots SET error=? WHERE execution_id=?',
                             ('授权状态查询失败，正在重试；也可到 Hermes 查看。', row['execution_id']))
                conn.commit()
            except Exception:
                conn.rollback()


def listing(project=None, thread_id=None):
    conn = db.get_connection()
    try:
        rows = conn.execute('SELECT e.*, s.prompts, s.generation AS snapshot_generation, s.checked_at, s.error AS approval_error '
                            'FROM hermes_executions e JOIN hermes_approval_snapshots s USING(execution_id) '
                            "WHERE e.state != 'terminal' ORDER BY e.started_at DESC").fetchall()
        result = []
        for row in rows:
            if (project and row['project'] != project) or (thread_id is not None and row['thread_id'] != thread_id):
                continue
            error = row['approval_error']
            if (time.time() - row['checked_at'] > 15 or row['state'] != 'running'
                    or row['snapshot_generation'] != row['generation'] or row['cancel_reason']):
                error = '正在核对授权状态，请稍候；也可到 Hermes 查看。'
            for prompt in json.loads(row['prompts']):
                result.append(dict(prompt, execution_id=row['execution_id'], project=row['project'],
                                   profile=row['profile'], thread_id=row['thread_id'], error=error))
        return {'approvals': result}
    finally:
        conn.close()


def respond(execution_id, prompt_id, generation, choice):
    from . import hermes_gateway, wake
    with _lock(execution_id):
        if wake._shutting_down.is_set():
            raise HTTPException(503, '服务正在重启，请稍后重新核对授权状态')
        conn = db.get_connection()
        client = None
        try:
            row = conn.execute('SELECT * FROM hermes_executions WHERE execution_id=?', (execution_id,)).fetchone()
            if row is None:
                raise HTTPException(404, '授权所属执行不存在')
            if row['state'] != 'running' or row['generation'] != generation or row['cancel_reason']:
                raise HTTPException(409, '这次执行已变化，请刷新授权状态')
            client, _, _ = hermes_gateway.connection(row['profile'], home=row['home'], read_only=True)
            snapshot = client.rpc('session.resume', session_id=row['session_id'], omit_messages=True, timeout=5)
            prompts = _prompts(snapshot, row)
            current = next((p for p in prompts if p['prompt_id'] == prompt_id), None)
            if current is None:
                _save(conn, row, prompts)
                return {'status': 'already_resolved'}
            if choice not in current['choices']:
                raise HTTPException(400, '该授权请求不支持此选项')
            latest = conn.execute('SELECT state, generation, cancel_reason FROM hermes_executions WHERE execution_id=?', (execution_id,)).fetchone()
            if latest['state'] != 'running' or latest['generation'] != generation or latest['cancel_reason']:
                raise HTTPException(409, '这次执行已变化，请刷新授权状态')
            result = client.rpc('approval.respond', session_id=row['session_id'], execution_generation=generation,
                                prompt_id=prompt_id, choice=choice, timeout=10)
            if result.get('status') not in ('resolved', 'already_resolved'):
                raise GatewayTransportError('Approval result unknown')
            _save(conn, row, [p for p in prompts if p['prompt_id'] != prompt_id])
            return {'status': result['status']}
        except GatewayRefusal as exc:
            raise HTTPException(409, 'Hermes 拒绝了这次授权操作，请刷新状态后核对（' + exc.reason + '）') from exc
        except (GatewayTransportError, OSError) as exc:
            conn.execute('UPDATE hermes_approval_snapshots SET error=? WHERE execution_id=?',
                         ('授权结果暂未确认，正在重新查询，请勿重复提交。', execution_id))
            conn.commit()
            raise HTTPException(503, '授权结果暂未确认，正在重新查询，请勿重复提交。') from exc
        finally:
            if client:
                client.close()
            conn.close()


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt_id: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=0, strict=True)
    choice: str = Field(min_length=1, max_length=30)


def router(identity, admin):
    routes = APIRouter(prefix='/hermes-approvals')

    @routes.get('')
    def get(project: str | None = None, thread_id: int | None = None, who=Depends(identity)):
        admin(who, '授权请求仅供用户查看')
        return listing(project, thread_id)

    @routes.post('/{execution_id}')
    def post(execution_id: str, body: Decision, who=Depends(identity)):
        admin(who, '只有用户可以处理授权')
        return respond(execution_id, body.prompt_id, body.generation, body.choice)

    return routes
