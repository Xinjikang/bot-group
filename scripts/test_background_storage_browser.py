"""Isolated Chrome + real API: full localStorage, reload, fresh browser, migration."""
import base64
import io
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image
from playwright.sync_api import sync_playwright
from background_test_support import BackgroundAPI
from engine import appearance

ROOT = Path(__file__).resolve().parents[1]


def ready(page):
    page.evaluate('showLogin(false)')
    page.wait_for_function('!document.querySelector("#background-select").disabled')


def assert_position(page):
    assert page.locator('#app-settings-menu').is_hidden()
    dialog = page.locator('#dlg-background').bounding_box()
    toggle = page.locator('#btn-settings').bounding_box()
    assert dialog['x'] > page.viewport_size['width'] / 2, dialog
    assert abs(dialog['y'] - toggle['y'] - toggle['height'] - 6) < 2, (dialog, toggle)
    assert dialog['x'] + dialog['width'] <= page.viewport_size['width'], dialog


def main():
    photo = io.BytesIO()
    Image.new('RGB', (80, 80), '#446688').save(photo, format='JPEG')
    upload = {'name': 'background.jpg', 'mimeType': 'image/jpeg', 'buffer': photo.getvalue()}
    with BackgroundAPI() as background, sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)

        def route(request):
            path = urlparse(request.request.url).path
            if path == '/appearance/background':
                background.route(request)
            elif path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                request.fulfill(json={'threads': [], 'messages': [], 'members': [], 'projects': [], 'tasks': [], 'requirements': []})

        page = browser.new_page()
        page.route('**/*', route)
        page.goto('http://background.test/app/')
        ready(page)
        page.evaluate('''() => {
            localStorage.setItem('group-engine.thinking.v2', JSON.stringify({runs:[], bubbles:[], padding:'x'.repeat(1000000)}));
            for (let size=512*1024, n=0; size>=128; size=Math.floor(size/2)) {
                while (true) {
                    try { localStorage.setItem('quota-fixture-' + n, 'x'.repeat(size)); n++; }
                    catch (e) { if(e.name !== 'QuotaExceededError') throw e; break; }
                }
            }
        }''')
        assert page.evaluate('''() => {
            try {localStorage.setItem('quota-probe', 'x'.repeat(4096)); return false;}
            catch(e) {return e.name === 'QuotaExceededError';}
        }''')
        page.locator('#btn-settings').click()
        page.locator('#btn-background').click()
        ready(page)
        assert_position(page)
        with page.expect_file_chooser() as chooser:
            page.locator('#background-select').click()
        chooser.value.set_files(upload)
        page.wait_for_function('document.querySelector("#background-status").textContent.includes("背景已保存")')
        assert_position(page)
        assert appearance.read() == photo.getvalue()
        assert page.evaluate('JSON.parse(localStorage.getItem("group-engine.thinking.v2")).padding.length') == 1000000
        for theme in ('light', 'dark'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            assert 'blob:' in page.locator('.col-msgs').evaluate('e => getComputedStyle(e).backgroundImage')
        page.reload()
        ready(page)
        assert 'blob:' in page.locator('.col-msgs').evaluate('e => getComputedStyle(e).backgroundImage')
        fresh = browser.new_page()
        fresh.route('**/*', route)
        fresh.goto('http://background.test/app/')
        ready(fresh)
        assert 'blob:' in fresh.locator('.col-msgs').evaluate('e => getComputedStyle(e).backgroundImage')
        appearance.path().unlink()
        legacy = browser.new_page()
        legacy.route('**/*', route)
        data_url = 'data:image/jpeg;base64,' + base64.b64encode(photo.getvalue()).decode()
        legacy.add_init_script('localStorage.setItem("ge_viewport_background", ' + json.dumps(data_url) + ');')
        legacy.goto('http://background.test/app/')
        ready(legacy)
        assert appearance.read() == photo.getvalue()
        assert legacy.evaluate('localStorage.getItem("ge_viewport_background")') is None
        legacy.locator('#btn-settings').click()
        legacy.locator('#btn-background').click()
        ready(legacy)
        assert_position(legacy)
        legacy.locator('#background-reset').click()
        legacy.wait_for_function('document.querySelector("#background-status").textContent.includes("已恢复默认")')
        assert_position(legacy)
        legacy.reload()
        ready(legacy)
        assert 'viewport-planet.jpg' in legacy.locator('.col-msgs').evaluate('e => getComputedStyle(e).backgroundImage')
        assert appearance.read() == b''
        browser.close()
    print('PASS: full localStorage, server JPEG save/reload/fresh context, legacy migration/reset; settings anchor after load/upload/reset; default planet retained')


if __name__ == '__main__':
    main()
