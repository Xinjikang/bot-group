"""Edit the paired gateway's own profile using the bundled Python environment."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys


_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='relay-profile')


def respond(ws, meta, spec, home, interpreter, *, secure):
    from . import mcp_digest

    def run():
        try:
            if not isinstance(spec, dict) or spec.get('action') not in ('read', 'save'):
                raise ValueError('invalid profile operation')
            python = mcp_digest.mcp_python(interpreter)
            if not python:
                raise FileNotFoundError('relay venv')
            # The engine bounds the receipt wait. Let a started write finish its commit or rollback.
            timeout = 20 if spec.get('action') == 'read' else None
            proc = subprocess.run([python, '-I', '-X', 'utf8', __file__, home],
                                  input=json.dumps([spec, secure]), encoding='utf-8',
                                  capture_output=True, timeout=timeout, check=True)
            result = json.loads(proc.stdout)
        except subprocess.TimeoutExpired:
            result = {'failure': '远端操作超时，请重新打开编辑窗口核对配置', 'status': 504}
        except (ValueError, OSError, subprocess.SubprocessError):
            result = {'failure': '远端配置进程未完成，请检查 relay 插件安装并重新打开编辑窗口', 'status': 502}
        try:
            ws.send(json.dumps({'relay': {**meta, 'kind': 'profile_result'}, 'profile': result}))
        except (OSError, RuntimeError, EOFError, ConnectionError):
            pass

    _workers.submit(run)


def edit(folder, spec, secure):
    from fastapi import HTTPException
    from engine import profile_config, relay_protocol

    if spec['action'] == 'read':
        values = profile_config.read_folder(folder)
        if not secure or spec.get('include_env') is not True:
            values.pop('env')
        return {'fields': relay_protocol.profile_fields(values)}
    if spec['action'] != 'save':
        raise ValueError('action')
    values = relay_protocol.profile_fields(spec['fields'], decode=True)
    if 'env' in values and not secure:
        raise HTTPException(409, '编辑 .env 需要加密的 WSS 连接')
    return profile_config.save_folder(folder, values)


@contextmanager
def profile_lock(home):
    """Serialize independent listener and gateway workers for the same profile."""
    if os.name == 'nt':
        import msvcrt
        lock = home / '.group-engine-profile.lock'
        if lock.is_symlink() or lock.resolve() != lock:
            raise ValueError('lock path')
        with lock.open('a+b') as stream:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            yield
        return
    import fcntl
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main():
    # -I isolates imports from the gateway's environment and current directory.
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root if (root / 'engine').is_dir() else root.parents[2]))
    from fastapi import HTTPException
    home = Path(sys.argv[1])
    try:
        if not home.is_absolute() or not home.is_dir():
            raise HTTPException(404, '远端 Hermes profile 目录不存在')
        spec, secure = json.load(sys.stdin)
        home = home.resolve()
        with profile_lock(home):
            result = edit(home, spec, secure)
    except HTTPException as exc:
        result = {'failure': exc.detail, 'status': exc.status_code}
    except (ValueError, TypeError, KeyError, OSError):
        result = {'failure': '远端配置请求或目录无效', 'status': 400}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
