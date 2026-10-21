"""Durable submission receipts and live phase reporting for project setup."""
import hashlib
import json
import re

from fastapi import HTTPException
from . import db

ACTIVE = {}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def begin(body):
    request_id = body.get('request_id')
    if request_id is None:
        return None
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', request_id):
        raise HTTPException(400, '无效的创建请求标识')
    digest = hashlib.sha256(encode(body).encode()).hexdigest()
    conn = db.get_connection()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM project_setup_requests WHERE id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise HTTPException(409, '请求内容已变化，请重新预览确认')
                if row['status'] == 'ready':
                    return json.loads(row['result'])
                raise HTTPException(409, '上次操作有进度记录，请查询结果或重新预览确认')
            conn.execute('INSERT INTO project_setup_requests(id,digest,status,payload,updated_at) VALUES(?,?,?,?,?)',
                         (request_id, digest, 'running', encode(body), db.now_iso()))
        phase(body, '检查目录和确认版本')
    finally:
        conn.close()


def phase(body, name):
    if body.get('request_id'):
        ACTIVE[body['request_id']] = name


def complete(conn, body, result):
    if body.get('request_id'):
        conn.execute("UPDATE project_setup_requests SET status='ready',result=?,updated_at=? WHERE id=?",
                     (encode(result), db.now_iso(), body['request_id']))


def failed(body, error):
    if not body.get('request_id'):
        return
    conn = db.get_connection()
    try:
        with conn:
            conn.execute("UPDATE project_setup_requests SET status='failed',error=?,updated_at=? WHERE id=? AND status='running'",
                         (str(getattr(error, 'detail', error)), db.now_iso(), body['request_id']))
    finally:
        ACTIVE.pop(body['request_id'], None)
        conn.close()


def view(request_id):
    conn = db.get_connection()
    try:
        row = conn.execute('SELECT * FROM project_setup_requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '尚未收到该创建请求，可以重试提交')
        return {'status': row['status'], 'phase': ACTIVE.get(request_id, '操作已中断，请重新检查' if row['status'] == 'running' else ''),
                'result': json.loads(row['result']), 'error': row['error']}
    finally:
        conn.close()
