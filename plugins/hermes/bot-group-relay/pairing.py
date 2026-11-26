"""一次性配对链接（req#173 验收 2）：自托管、短时单次、兑换原子。

- 链接含入口地址 + 证书指纹 + 一次性配对凭据；导入时先显示设备身份与短指纹
  供确认，兑换原子、首次成功即作废；
- 重新生成时旧未用链接失效、已配对连接不受影响（吊销才断）；
- 日志只记签发事件（时间/成员/对端地址），绝不记录链接/凭据值；
- 非交互终端输出链接写 0600 文件（POSIX）；Windows 沿用文件权限继承。
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 600.0   # 短时：10 分钟内兑换有效
_LOCK = threading.Lock()
_PEER = ContextVar("relay_peer", default="")


def _state_dir() -> Path:
    """配对状态目录：插件目录 state/（发布包内可写，随插件升级保留由安装
    脚本负责）。"""
    return Path(__file__).resolve().parent / "state"


def _state_path() -> Path:
    return _state_dir() / "pairing.json"


@contextmanager
def using_peer(peer):
    token = _PEER.set(peer)
    try:
        yield
    finally:
        _PEER.reset(token)


def current_peer():
    return _PEER.get()


def _peer_file():
    base = _state_path()
    peer = current_peer()
    return base.parent / "peers" / peer / base.name if peer else base


def peer_ids():
    base = _state_path()
    return ["", *(p.parent.name for p in sorted((base.parent / "peers").glob("*/" + base.name)))]


def select_peer(member, *, engine_url="", port=None, reserve=True):
    """Keep a legacy binding in place and allocate independent state for new peers."""
    url = engine_url.rstrip("/")
    identity = "url:" + url if url else f"listen:{port or secrets.token_hex(16)}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    with _LOCK:
        occupied = False
        for peer in peer_ids():
            with using_peer(peer):
                state = _load_state()
            register = state.get("register") or {}
            scope = register.get("scope") or []
            address = register.get("engine_url") or (scope[1] if len(scope) == 3 else "")
            listener = state.get("pairing") or {}
            if url and register.get("member") == member and url == address.rstrip("/"):
                return peer
            if not url and port and listener.get("member") == member and port == listener.get("port"):
                return peer
            if not peer:
                occupied = bool(register or listener)
        env_url = (os.environ.get("BOT_GROUP_ENGINE_URL") or "").rstrip("/")
        if env_url and env_url != url:
            occupied = True
        if occupied:
            return digest
        # The CLI and the gateway can register different engines concurrently.
        base = _state_path()
        owner = base.with_suffix(".owner")
        if owner.is_file():
            return "" if owner.read_text(encoding="ascii") == digest else digest
        if not reserve:
            return ""
        base.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=base.parent, delete=False, mode="w") as f:
            f.write(digest)
            temp = Path(f.name)
        try:
            try:
                os.link(temp, owner)
            except FileExistsError:
                pass
            return "" if owner.read_text(encoding="ascii") == digest else digest
        finally:
            temp.unlink(missing_ok=True)


def _load_state() -> dict:
    path = _peer_file()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = _peer_file()
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


@contextmanager
def _transaction():
    """Serialize read-modify-replace across the gateway and its pairing CLI."""
    with _LOCK:
        path = _peer_file().with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def issue_pairing(*, member: str, advertise: str, port: int, fingerprint: str,
                  scheme: str = "wss", listener: dict | None = None) -> dict:
    """签发一次性配对：替换旧未用凭据（旧链接即刻失效），返回
    {pairing_url, pairing_token, fingerprint_short, expires_at}。

    日志只记签发事件，不记链接/凭据。"""
    with _transaction():
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
        if listener:
            state["pairing"]["listener"] = listener
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
    with _transaction():
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
        pairing.pop('activation', None)
        _save_state(state)
    logger.info("bot-group relay pairing redeemed member=%s", member)
    return True


def _valid_offer(offer, member, ticket, fingerprint, now):
    return (isinstance(ticket, str) and bool(ticket) and isinstance(fingerprint, str)
            and offer.get('member') == member and not offer.get('redeemed')
            and offer.get('expires_at', 0) > now
            and hmac.compare_digest(offer.get('token_hash', ''), hashlib.sha256(ticket.encode()).hexdigest())
            and bool(fingerprint) and hmac.compare_digest(offer.get('fingerprint', ''), fingerprint))


def verify_dial_offer(member: str, ticket: str, fingerprint: str) -> float | None:
    """Read-only check. A successful probe does not reserve or consume the ticket."""
    with _LOCK:
        offer = _load_state().get('pairing') or {}
        return offer['expires_at'] if _valid_offer(offer, member, ticket, fingerprint, time.time()) else None


def activate_dial_offer(member: str, key: str, ticket: str, fingerprint: str) -> bool:
    """Consume the original ticket and install a committed credential atomically."""
    if not isinstance(key, str) or not key:
        return False
    digest = hashlib.sha256(key.encode()).hexdigest()
    with _transaction():
        state = _load_state()
        offer = state.get('pairing') or {}
        now = time.time()
        if not _valid_offer(offer, member, ticket, fingerprint, now):
            return False
        offer.update(redeemed=True, redeemed_at=now)
        offer.pop('activation', None)
        state['persistent'] = {'member': member, 'token_hash': digest, 'paired_at': now}
        _save_state(state)
    return True


def issue_persistent(member: str) -> str:
    """配对兑换成功后签发持久身份凭据（manager node2057 ①）：重连不再
    用一次性配对票，改用持久凭据认证；吊销时 clear_persistent 才断。
    返回凭据明文（只在 hello_ok 帧下发一次，不落日志）。"""
    with _transaction():
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
    with _transaction():
        state = _load_state()
        persistent = state.get("persistent")
        if persistent is None or (member and persistent.get("member") != member):
            return
        state.pop("persistent", None)
        _save_state(state)
    logger.info("bot-group relay persistent identity cleared member=%s", member)


def listener_selected(member, *, confirmed=False):
    if not member:
        return False
    with _LOCK:
        state = _load_state()
    offer = state.get("pairing") or {}
    identity = state.get("persistent") or {}
    return (identity.get("member") == member and offer.get("member") == member
            and bool(identity.get("token_hash")) and offer.get("scheme") == "wss"
            and bool(offer.get("fingerprint"))
            and offer.get("role", "" if confirmed else "listen") == "listen")


def select_role(member, role):
    with _transaction():
        state = _load_state()
        offer = state.get("pairing") or {}
        if offer.get("member") == member and offer.get("role") != role:
            offer.pop("dial_until", None)
            state.pop("outbound", None)
            offer["role"] = role
            _save_state(state)


def request_outbound(member):
    with _transaction():
        state = _load_state()
        offer = state.get("pairing") or {}
        if offer.get("member") == member:
            state.pop("outbound", None)
            offer["dial_until"] = time.time() + 45
            _save_state(state)


def outbound_requested(member):
    with _LOCK:
        offer = _load_state().get("pairing") or {}
    return offer.get("member") == member and offer.get("dial_until", 0) > time.time()


def saved_listener(member):
    """Only a locally paired TLS endpoint may be reopened by its gateway."""
    with _LOCK:
        state = _load_state()
    offer = state.get("pairing") or {}
    if (offer.get("member") != member or offer.get("scheme") != "wss"
            or not offer.get("fingerprint")):
        raise ValueError("当前 profile 缺少已配对的 TLS 监听配置")
    return offer


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


_WORKER_STATES = {"connecting", "online", "backoff"}


def _lock_worker(handle):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def outbound_worker():
    """The OS releases the worker's lock on normal exit and on process death."""
    worker = secrets.token_hex(16)
    path = _peer_file().with_name(f"worker-{worker}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x+b") as handle:
            _lock_worker(handle)
            try:
                yield worker
            finally:
                retire_outbound(worker)
    finally:
        path.unlink(missing_ok=True)


