"""Real Chrome, isolated API: rapid submits and retry after a lost response."""
import mimetypes
import io
import os
from unittest.mock import patch
from PIL import Image
from pathlib import Path
import sys
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_thread_creation import ThreadCreationTests

ROOT = Path(__file__).resolve().parents[1]


def main():
    fixture = ThreadCreationTests()
    fixture.setUp()
    fixture.prepare()
    env = patch.dict(os.environ, {'GROUP_ATTACHMENTS_DIR': str(fixture.base / 'images')})
    env.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            pending, bodies, errors = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route(request):
                req = request.request
                path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                elif path == '/attachments' and req.method == 'POST':
                    response = fixture.client.post(req.url.replace('http://workspace.test', ''), content=req.post_data_buffer)
                    request.fulfill(status=response.status_code, json=response.json())
                elif path == '/thread' and req.method == 'POST':
                    bodies.append(req.post_data_json)
                    response = fixture.client.post('/thread', json=req.post_data_json)
                    assert response.status_code == 200, response.text
                    if len(bodies) == 1:
                        pending.append(request)  # commit succeeded, response has not reached browser
                    else:
                        request.fulfill(json=response.json())
                else:
                    request.fulfill(json={'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'members': [], 'projects': []})

            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            page.evaluate('(project) => { showLogin(false); S.currentProject=project; openDialogAnchored(document.querySelector("#dlg-thread"), null); }', fixture.topic['project'])
            page.evaluate('openDialogAnchored(document.querySelector("#dlg-thread-members"), null)')
            page.locator('body').click(position={'x': 2, 'y': 2})
            assert not page.locator('#dlg-thread-members').is_visible()
            assert page.locator('#dlg-thread').is_visible()
            page.locator('#dlg-title').fill('Topic')
            page.locator('#dlg-opening').fill('Hello')
            page.locator('body').click(position={'x': 2, 'y': 2})
            assert page.locator('#dlg-thread').is_visible()
            assert page.locator('#dlg-opening').input_value() == 'Hello'
            image = io.BytesIO()
            Image.new('RGB', (8, 8), 'red').save(image, format='PNG')
            page.locator('#thread-opening-editor input[type=file]').set_input_files({'name': 'opening.png', 'mimeType': 'image/png', 'buffer': image.getvalue()})
            assert page.locator('#thread-opening-editor .image-drafts img').count() == 1
            page.evaluate('''() => {
                const form = document.querySelector('#thread-form');
                form.requestSubmit();
                form.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
            }''')
            page.wait_for_function('threadSubmitting')
            assert page.locator('#thread-form [type=submit]').is_disabled()
            page.keyboard.press('Escape')
            assert page.locator('#dlg-thread').is_visible()
            assert len(bodies) == 1
            pending[0].fulfill(status=500, json={'detail': 'simulated lost response'})
            page.wait_for_function('!threadSubmitting')
            page.locator('#app-alert .primary').click()
            assert page.locator('#dlg-opening').input_value() == 'Hello'
            assert page.locator('#thread-opening-editor .image-drafts img').count() == 1
            page.locator('#thread-form [type=submit]').click()
            page.locator('#dlg-thread').wait_for(state='hidden')
            assert len(bodies) == 2
            assert bodies[0]['request_id'] == bodies[1]['request_id']
            assert bodies[0]['attachment_ids'] == bodies[1]['attachment_ids']
            assert len(bodies[0]['attachment_ids']) == 1
            assert fixture.conn.execute('SELECT COUNT(*) FROM attachments WHERE message_id IS NOT NULL').fetchone()[0] == 1
            assert fixture.conn.execute('SELECT COUNT(*) FROM threads').fetchone()[0] == 1
            page.evaluate('document.querySelector("#dlg-thread").showModal()')
            page.locator('#dlg-title').fill('Topic')
            page.locator('#dlg-opening').fill('Hello')
            page.locator('#thread-form [type=submit]').click()
            page.locator('#dlg-thread').wait_for(state='hidden')
            assert bodies[2]['request_id'] != bodies[1]['request_id']
            assert fixture.conn.execute('SELECT COUNT(*) FROM threads').fetchone()[0] == 2
            assert not errors, errors
            browser.close()
        print('PASS: protected creation dialog, other dialogs dismiss, image opening, retained draft, duplicate submit lock, lost-response retry')
    finally:
        env.stop()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
