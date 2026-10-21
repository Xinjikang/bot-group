"""Synchronize file credentials from the engine's primary Codex home."""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from . import discovery, profile_config

_LOCK = threading.Lock()


class AuthSyncError(ValueError):
    """A credential operation failed without exposing credential contents."""


def read_auth(home):
    try:
        raw = profile_config._snapshot(Path(home).resolve(), ['auth.json'])['auth.json']
        if raw is None:
            return None, {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        if value.get('tokens') is not None and not isinstance(value['tokens'], dict):
            raise ValueError()
        return raw, value
    except Exception:
        raise AuthSyncError('无法读取 Codex 文件认证，请检查 auth.json 格式与文件权限') from None


def _expires(auth):
    token = (auth.get('tokens') or {}).get('access_token')
    if not isinstance(token, str):
        return None
    try:
        payload = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['exp']
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        pass
    return None


def _usable(auth, now):
    expiry = _expires(auth)
    return expiry > now if expiry is not None else bool(auth.get('OPENAI_API_KEY'))


def sync(home, *, unauthorized=False):
    """Copy usable primary credentials when missing, expired or rejected by Codex.

    JWT expiry is a local scheduling hint; the provider validates the token.
    Unknown token formats and keyring credentials retain native authentication.
    """
    target = Path(home).resolve()
    primary = discovery._codex_root().resolve()
    if target == primary:
        return False
    with _LOCK:
        old_raw, old = read_auth(target)
        now = time.time()
        expiry = _expires(old)
        if old_raw is not None and not unauthorized and (expiry is None or expiry > now):
            return False
        raw, auth = read_auth(primary)
        if raw is None or not _usable(auth, now):
            if expiry is not None and expiry <= now and not unauthorized:
                raise AuthSyncError('Codex 凭据已过期，主账户暂无有效文件凭据，请恢复主账户登录')
            return False
        old_account = (old.get('tokens') or {}).get('account_id')
        account = (auth.get('tokens') or {}).get('account_id')
        if old_account and old_account != account:
            raise AuthSyncError('Codex profile 与主账户身份不同，请确认主账户登录')
        if auth == old:
            return False
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=target, prefix='.auth-', suffix='.tmp', delete=False) as stream:
                staged = Path(stream.name)
                os.chmod(staged, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Preserve a native refresh that completed while preparing the copy.
            if read_auth(target)[0] != old_raw or read_auth(primary)[0] != raw:
                return False
            os.replace(staged, target / 'auth.json')
            return True
        except Exception:
            raise AuthSyncError('无法同步 Codex 文件认证，请检查 profile 目录权限') from None
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)


def is_auth_error(event):
    """Match runtime authentication failures, excluding tool outputs."""
    if not isinstance(event, dict):
        return False
    kind = event.get('type')
    if kind == 'error':
        message = event.get('message')
    elif kind == 'turn.failed':
        error = event.get('error')
        message = error.get('message') if isinstance(error, dict) else None
    elif kind == 'item.completed' and isinstance(event.get('item'), dict):
        item = event['item']
        message = item.get('message') if item.get('type') == 'error' else None
    else:
        return False
    if not isinstance(message, str):
        return False
    text = message.lower()
    return ('401' in text and ('unauthorized' in text or 'authentication' in text)
            or 'token_revoked' in text or 'refresh_token_invalidated' in text)