def _worker_alive(worker):
    if not isinstance(worker, str) or len(worker) != 32 or any(c not in "0123456789abcdef" for c in worker):
        return False
    path = _peer_file().with_name(f"worker-{worker}.lock")
    try:
        with path.open("r+b") as handle:
            try:
                _lock_worker(handle)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True
                raise
    except FileNotFoundError:
        return False
    # Only a dead worker leaves an unlocked file. All observers hold the pairing transaction.
    path.unlink(missing_ok=True)
    return False


def _observed_outbound(state):
    outbound = dict(state.get("outbound") or {})
    worker = outbound.pop("worker", None)
    if outbound.get("state") in _WORKER_STATES and not _worker_alive(worker):
        outbound["state"] = "offline"
    return outbound


def retire_outbound(worker):
    with _transaction():
        state = _load_state()
        outbound = state.get("outbound") or {}
        if outbound.get("worker") == worker and outbound.get("state") in _WORKER_STATES:
            outbound.pop("worker")
            outbound["state"] = "offline"
            _save_state(state)


def outbound_state():
    with _transaction():
        return _observed_outbound(_load_state())


def set_outbound_state(status, **fields):
    with _transaction():
        state = _load_state()
        state["outbound"] = {"state": status, **fields}
        _save_state(state)


def resume_outbound():
    """Rearm the saved gateway worker, including tools running in another process."""
    with _transaction():
        state = _load_state()
        outbound = state.get("outbound") or {}
        observed = _observed_outbound(state).get("state", "offline")
        if observed in {"stopped", "offline"}:
            state["outbound"] = {field: outbound[field] for field in
                                 ("member", "engine_host", "engine_port") if field in outbound}
            state["outbound"]["state"] = "requested"
            _save_state(state)
            return "requested", True
        return observed, False


