"""Chromium + isolated API: logical removal, hidden records and reimport."""
import mimetypes
from pathlib import Path
import sys
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_import import ImportTests, fingerprints
ROOT = Path(__file__).resolve().parents[1]


def main():
    fixture = ImportTests()
    fixture.setUp()
    try:
        old = fixture.submit().json()['project']
        before = fingerprints(fixture.root)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route(request):
                req = request.request
                path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                elif path.startswith('/projects'):
                    response = fixture.client.request(req.method, req.url, json=req.post_data_json if req.post_data else None)
                    request.fulfill(status=response.status_code, json=response.json())
                else:
                    request.fulfill(json={'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'members': []})
            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            page.evaluate('showLogin(false); S.members=[]; loadProjects()')
            page.evaluate('(id) => openProjectDetail(id)', old)
            page.locator('#pj-detail-remove').click()
            page.locator('#app-alert').get_by_role('button', name='取消', exact=True).click()
            assert fixture.conn.execute('SELECT removed FROM projects WHERE name=?', (old,)).fetchone()[0] == 0
            page.locator('#pj-detail-remove').click()
            page.locator('#app-alert .primary').click()
            page.locator('#dlg-project-detail').wait_for(state='hidden')
            assert fixture.conn.execute('SELECT removed FROM projects WHERE name=?', (old,)).fetchone()[0] == 1
            page.locator('#chip-all').click()
            assert page.locator('#pj-drop-removed').count() == 0
            assert page.locator('#dlg-removed-projects').count() == 0
            assert page.locator('.pj-drop-row').count() == 0
            new = fixture.submit().json()['project']
            assert old != new
            page.evaluate('loadProjects(); closeProjectDrop()')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#chip-all').click()
            page.locator('.pj-drop-row').wait_for()
            assert page.locator('.pj-drop-row').count() == 1
            page.locator('.pj-drop-row').get_by_role('button', name='移除', exact=True).click()
            page.locator('#app-alert').get_by_role('button', name='取消', exact=True).click()
            assert fixture.conn.execute('SELECT removed FROM projects WHERE name=?', (new,)).fetchone()[0] == 0
            page.locator('.pj-drop-row').get_by_role('button', name='移除', exact=True).click()
            page.locator('#app-alert .primary').click()
            page.wait_for_function("document.querySelectorAll('.pj-drop-row').length === 0")
            assert fixture.conn.execute('SELECT removed FROM projects WHERE name=?', (new,)).fetchone()[0] == 1
            page.locator('#pj-drop-toggle-archived').click()
            page.wait_for_function("document.querySelector('#pj-drop-list').textContent.includes('暂无归档项目')")
            archived = fixture.submit().json()['project']
            assert fixture.client.post(f'/projects/{archived}/archive', json={'archived': True}).status_code == 200
            page.evaluate('renderProjectDropList(true)')
            page.locator('.pj-drop-row').get_by_role('button', name='移除', exact=True).click()
            page.locator('#app-alert .primary').click()
            page.wait_for_function("document.querySelectorAll('.pj-drop-row').length === 0")
            assert fixture.conn.execute('SELECT removed FROM projects WHERE name=?', (archived,)).fetchone()[0] == 1
            assert fingerprints(fixture.root) == before
            assert not errors, errors
            browser.close()
        print('PASS: cancel/remove, removed records hidden, reimport, 390px, file preservation')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
