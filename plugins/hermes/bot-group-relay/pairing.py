"""一次性配对链接（req#173 验收 2）：自托管、短时单次、兑换原子。

- 链接含入口地址 + 证书指纹 + 一次性配对凭据；导入时先显示设备身份与短指纹
  供确认，兑换原子、首次成功即作废；
- 重新生成时旧未用链接失效、已配对连接不受影响（吊销才断）；
- 日志只记签发事件（时间/成员/对端地址），绝不记录链接/凭据值；
- 非交互终端输出链接写 0600 文件（POSIX）；Windows 沿用文件权限继承。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 600.0   # 短时：10 分钟内兑换有效
_LOCK = threading.Lock()


def _state_dir() -> Path:
    """配对状态目录：插件目录 state/（发布包内可写，随插件升级保留由安装
    脚本负责）。"""
    return Path(__file__).resolve().parent / "state"


def _state_path() -> Path:
    return _state_dir() / "pairing.json"


def _load_state() -> dict:
    path = _state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def issue_pairing(*, member: str, advertise: str, port: int, fingerprint: str,
                  scheme: str = "wss") -> dict:
    """签发一次性配对：替换旧未用凭据（旧链接即刻失效），返回
    {pairing_url, pairing_token, fingerprint_short, expires_at}。

    日志只记签发事件，不记链接/凭据。"""
    with _LOCK:
        state = _load_state()
        token = secrets.token_urlsafe(32)
        now = time.time()
        state["pairing"] = {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "member": member,
            "advertise": advertise,
            "port": int(port),
            "fingerprint": fingerprint,
            "scheme": scheme,
            "issued_at": now,
            "expires_at": now + PAIRING_TTL_SECONDS,
            "redeemed": False,
        }
        _save_state(state)
    pairing_url = (f"{scheme}://{advertise}:{port}?pairing_token={token}"
                   f"&fingerprint={fingerprint}")
    logger.info("bot-group relay pairing issued member=%s advertise=%s:%s",
                member, advertise, port)
    return {
        "pairing_url": pairing_url,
        "pairing_token": token,
        "fingerprint": fingerprint,
        "fingerprint_short": fingerprint[:8] if fingerprint else "",
        "expires_at": state["pairing"]["expires_at"],
    }


def redeem_pairing(*, member: str, token: str, fingerprint: str = "") -> bool:
    """原子兑换：凭据匹配、未过期、未兑换才通过；首次成功即作废（再次兑换
    失败）。调用方持锁场景（握手线程）由 _LOCK 串行保证。"""
    with _LOCK:
        state = _load_state()
        pairing = state.get("pairing") or {}
        if not pairing or pairing.get("redeemed"):
            return False
        if pairing.get("member") != member:
            return False
        if time.time() > float(pairing.get("expires_at") or 0):
            return False
        expected = hashlib.sha256((token or "").encode()).hexdigest()
        if not hmac.compare_digest(pairing.get("token_hash") or "", expected):
            return False
        if fingerprint and pairing.get("fingerprint") and \
                not hmac.compare_digest(pairing["fingerprint"], fingerprint):
            return False
        pairing["redeemed"] = True
        pairing["redeemed_at"] = time.time()
        _save_state(state)
    logger.info("bot-group relay pairing redeemed member=%s", member)
    return True


def pairing_metadata() -> dict:
    """当前配对状态（脱敏）：指纹/端口/签发与过期时间，不含凭据值。"""
    with _LOCK:
        pairing = _load_state().get("pairing") or {}
        return {
            "member": pairing.get("member", ""),
            "advertise": pairing.get("advertise", ""),
            "port": pairing.get("port", 0),
            "fingerprint_short": (pairing.get("fingerprint") or "")[:8],
            "issued_at": pairing.get("issued_at", 0),
            "expires_at": pairing.get("expires_at", 0),
            "redeemed": bool(pairing.get("redeemed")),
        }


def write_link_file(pairing_url: str, *, path: str | None = None) -> str | None:
    """非交互终端输出：链接写 0600 文件（POSIX），不落 stdout。
    path=None 时写到插件 state/pairing-link.txt。返回文件路径。"""
    target = Path(path) if path else _state_dir() / "pairing-link.txt"
    _state_dir().mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(pairing_url + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows 无 0600 语义：继承目录权限
    os.replace(tmp, target)
    return str(target)
