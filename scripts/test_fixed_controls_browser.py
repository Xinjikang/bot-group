"""Fixed topic toggle and sticky detail controls; synthetic browser data only."""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                req.fulfill(json={'messages': [], 'threads': [], 'tasks': [], 'requirements': [], 'logs': []})
        page.route('**/*', route)
        page.goto('http://controls.test/app/')
        page.evaluate('''() => {
          showLogin(false); applyTheme('light'); S.currentProject='p';
          S.threads = Array.from({length:100}, (_,i) => ({id:i+1,project:'p',
            title:'话题 '+i,status:'closed',hidden:i>=50,members:'[]'})); renderThreads();
        }''')
        toggle = page.locator('#thread-visibility')
        initial = toggle.bounding_box()
        assert page.locator('.thread-hidden-card').count() == 0
        page.locator('#thread-list').evaluate('e => e.scrollTop=e.scrollHeight')
        assert toggle.bounding_box() == initial
        toggle.click()
        assert page.locator('.thread-hidden-card').count() == 50
        page.locator('#thread-list').evaluate('e => e.scrollTop=e.scrollHeight')
        assert toggle.bounding_box() == initial
        toggle.click()
        assert page.locator('.thread-hidden-card').count() == 0

        for width in (1280, 390):
            page.set_viewport_size({'width':width,'height':800})
            for selector in ('#dlg-task-history', '#dlg-requirement-details'):
                page.evaluate('''selector => {
                  const d=document.querySelector(selector);
                  const content=d.querySelector('#task-history-body, [data-content]');
                  content.innerHTML='<p>长详情内容</p>'.repeat(250);
                  d.showModal();
                  const scroll=d.querySelector('.task-detail-scroll');
                  scroll.scrollTop=scroll.scrollHeight;
                }''', selector)
                dialog = page.locator(selector)
                bounds = dialog.bounding_box()
                assert dialog.locator('.task-detail-head h3').count() == 0
                assert dialog.locator('.task-detail-scroll').evaluate('(e) => e.scrollTop > 0')
                title = dialog.locator('.task-detail-scroll > h3').bounding_box()
                assert title['y'] + title['height'] < bounds['y'], 'title scrolls away with content'
                buttons = dialog.locator('.task-detail-head button')
                for button in buttons.all():
                    b = button.bounding_box()
                    assert bounds['y'] <= b['y'] and b['y']+b['height'] <= bounds['y']+bounds['height']
                    assert button.evaluate('(e) => { const r=e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)); }')
                # Real close handler remains reachable at the end of long content.
                buttons.last.click()
                dialog.wait_for(state='hidden')
        assert not errors, errors
        browser.close()
    print('PASS: persistent topic toggle, expand/collapse, task/requirement controls at scroll bottom, desktop/mobile')


if __name__ == '__main__':
    main()
