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
import tempfile
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
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, encoding='utf-8', mode='w', delete=False) as handle:
        tmp = Path(handle.name)
        try:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


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
                   f"&fingerprint={fingerprint}&member={member}")
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


def issue_persistent(member: str) -> str:
    """配对兑换成功后签发持久身份凭据（manager node2057 ①）：重连不再
    用一次性配对票，改用持久凭据认证；吊销时 clear_persistent 才断。
    返回凭据明文（只在 hello_ok 帧下发一次，不落日志）。"""
    with _LOCK:
        state = _load_state()
        token = secrets.token_urlsafe(32)
        state["persistent"] = {
            "member": member,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "paired_at": time.time(),
        }
        _save_state(state)
    logger.info("bot-group relay persistent identity issued member=%s", member)
    return token


def verify_persistent(member: str, token: str) -> bool:
    """持久身份认证：已配对成员 + 持久凭据匹配（不销毁，可反复认证）。"""
    with _LOCK:
        persistent = _load_state().get("persistent") or {}
        if persistent.get("member") != member:
            return False
        expected = persistent.get("token_hash") or ""
    if not expected:
        return False
    return hmac.compare_digest(expected, hashlib.sha256((token or "").encode()).hexdigest())


def clear_persistent(member: str | None = None) -> None:
    """吊销持久身份（吊销才断：清态后该成员重连须重新配对）。"""
    with _LOCK:
        state = _load_state()
        persistent = state.get("persistent")
        if persistent is None or (member and persistent.get("member") != member):
            return
        state.pop("persistent", None)
        _save_state(state)
    logger.info("bot-group relay persistent identity cleared member=%s", member)


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


def register_offer(member: str, engine_url: str, invite: str) -> tuple[str, str]:
    """Derive separate proof and credential from a local seed scoped to one invitation."""
    scope = [member, engine_url, hashlib.sha256(invite.encode()).hexdigest()]
    with _LOCK:
        state = _load_state()
        pending = state.get('register_offer') or {}
        if pending.get('scope') != scope:
            pending = {'scope': scope, 'seed': secrets.token_hex(32)}
            state['register_offer'] = pending
            _save_state(state)
        seed = bytes.fromhex(pending['seed'])
    return (hmac.new(seed, b'relay-join-proof-v1', 'sha256').hexdigest(),
            hmac.new(seed, b'relay-node-key-v1', 'sha256').hexdigest())


def save_register_credential(member: str, credential: str, *, engine_url: str = '', invite: str = '') -> None:
    """Persist confirmed credentials before opening a normal authenticated link."""
    with _LOCK:
        state = _load_state()
        state['register'] = {'member': member, 'credential': credential, 'saved_at': time.time()}
        if invite:
            scope = [member, engine_url, hashlib.sha256(invite.encode()).hexdigest()]
            state['register']['scope'] = scope
            if (state.get('register_offer') or {}).get('scope') == scope:
                state.pop('register_offer')
        _save_state(state)
    logger.info("bot-group relay register credential saved member=%s", member)


def load_register_credential(member: str, *, engine_url: str = '', invite: str = '') -> str:
    """A new invitation cannot silently reuse credentials for an earlier one."""
    with _LOCK:
        saved = _load_state().get("register") or {}
        if saved.get("member") == member:
            scope = [member, engine_url, hashlib.sha256(invite.encode()).hexdigest()]
            if not invite or saved.get('scope') == scope:
                return saved.get("credential") or ""
    return ""


def write_link_file(pairing_url: str, *, path: str | None = None) -> str:
    """非交互终端输出：链接写 0600 文件（POSIX），不落 stdout。
    manager node2060 ①：从创建时即限权（os.open O_CREAT 0600，非事后
    chmod——创建瞬间无宽权限窗口）；写失败如实抛 OSError（不吞）；
    写临时文件后原子 rename。path=None 时写到插件 state/pairing-link.txt。"""
    target = Path(path) if path else _state_dir() / "pairing-link.txt"
    _state_dir().mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError:
        # Windows 无 0600 语义：退化为普通创建（继承目录权限）
        fd = os.open(tmp, flags)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(pairing_url + "\n")
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, target)
    return str(target)
