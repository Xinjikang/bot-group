"""Real creation wizard with isolated HUD APIs and no model execution."""
import argparse
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_hud import HudTests

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not args.evidence.is_absolute():
        parser.error('--evidence must be an absolute path')
    args.evidence.mkdir(parents=True, exist_ok=True)
    fixture = HudTests()
    fixture.setUp()
    try:
        configured = fixture.session('p')
        bot = dict(fixture.conn.execute('SELECT * FROM bots WHERE id=?', (configured['bot_id'],)).fetchone())
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            try:
                for project in ('p', 'q'):
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors, drafts = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def route(request):
                        req = request.request
                        path = urlparse(req.url).path
                        if path.startswith('/app/'):
                            file = ROOT / 'frontend' / (path[5:] or 'index.html')
                            request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                        elif path.startswith('/hud'):
                            response = fixture.client.request(req.method, path,
                                json=req.post_data_json if req.method in ('POST', 'PATCH') else None)
                            assert response.status_code == 200, response.text
                            data = response.json()
                            if path == '/hud' and req.method == 'POST' and req.post_data_json.get('source_project'):
                                drafts.append(data)
                            request.fulfill(json=data)
                        elif path == '/profiles/candidates':
                            request.fulfill(json={'candidates': [{**bot, 'bot_id': bot['id']}]})
                        else:
                            request.fulfill(json={'projects': [], 'members': [], 'threads': [],
                                'messages': [], 'tasks': [], 'requirements': []})

                    page.route('**/*', route)
                    page.goto('http://hud-fixture.test/app/index.html')
                    page.evaluate("project => {S.currentProject=project;showLogin(false);openProjectInitialization();}", project)
                    page.wait_for_function("document.querySelector('#hud-context').textContent.includes('项目目标')")
                    assert drafts and drafts[-1]['project'] is None
                    if project == 'p':
                        assert drafts[-1]['bot_id'] == configured['bot_id']
                        assert page.locator('.hud-name').inner_text() == bot['display_name']
                        assert '为 HUD 选择一个 AI' not in page.locator('#hud-content').inner_text()
                    else:
                        assert drafts[-1]['bot_id'] is None
                        page.get_by_role('button', name='选择 AI', exact=True).click()
                        page.wait_for_selector('#hud-settings select')
                        assert page.get_by_label('维护 AI', exact=True).input_value() == ''
                        assert page.locator('#hud-settings input[type=checkbox]').count() == 0
                        page.get_by_label('维护 AI', exact=True).select_option('0')
                        page.get_by_role('button', name='保存并开始维护', exact=True).click()
                        page.wait_for_function("document.querySelector('.hud-name').textContent === '知夏'")
                        saved = fixture.client.get('/hud/' + drafts[-1]['id']).json()
                        assert saved['bot_id'] == configured['bot_id']
                    for width in (1440, 390):
                        page.set_viewport_size({'width': width, 'height': 1000})
                        page.screenshot(path=str(args.evidence / f'hud-{project}-{width}.png'))
                    assert not errors, errors
                    page.close()
            finally:
                browser.close()
        (args.evidence / 'result.json').write_text(json.dumps({'passed': True,
            'configured_project_reused': True, 'unconfigured_project_requires_save': True,
            'global_prefill_removed': True, 'viewports': [1440, 390]}), encoding='utf-8')
        print('PASS: configured project assistant reused; unconfigured project requires selection and save; global prefill removed')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
