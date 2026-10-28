"""Relocate the default database after writers have stopped, with recovery evidence."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import uuid

from engine import config


def _paths():
    root = config.program_root().resolve()
    return root / 'group.db', root / 'data' / 'group.db', root / 'data' / 'database-layout.json'


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _lock(path):
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _assert_stopped():
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(config.host(), config.host())
    try:
        with socket.create_connection((host, config.port()), timeout=5):
            raise RuntimeError('数据库目录迁移需要先停止引擎')
    except ConnectionRefusedError:
        return


def _legacy_upgrade_parent():
    """Older GUI helpers require the original database identity until they exit."""
    try:
        receipt = json.loads((config.logs_dir() / 'app-upgrade.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    parent = receipt.get('helper_pid')
    legacy = (receipt.get('state') == 'running'
            and (parent == os.getppid() or str(parent) == os.environ.get('GROUP_LEGACY_LAYOUT_HELPER'))
            and receipt.get('program_root') == str(config.program_root().resolve())
            and receipt.get('database_layout_version', 0) < 1)
    if legacy:
        os.environ['GROUP_LEGACY_LAYOUT_HELPER'] = str(parent)
    return legacy


def _read_state(path):
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(state, dict)
            or state.get('state') not in ('prepared', 'complete')
            or not all(isinstance(state.get(key), str) for key in ('id', 'source', 'target', 'backup'))):
        raise RuntimeError('数据库迁移记录格式损坏，已保留现场')
    source, target, _ = _paths()
    backup = Path(state['backup'])
    if (state.get('source') != str(source) or state.get('target') != str(target)
            or backup.parent != target.parent / 'migration-backups'
            or backup.name != state['id'] + '.db'):
        raise RuntimeError('数据库迁移记录与当前安装不匹配')
    return state


def relocated_path(default):
    source, target, receipt = _paths()
    state = _read_state(receipt)
    if state:
        if state['state'] != 'complete':
            raise RuntimeError('数据库目录迁移尚未完成，请通过标准启动入口恢复')
        if not target.is_file() or source.exists():
            raise RuntimeError('数据库迁移路径存在冲突')
        return target
    return default


def _restore_pending(state, receipt):
    source, target, _ = _paths()
    backup = Path(state['backup'])
    stage = target.with_name(state['id'] + '.pending')
    if state['state'] == 'complete':
        return
    if target.exists():
        if source.exists() or _sha(target) != state.get('target_sha256'):
            raise RuntimeError('数据库迁移中断后发现冲突，已保留两份数据')
        state['state'] = 'complete'
        _write(receipt, state)
        return
    if not source.exists():
        if not backup.is_file():
            raise RuntimeError('数据库迁移恢复缺少原库备份')
        os.replace(backup, source)
    stage.unlink(missing_ok=True)
    receipt.unlink()


def migrate(*, upgrade_backup=None):
    """Migrate only the standard root database; explicit custom locations stay intact."""
    source, target, receipt = _paths()
    if config.data_dir().resolve() != source.parent:
        return None
    if _legacy_upgrade_parent():
        return None
    explicit = config._pick(None, ('GROUP_DB',), '').strip()
    configured = Path(explicit).resolve() if explicit else (source if source.exists() else target)
    if configured not in (source, target):
        return None
    if target.parent.exists() and target.parent.resolve() != target.parent:
        raise RuntimeError('data 目录指向其他位置，请使用显式 GROUP_DB 配置')
    if not source.exists() and not receipt.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock(target.parent / '.database-layout.lock'):
        state = _read_state(receipt)
        if state and state['state'] != 'complete':
            _assert_stopped()
            _restore_pending(state, receipt)
            state = _read_state(receipt)
        if state and state['state'] == 'complete':
            if not target.is_file() or source.exists():
                raise RuntimeError('已迁移数据库与根目录旧库发生冲突')
            if configured == source:
                os.environ['GROUP_DB'] = str(target)
            return state
        if not source.exists():
            return None
        if target.exists():
            raise RuntimeError('根目录和 data/ 同时存在数据库，已保留现场并中止迁移')
        _assert_stopped()
        migration_id = uuid.uuid4().hex
        backup = target.parent / 'migration-backups' / (migration_id + '.db')
        backup.parent.mkdir(exist_ok=True)
        stage = target.with_name(migration_id + '.pending')
        state = {'id': migration_id, 'state': 'prepared', 'source': str(source),
                 'target': str(target), 'backup': str(backup),
                 'upgrade_backup': str(Path(upgrade_backup).resolve()) if upgrade_backup else None}
        try:
            with closing(sqlite3.connect(source, timeout=2)) as src:
                checkpoint = src.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0] != 0:
                    raise RuntimeError('数据库仍被占用，迁移已中止')
                if src.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise RuntimeError('源数据库完整性检查失败')
                before = src.execute('PRAGMA data_version').fetchone()[0]
                with closing(sqlite3.connect(stage)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise RuntimeError('目标数据库完整性检查失败')
                if src.execute('PRAGMA data_version').fetchone()[0] != before:
                    raise RuntimeError('迁移期间有其他数据库写入，迁移已中止')
            if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                raise RuntimeError('数据库仍有未退出的连接，迁移已中止')
            state['target_sha256'] = _sha(stage)
            _write(receipt, state)
            os.replace(source, backup)
            os.replace(stage, target)
            state['state'] = 'complete'
            _write(receipt, state)
        except BaseException:
            if receipt.exists():
                _restore_pending(_read_state(receipt), receipt)
            else:
                stage.unlink(missing_ok=True)
            raise
        if configured == source and os.environ.get('GROUP_DB'):
            os.environ['GROUP_DB'] = str(target)
        return state


def rollback(upgrade_backup):
    """Restore the original path only for the upgrade that performed the relocation."""
    source, target, receipt = _paths()
    state = _read_state(receipt)
    if not state or state.get('upgrade_backup') != str(Path(upgrade_backup).resolve()):
        return
    _assert_stopped()
    with _lock(target.parent / '.database-layout.lock'):
        if source.exists():
            raise RuntimeError('回滚路径已存在数据库，已保留现场')
        backup = Path(state['backup'])
        if not backup.is_file():
            raise RuntimeError('回滚缺少原数据库')
        if target.exists():
            with closing(sqlite3.connect(target, timeout=2)) as db:
                if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise RuntimeError('新数据库仍被占用，回滚已中止')
            if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                raise RuntimeError('新数据库仍有未退出的连接，回滚已中止')
            os.replace(target, backup.with_suffix('.failed.db'))
        os.replace(backup, source)
        receipt.unlink()
        if os.environ.get('GROUP_DB') == str(target):
            os.environ['GROUP_DB'] = str(source)
