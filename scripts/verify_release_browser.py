"""#215：分配浏览器目录与调试端口，访问真实隔离服务，免密创建话题并刷新/重开。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True)
    p.add_argument('--profile', required=True)
    p.add_argument('--debug-port', required=True, type=int)
    p.add_argument('--report-dir', required=True)
    args = p.parse_args()
    target = urlsplit(args.url)
    if target.hostname != '127.0.0.1' or target.port != 18042 or args.debug_port != 18043:
        p.error('本工作区验证仅允许 127.0.0.1:18042，浏览器端口 18043')
    report = Path(args.report_dir)
    report.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            args.profile, channel='chrome', headless=True,
            args=[f'--remote-debugging-port={args.debug_port}', '--remote-debugging-address=127.0.0.1'],
            viewport={'width': 1280, 'height': 900})
        try:
            page = browser.new_page()
            errors, status_requests, login_requests = [], [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda r: status_requests.append(r.url) if '/auth/status' in r.url else None)
            page.on('request', lambda r: login_requests.append(r.url) if '/auth/login' in r.url else None)
            page.goto(args.url + '/app/')
            page.locator('#layout').wait_for(state='visible')
            assert page.evaluate('S.isUserIdentity') is True
            assert len(status_requests) == 1 and not login_requests
            # 初始项目为唯一已登记的隔离项目。
            page.wait_for_function("S.currentProject === 'release215'")
            page.locator('#btn-new-thread').click()
            page.locator('#dlg-title').fill('隔离发布验收话题')
            page.locator('#dlg-opening').fill('请为隔离发布验收创建一项任务，完成开发审查验收流程。')
            with page.expect_response(lambda r: r.url.endswith('/thread') and r.request.method == 'POST') as pending:
                page.locator('#thread-form [type=submit]').click()
            response = pending.value
            assert response.ok, response.text()
            thread = response.json()
            assert thread['wake'] == [], thread
            page.locator('#dlg-thread').wait_for(state='hidden')
            page.get_by_text('隔离发布验收话题', exact=True).first.wait_for(state='visible')
            page.reload()
            page.locator('#layout').wait_for(state='visible')
            page.get_by_text('隔离发布验收话题', exact=True).first.wait_for(state='visible')
            assert page.evaluate('S.isUserIdentity') is True
            page.screenshot(path=str(report / 'password-free-refresh.png'))
            page.close()
            reopened = browser.new_page()
            reopened.goto(args.url + '/app/')
            reopened.locator('#layout').wait_for(state='visible')
            reopened.get_by_text('隔离发布验收话题', exact=True).first.wait_for(state='visible')
            assert reopened.evaluate('S.isUserIdentity') is True
            assert not errors, errors
            # 核验本次浏览器的实际 CDP 端口可用。
            import urllib.request
            with urllib.request.urlopen(f'http://127.0.0.1:{args.debug_port}/json/version') as r:
                assert 'webSocketDebuggerUrl' in json.load(r)
            command = browser.new_cdp_session(reopened).send('Browser.getBrowserCommandLine')['arguments']
            actual_profile = next(a.split('=', 1)[1] for a in command if a.startswith('--user-data-dir='))
            assert Path(actual_profile).resolve() == Path(args.profile).resolve()
            listeners = []
            if os.name == 'nt':
                raw = subprocess.check_output(['powershell', '-NoProfile', '-Command',
                    'Get-NetTCPConnection -State Listen -LocalPort 18042,18043 | '
                    'Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress'])
                listeners = json.loads(raw)
                if isinstance(listeners, dict):
                    listeners = [listeners]
                assert {r['LocalPort'] for r in listeners} == {18042, 18043}, listeners
                assert all(r['LocalAddress'] == '127.0.0.1' for r in listeners), listeners
            (report / 'browser.json').write_text(json.dumps({
                'thread_id': thread['thread_id'], 'login_submissions': len(login_requests),
                'auth_probes_first_page_and_refresh': len(status_requests), 'page_errors': errors,
                'debug_host': '127.0.0.1', 'debug_port': args.debug_port,
                'user_data_dir': str(Path(args.profile).resolve()),
                'actual_cdp_user_data_dir': actual_profile, 'actual_listeners': listeners,
                'refresh': 'passed', 'reopen': 'passed'}, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
