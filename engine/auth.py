"""管理员单账号登录认证。

使用单账号和密码，登录成功签发无状态 token（HMAC 签名），
后续请求带 `Authorization: Bearer <token>` 头。

- 密码从环境变量 GROUP_ADMIN_PASSWORD 读（部署时设置，不落库）。
- 签名密钥从 GROUP_SECRET 读；未设置则用密码派生（仅本地开发）。
- token 无状态、可校验、可过期（默认 30 天），重启不失效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import threading
from collections import deque

_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 天
_BOT_SECRET = secrets.token_bytes(32)
_LOGIN_LOCK = threading.Lock()
_LOGIN_ATTEMPTS: dict[str, deque] = {}
LOGIN_WINDOW = 60
LOGIN_LIMIT = 10
MAX_LOGIN_CLIENTS = 4096


def bot_token(profile: str) -> str:
    """Issue a process-local credential bound to one member profile."""
    key = os.environ.get("GROUP_ENGINE_KEY", "").strip().encode("utf-8") or _BOT_SECRET
    return hmac.new(key, profile.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_bot(profile: str, token: str) -> bool:
    return bool(profile and token) and (secrets.compare_digest(
        token.encode("utf-8"), bot_token(profile).encode("ascii"))
        or execution_scope(profile, token) is not None)


def execution_token(profile: str, context: dict) -> str:
    """Issue a member credential restricted to one engine execution."""
    payload = base64.urlsafe_b64encode(json.dumps(
        {key: context[key] for key in EXECUTION_FIELDS},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = hmac.new(bot_token(profile).encode("ascii"),
                         ("execution:" + payload).encode("ascii"), hashlib.sha256).hexdigest()
    token = f"run.{payload}.{signature}"
    if execution_scope(profile, token) is None:
        raise ValueError("Invalid execution context")
    return token


EXECUTION_FIELDS = ("profile", "project", "thread_id", "session", "wake_started_at")


def execution_scope(profile: str, token: str) -> dict | None:
    """Read scope only after verifying its signature and field types."""
    if not token.startswith("run.") or len(token) > 8192:
        return None
    try:
        prefix, payload, signature = token.split(".")
        expected = hmac.new(bot_token(profile).encode("ascii"),
                            ("execution:" + payload).encode("ascii"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
            return None
        context = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        if not isinstance(context, dict) or set(context) != set(EXECUTION_FIELDS):
            return None
        if context["profile"] != profile:
            return None
        if any(not isinstance(context[key], str) or not context[key].strip()
               for key in ("project", "session")):
            return None
        tid, start = context["thread_id"], context["wake_started_at"]
        if tid is not None and (type(tid) is not int or tid <= 0):
            return None
        if type(start) not in (int, float) or not math.isfinite(start):
            return None
        return context
    except (ValueError, UnicodeError, TypeError):
        return None


def allow_login(client: str) -> bool:
    """Reserve an attempt atomically; bound both rate and retained client state."""
    now = time.monotonic()
    with _LOGIN_LOCK:
        for key in list(_LOGIN_ATTEMPTS):
            attempts = _LOGIN_ATTEMPTS[key]
            while attempts and attempts[0] <= now - LOGIN_WINDOW:
                attempts.popleft()
            if not attempts:
                del _LOGIN_ATTEMPTS[key]
        if client not in _LOGIN_ATTEMPTS:
            if len(_LOGIN_ATTEMPTS) >= MAX_LOGIN_CLIENTS:
                return False
            _LOGIN_ATTEMPTS[client] = deque()
        attempts = _LOGIN_ATTEMPTS[client]
        if len(attempts) >= LOGIN_LIMIT:
            return False
        attempts.append(now)
        return True


def _password() -> str:
    return os.environ.get("GROUP_ADMIN_PASSWORD", "").strip()


def configured() -> bool:
    """是否配置了管理员密码。默认免密（未配置）返回 False，供 /auth/status 探测。"""
    return bool(_password())


def _secret() -> bytes:
    env = os.environ.get("GROUP_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    # 本地开发兜底：用密码派生（生产必须设 GROUP_SECRET）
    return hashlib.sha256(("group-engine:" + _password()).encode("utf-8")).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def login(password: str) -> dict:
    """校验密码，成功返回 token。"""
    expected = _password()
    if not expected:
        return {"error": "server not configured with GROUP_ADMIN_PASSWORD"}
    try:
        valid = isinstance(password, str) and secrets.compare_digest(
            password.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        valid = False
    if not valid:
        return {"error": "invalid password"}
    exp = int(time.time()) + _TOKEN_TTL_SECONDS
    payload = f"admin:{exp}"
    token = base64.urlsafe_b64encode(
        f"{payload}.{_sign(payload)}".encode("utf-8")
    ).decode("ascii")
    return {"token": token, "expires_at": exp}


def verify_token(token: str) -> bool:
    """校验 token 是否有效且未过期。"""
    if not token or not configured():
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception:
        return False
    if "." not in raw:
        return False
    payload, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig.encode("utf-8"), _sign(payload).encode("ascii")):
        return False
    try:
        subject, exp = payload.split(":", 1)
        return subject == "admin" and int(exp) > int(time.time())
    except Exception:
        return False
