"""Bounded MCP HTTP transport; uncertain writes are reconciled without replay."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import uuid

import httpx

_pending = {}
_lock = threading.Lock()
_BACKOFF = (1, 2, 4, 8)
BUDGET = 30.0


async def _send(method, url, *, headers, body=None, params=None):
    async with httpx.AsyncClient(timeout=BUDGET, follow_redirects=False) as client:
        return await client.request(method, url, headers=headers, json=body, params=params)


def _decode(response):
    if response.status_code >= 400:
        return {'error': f'engine {response.status_code}: {response.text[:200]}'}
    try:
        value = response.json()
        if isinstance(value, dict):
            return value
        return {'error': 'engine non-json: expected a JSON object'}
    except ValueError:
        return {'error': f'engine non-json: {response.text[:200]}'}


def _unknown(operation_id):
    return {'error': 'Operation result unknown; query the receipt before repeating the action',
            'code': 'operation_unknown', 'operation_id': operation_id}


async def _reconcile(url, headers, operation_id):
    for attempt in range(5):
        try:
            response = await _send('GET', f'{url}/mcp-operations/{operation_id}', headers=headers)
            if response.status_code == 200:
                data = response.json()
                if data.get('state') == 'completed':
                    if data['status_code'] >= 400:
                        return {'error': f"engine {data['status_code']}: {json.dumps(data['result'], ensure_ascii=False)[:200]}"}
                    return data['result']
            elif response.status_code in (401, 403):
                return {**_unknown(operation_id), 'receipt_error': f'engine {response.status_code}'}
        except (httpx.RequestError, ValueError, KeyError, TypeError, AttributeError):
            pass
        if attempt < 4:
            await asyncio.sleep(_BACKOFF[attempt])
    return _unknown(operation_id)


async def _run(method, url, path, headers, body, params, operation_id, recovering, state):
    if recovering:
        state['uncertain'] = True
        return await _reconcile(url, headers, operation_id)
    for attempt in range(5):
        state['uncertain'] = True
        try:
            response = await _send(method, url + path, headers=headers, body=body, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            state['uncertain'] = False
        except httpx.RequestError:
            if operation_id:
                return await _reconcile(url, headers, operation_id)
            return {'error': 'Engine API unavailable: response unknown', 'code': 'engine_unavailable'}
        else:
            if response.status_code == 503 and response.headers.get('X-Group-Not-Accepted') == '1':
                state['uncertain'] = False
            elif response.status_code >= 500 or response.is_redirect:
                if operation_id:
                    return await _reconcile(url, headers, operation_id)
                return _decode(response)
            else:
                result = _decode(response)
                try:
                    unresolved = response.json().get('code') == 'operation_unknown'
                except (ValueError, AttributeError):
                    unresolved = False
                if operation_id and (unresolved or
                                     (response.status_code < 400 and 'engine non-json:' in result.get('error', ''))):
                    return await _reconcile(url, headers, operation_id)
                return result
        if attempt < 4:
            await asyncio.sleep(_BACKOFF[attempt])
    return {'error': 'Engine API unavailable: 引擎离线（重试预算耗尽）', 'code': 'engine_offline'}


def request(method, url, path, *, headers, body=None, params=None):
    recorded = method == 'POST' and (path.startswith('/task-tools/') or
                                    path in ('/msg', '/thread', '/thread/task-update', '/inbox/seen'))
    operation_id, recovering, key = None, False, None
    if recorded:
        key = hashlib.sha256(json.dumps([url, path, headers, body], sort_keys=True,
                                       ensure_ascii=False).encode()).hexdigest()
        with _lock:
            recovering = key in _pending
            operation_id = _pending.setdefault(key, uuid.uuid4().hex)
        headers = {**headers, 'X-Group-Operation-Id': operation_id}
    state = {'uncertain': False}

    async def bounded():
        try:
            return await asyncio.wait_for(
                _run(method, url, path, headers, body, params, operation_id, recovering, state), BUDGET)
        except asyncio.TimeoutError:
            if operation_id and state['uncertain']:
                return _unknown(operation_id)
            return {'error': 'Engine API unavailable: 引擎离线（30s 预算耗尽）', 'code': 'engine_offline'}

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result = asyncio.run(bounded())
    else:
        # FastMCP may call synchronous tools on its own running event loop.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='mcp-http') as executor:
            result = executor.submit(lambda: asyncio.run(bounded())).result()
    if key and result.get('code') != 'operation_unknown':
        with _lock:
            _pending.pop(key, None)
    return result
