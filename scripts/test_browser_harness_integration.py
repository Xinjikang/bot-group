"""Real harness/Chromium check. Set BROWSER_HARNESS_PYTHON to its installed Python."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import browser_resources as br
from scripts.test_browser_resources import scope
from scripts.unified_test_support import isolated_database


@unittest.skipUnless(os.environ.get('BROWSER_HARNESS_PYTHON'), 'set BROWSER_HARNESS_PYTHON for real harness')
class RealHarnessTests(unittest.TestCase):
    def test_dedicated_tab_and_directory_release_preserve_shared_chrome(self):
        from playwright.sync_api import sync_playwright
        with isolated_database(), sync_playwright() as pw:
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1', 0))
                port = port_socket.getsockname()[1]
            browser = pw.chromium.launch(headless=True, args=[f'--remote-debugging-port={port}'])
            try:
                page = browser.new_page()
                page.goto('about:blank')
                cdp = browser.new_browser_cdp_session()
                def pages():
                    return {p['targetId'] for p in cdp.send('Target.getTargets')['targetInfos'] if p['type'] == 'page'}
                baseline = pages()
                env, _, _ = scope(task_id=1)
                root = Path(env['BH_RUNTIME_DIR'])
                workdir = root / 'workdir'
                workdir.mkdir()
                child_env = {**os.environ, **env, 'BH_HOME': str(root / 'home'),
                             'BU_CDP_URL': f'http://127.0.0.1:{port}', 'BU_AUTOSPAWN': '0',
                             'NO_PROXY': '127.0.0.1,localhost', 'PYTHONDONTWRITEBYTECODE': '1'}
                for key in ('BU_CDP_WS', 'BU_BROWSER_ID', 'PYTHONHOME', 'PYTHONPATH'):
                    child_env.pop(key, None)
                with (root / 'harness-output.log').open('wb') as output:
                    proc = subprocess.Popen([os.environ['BROWSER_HARNESS_PYTHON'], '-B', '-m', 'browser_harness.daemon'],
                        cwd=workdir, env=child_env, stdout=output, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    try:
                        endpoint = root / ('bu-' + env['BU_NAME'] + ('.port' if os.name == 'nt' else '.sock'))
                        deadline = time.monotonic() + 20
                        while not endpoint.exists():
                            if proc.poll() is not None or time.monotonic() > deadline:
                                self.fail((root / 'harness-output.log').read_text(errors='replace'))
                            time.sleep(0.05)
                        self.assertEqual(len(pages() - baseline), 1)
                        result = br.release(project='alpha', task_id=1)
                        self.assertTrue(result['ok'], result)
                        self.assertEqual(proc.wait(timeout=10), 0)
                        self.assertFalse(endpoint.exists())
                        self.assertTrue(browser.is_connected())
                        self.assertEqual(pages(), baseline)
                        # Removing this empty test-only cwd proves the launcher also
                        # released its Windows directory handle.
                        workdir.rmdir()
                    finally:
                        if proc.poll() is None:
                            br.release(project='alpha')
                            proc.wait(timeout=10)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
