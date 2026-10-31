"""Run unit regressions with temporary defaults and fail-closed I/O boundaries."""
import argparse
import json
import logging
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('tests', nargs='+')
    args = parser.parse_args()
    report = args.report.resolve()
    if not args.report.is_absolute():
        parser.error('--report must be an absolute path')
    repo = Path(__file__).resolve().parents[1]
    if not report.is_relative_to(repo / 'reports' / 'tests'):
        parser.error('--report must be under this project reports/tests directory')
    checked = subprocess.run(['git', 'check-ignore', str(report / 'isolation.json')],
                             cwd=repo, capture_output=True)
    if checked.returncode:
        parser.error('report directory must be ignored by Git')
    report.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='unit-runtime-', dir=report) as directory:
        root = Path(directory).resolve()
        for name in list(os.environ):
            if name.startswith(('GROUP_', 'XDG_GROUP_', 'HERMES_', 'CODEX_', 'OPENAI_', 'ANTHROPIC_')):
                os.environ.pop(name)
        defaults = {
            'GROUP_DB': str(root / 'group.db'), 'GROUP_DATA_DIR': str(root / 'data'),
            'DATA_DIR': str(root / 'data'), 'GROUP_ATTACHMENTS_DIR': str(root / 'attachments'),
            'HERMES_HOME': str(root / 'hermes'), 'CODEX_HOME': str(root / 'codex'),
            'GROUP_ENGINE_URL': 'http://127.0.0.1:1', 'GROUP_ENGINE_KEY': 'isolated-unit-key',
            'GROUP_TEST_SANDBOX': str(root), 'GROUP_TEST_REPORT': str(report),
            'TEMP': str(root), 'TMP': str(root), 'TMPDIR': str(root), 'PYTHONDONTWRITEBYTECODE': '1',
        }
        os.environ.update(defaults)
        tempfile.tempdir = str(root)
        bound = []
        blocked = []
        allocated = set()
        delegated = {}
        children = []

        def within(value, *, evidence=False):
            target = Path(value).resolve()
            return target.is_relative_to(root) or (evidence and target.is_relative_to(report))

        def deny(kind, target):
            blocked.append({'kind': kind, 'target': str(target)})
            raise RuntimeError(f'Isolated test blocked {kind}: {target}')

        def audit(event, values):
            if event == 'sqlite3.connect':
                target = os.fsdecode(values[0])
                if target == ':memory:':
                    return
                if target.startswith('file:'):
                    target = unquote(urlparse(target).path).lstrip('/') if os.name == 'nt' else unquote(urlparse(target).path)
                if not target or not within(target):
                    deny('database outside sandbox', target)
            elif event == 'open':
                target, mode, flags = values
                writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                if writing and isinstance(target, (str, bytes, os.PathLike)) and not within(os.fsdecode(target), evidence=True):
                    if os.fsdecode(target) != os.devnull:
                        deny('file write outside sandbox', target)
            elif event == 'socket.bind':
                address = values[1]
                if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1') or address[1] != 0:
                    deny('non-ephemeral loopback listener', address)
                bound.append(values[0])
            elif event == 'socket.connect':
                address = values[1]
                if not isinstance(address, tuple):
                    deny('unowned socket', address)
                endpoints = set()
                for sock in bound:
                    try:
                        local = sock.getsockname()
                        endpoints.add((local[0], local[1]))
                    except OSError:
                        pass
                endpoint = (address[0], address[1])
                if endpoint not in endpoints:
                    child = delegated.get(endpoint)
                    if child is None:
                        deny('unowned endpoint', address)
                    import psutil
                    if child.poll() is not None:
                        if any(item.status == psutil.CONN_LISTEN and tuple(item.laddr) == endpoint
                               for item in psutil.net_connections(kind='tcp')):
                            deny('foreign listener at released test endpoint', address)
                        raise ConnectionRefusedError('Owned test child and listener have exited')
                    owner = psutil.Process(child.pid)
                    processes = [owner, *owner.children(recursive=True)]
                    listening = []
                    for process in processes:
                        try:
                            if process.environ().get('GROUP_DB') == child._isolated_database:
                                listening.extend(process.net_connections(kind='tcp'))
                        except psutil.NoSuchProcess:
                            continue
                    if not any(item.status == psutil.CONN_LISTEN and tuple(item.laddr) == endpoint
                               for item in listening):
                        raise ConnectionRefusedError('Owned test child is not listening yet')
            elif event == 'subprocess.Popen':
                env = values[3] if values[3] is not None else os.environ
                for name in ('GROUP_DB', 'GROUP_DATA_DIR', 'HERMES_HOME', 'CODEX_HOME'):
                    if not env.get(name) or not within(env[name]):
                        deny('child runtime outside sandbox', name)

        sys.addaudithook(audit)
        # Explicit child environments also receive safe filesystem defaults.
        original_bind = socket.socket.bind

        def bind(sock, address):
            result = original_bind(sock, address)
            actual = sock.getsockname()
            allocated.add((actual[0], actual[1]))
            return result

        socket.socket.bind = bind
        original_popen = subprocess.Popen

        class IsolatedPopen(original_popen):
            def __init__(self, *positional, **kwargs):
                if kwargs.get('env') is not None:
                    kwargs['env'] = {**defaults, **kwargs['env']}
                effective = kwargs.get('env') or os.environ
                command = positional[0] if positional else kwargs.get('args', [])
                engine_child = isinstance(command, (list, tuple)) and 'engine.main' in command
                port = effective.get('GROUP_ENGINE_PORT') if engine_child else None
                endpoint = ('127.0.0.1', int(port)) if port else None
                if endpoint and endpoint not in allocated:
                    deny('unallocated child listener', endpoint)
                super().__init__(*positional, **kwargs)
                self._isolated_database = effective['GROUP_DB']
                children.append(self)
                if endpoint:
                    delegated[endpoint] = self

        subprocess.Popen = IsolatedPopen
        from scripts.runtime_test_support import isolated_runtime
        errors = []

        class BackgroundErrors(logging.Handler):
            def emit(self, record):
                if record.name == 'engine.hermes_connections' and record.levelno >= logging.ERROR:
                    errors.append(record.getMessage())

        class OwnedResult(unittest.TextTestResult):
            def stopTest(self, test):
                for child in children:
                    if child.poll() is None:
                        errors.append(f'Test child leaked: {child.pid}')
                        import psutil
                        try:
                            descendants = psutil.Process(child.pid).children(recursive=True)
                        except psutil.NoSuchProcess:
                            descendants = []
                        for descendant in descendants:
                            try:
                                if descendant.environ().get('GROUP_DB') == child._isolated_database:
                                    descendant.terminate()
                            except psutil.NoSuchProcess:
                                pass
                        child.terminate()
                        try:
                            child.wait(5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(5)
                super().stopTest(test)

        logging.getLogger().addHandler(BackgroundErrors())
        with isolated_runtime(root):
            suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests)
            result = unittest.TextTestRunner(verbosity=2, resultclass=OwnedResult).run(suite)
        remaining = [t.name for t in threading.enumerate() if t.name.startswith('hermes-')]
        passed = result.wasSuccessful() and not errors and not remaining
        (report / 'isolation.json').write_text(json.dumps({
            'selected_tests': args.tests, 'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'background_errors': errors, 'remaining_workers': remaining,
            'blocked_io': blocked, 'passed': passed,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
