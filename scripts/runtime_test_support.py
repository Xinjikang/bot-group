"""Owned engine paths and connection pools for in-process runtime tests."""
from contextlib import ExitStack, contextmanager
import hashlib
import os
import socket
import sys
from pathlib import Path
from unittest.mock import patch


@contextmanager
def isolated_runtime(directory):
    from engine import config, db, hermes_connections as connections, signing_keys, workspace_resources
    from engine.hermes_gateway_client import GatewayUnavailable

    root = Path(directory).resolve()
    pools = []
    reservations = []
    ports = set()
    connect = getattr(db.get_connection, '_test_base', db.get_connection)

    def owned(path):
        target = Path(path).resolve()
        if not target.is_relative_to(root):
            raise AssertionError(f'Test resource escaped its runtime: {target}')
        return target

    def database():
        owned(config.db_path())
        return connect()

    database._test_base = connect

    def offline(home, command, callback, **options):
        owned(home)
        raise GatewayUnavailable('isolated_test_gateway_offline')

    def workspace_defaults(project_root, slot, used):
        def allocate():
            for _ in range(128):
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                reservations.append(sock)
                port = sock.getsockname()[1]
                if port not in used:
                    used.add(port)
                    ports.add(port)
                    return port
            raise RuntimeError('Cannot allocate independent test ports')
        identity = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]
        base = root / 'workspace-resources' / identity / slot
        return {'services': {name: {'host': '127.0.0.1', 'port': allocate()}
                             for name in ('frontend', 'backend')},
                'browser': {'host': '127.0.0.1', 'port': allocate(),
                            'data_directory': str(base / 'browser')},
                'test_data_directory': str(base / 'test-data')}

    def new_pool():
        pool = connections.Pool(opener=offline, revision=lambda: ('test', 'isolated'),
                                pinned=lambda home: False)
        pools.append(pool)
        return pool

    def resume():
        if connections._POOL.stopped:
            connections._POOL = new_pool()

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            'GROUP_DB': str(root / 'group.db'), 'GROUP_DATA_DIR': str(root / 'data'),
            'DATA_DIR': str(root / 'data'), 'GROUP_ATTACHMENTS_DIR': str(root / 'attachments'),
            'HERMES_HOME': str(root / 'hermes'), 'CODEX_HOME': str(root / 'codex'),
            'GROUP_ENGINE_KEY': 'isolated-runtime-test-key',
            'GROUP_ENGINE_URL': 'http://127.0.0.1:1',
            'XDG_GROUP_ENGINE_URL': 'http://127.0.0.1:1',
            'GROUP_ENGINE_TOKEN': 'isolated-runtime-test-token',
        }))
        stack.enter_context(patch.dict(config._cli, {'data_dir': None}))
        stack.enter_context(patch.object(signing_keys, '_active_path', None))
        stack.enter_context(patch.object(signing_keys, '_trusted_keys', {}))
        stack.enter_context(patch.object(db, 'get_connection', database))
        stack.enter_context(patch.object(connections, '_POOL', new_pool()))
        stack.enter_context(patch.object(connections, 'resume', side_effect=resume))
        stack.enter_context(patch.object(workspace_resources, 'defaults', workspace_defaults))
        stack.enter_context(patch.object(workspace_resources, 'available', side_effect=lambda port: port in ports))
        try:
            yield root
        finally:
            # Keep guards and paths installed until every owned worker has exited.
            for pool in pools:
                pool.stop()
            try:
                for pool in pools:
                    pool.join()
            except BaseException as exc:
                # A live daemon cannot outlast restoration of process-wide guards.
                print(f'Unsafe test teardown: {exc}', file=sys.stderr, flush=True)
                os._exit(70)
            for pool in pools:
                for entry in pool.entries.values():
                    if entry.client is not None:
                        entry.client.close()
            for sock in reservations:
                sock.close()