def status():
    from urllib.parse import urlsplit
    result = pairing_metadata()
    result["engines"] = []
    for peer in peer_ids():
        with using_peer(peer):
            with _transaction():
                state = _load_state()
                outbound = _observed_outbound(state)
            register = state.get("register") or {}
            listener = state.get("pairing") or {}
            member = register.get("member") or listener.get("member") or outbound.get("member")
            if not member:
                continue
            address, _ = saved_register(member)
            host = urlsplit(address)
            result["engines"].append({"member": member, "engine_host": host.hostname or outbound.get("engine_host"),
                "engine_port": host.port or outbound.get("engine_port"), "listen_port": listener.get("port"),
                "outbound": outbound,
                "role": listener.get("role") or ("dial" if address or outbound.get("engine_host") else "listen")})
    return result


def register_offer(member: str, engine_url: str, invite: str) -> tuple[str, str]:
    """Derive separate proof and credential from a local seed scoped to one invitation."""
    scope = [member, engine_url, hashlib.sha256(invite.encode()).hexdigest()]
    with _transaction():
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
    with _transaction():
        state = _load_state()
        if (state.get("register") or {}).get("credential") != credential:
            state.pop("outbound", None)
        state['register'] = {'member': member, 'credential': credential, 'saved_at': time.time()}
        if engine_url:
            state['register']['engine_url'] = engine_url.rstrip('/')
        if invite:
            scope = [member, engine_url, hashlib.sha256(invite.encode()).hexdigest()]
            state['register']['scope'] = scope
            if (state.get('register_offer') or {}).get('scope') == scope:
                state.pop('register_offer')
        _save_state(state)
    logger.info("bot-group relay register credential saved member=%s", member)


def saved_register(member: str) -> tuple[str, str]:
    """Read the confirmed peer for this profile, including older scoped records."""
    with _LOCK:
        saved = _load_state().get('register') or {}
    if not member or saved.get('member') != member:
        return '', ''
    scope = saved.get('scope')
    url = saved.get('engine_url') or (scope[1] if isinstance(scope, list) and len(scope) == 3 else '')
    return url.rstrip('/'), saved.get('credential') or ''


def load_register_credential(member: str, *, engine_url: str = '', invite: str = '') -> str:
    """A new invitation cannot silently reuse credentials for an earlier one."""
    if not invite:
        url, credential = saved_register(member)
        return credential if not engine_url or url == engine_url.rstrip('/') else ''
    with _LOCK:
        saved = _load_state().get("register") or {}
        if saved.get("member") == member:
            scope = [member, engine_url, hashlib.sha256(invite.encode()).hexdigest()]
            if saved.get('scope') == scope:
                return saved.get("credential") or ""
    return ""


def write_link_file(pairing_url: str, *, path: str | None = None) -> str:
    """非交互终端输出：链接写 0600 文件（POSIX），不落 stdout。
    manager node2060 ①：从创建时即限权（os.open O_CREAT 0600，非事后
    chmod——创建瞬间无宽权限窗口）；写失败如实抛 OSError（不吞）；
    写临时文件后原子 rename。path=None 时写到插件 state/pairing-link.txt。"""
    target = Path(path) if path else _peer_file().parent / "pairing-link.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
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
