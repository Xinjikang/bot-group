"""Durable, identity-bound receipts for MCP writes and uncertain results."""

import functools
import hashlib
import inspect
import json
import re

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import auth, db, events
from .operation_transaction import Transaction, current


def _json(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _owner(request):
    actor, kind = request.state.identity
    if kind != 'bot':
        raise HTTPException(403, 'Operation receipts require a member credential')
    scope = getattr(request.state, 'execution_scope', None)
    identity = {key: scope[key] for key in auth.EXECUTION_FIELDS} if scope else None
    return hashlib.sha256(_json([actor, identity]).encode()).hexdigest()


def _validate_id(operation_id):
    if not re.fullmatch(r'[a-f0-9]{32}', operation_id):
        raise HTTPException(422, 'operation_id must be 32 lowercase hexadecimal characters')


def _check_access(conn, request, project, thread_id):
    from .access import require_project, require_thread
    from . import task_access
    actor = request.state.identity[0]
    scope = getattr(request.state, 'execution_scope', None)
    if project:
        task_access.validate(conn, actor, project, 'operation_receipt', scope)
    elif scope:
        require_project(conn, actor, scope['project'])
        task_access.validate(conn, actor, scope['project'], 'operation_receipt', scope)
    if thread_id is not None:
        require_thread(conn, actor, thread_id)


def receipt(request, operation_id):
    _validate_id(operation_id)
    owner = _owner(request)
    conn = db.get_connection()
    try:
        row = conn.execute('SELECT * FROM mcp_operations WHERE owner=? AND operation_id=?',
                           (owner, operation_id)).fetchone()
        if row is None:
            return {'operation_id': operation_id, 'state': 'unknown'}
        _check_access(conn, request, row['project'], row['thread_id'])
        result = {'operation_id': operation_id, 'state': row['state']}
        if row['state'] == 'completed':
            result.update(status_code=row['status_code'], result=json.loads(row['response']))
        return result
    finally:
        conn.close()


def recorded(function):
    """Wrap synchronous JSON handlers after FastAPI authentication/validation."""
    signature = inspect.signature(function, eval_str=True)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        request = bound.arguments.get('request')
        operation_id = request.headers.get('X-Group-Operation-Id', '') if request else ''
        if not operation_id or request.state.identity[1] != 'bot':
            return function(*args, **kwargs)
        _validate_id(operation_id)
        owner = _owner(request)
        body = jsonable_encoder(bound.arguments.get('body', {}))
        fingerprint = hashlib.sha256(_json([request.url.path, body]).encode()).hexdigest()
        scope = getattr(request.state, 'execution_scope', None) or {}
        project = scope.get('project') or body.get('project') or body.get('arguments', {}).get('project', '')
        thread_id = scope.get('thread_id', body.get('thread_id'))
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            conn.execute('BEGIN IMMEDIATE')
            _check_access(conn, request, project, thread_id)
            row = conn.execute('SELECT * FROM mcp_operations WHERE owner=? AND operation_id=?',
                               (owner, operation_id)).fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint:
                    raise HTTPException(409, 'operation_id already belongs to another request')
                _check_access(conn, request, row['project'], row['thread_id'])
                if row['state'] == 'completed':
                    return JSONResponse(json.loads(row['response']), status_code=row['status_code'])
                return JSONResponse({'error': 'Operation result unknown; query its receipt before proceeding',
                                     'code': 'operation_unknown', 'operation_id': operation_id}, status_code=409)
            conn.execute('INSERT INTO mcp_operations(owner,operation_id,fingerprint,project,thread_id,state,created_at) '
                         "VALUES(?,?,?,?,?,'unknown',?)",
                         (owner, operation_id, fingerprint, project, thread_id, db.now_iso()))
            conn.commit()
            # A crash after reservation keeps unknown even if external effects already happened.
            conn.execute('BEGIN IMMEDIATE')
            transaction = Transaction(conn)
            token = current.set(transaction)
            try:
                try:
                    result = function(*args, **kwargs)
                except HTTPException as exc:
                    result = JSONResponse({'detail': jsonable_encoder(exc.detail)},
                                          status_code=exc.status_code, headers=exc.headers)
                status = result.status_code if isinstance(result, JSONResponse) else 200
                payload = json.loads(result.body) if isinstance(result, JSONResponse) else jsonable_encoder(result)
                conn.execute("UPDATE mcp_operations SET state='completed',status_code=?,response=? "
                             'WHERE owner=? AND operation_id=?', (status, _json(payload), owner, operation_id))
                conn.commit()
            finally:
                current.reset(token)
            for event_type, data in transaction.events:
                events.bus.publish(event_type, data)
            return result
        finally:
            conn.close()

    wrapped.__signature__ = signature
    return wrapped
