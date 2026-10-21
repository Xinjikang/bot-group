"""Real Chromium with mocked HTTP only; no production reads or writes."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def message(mid, mentions=False, thread=None, project='p'):
    return dict(id=mid, project=project, sender='bot-1', sender_type='bot',
                content=f'Message {mid} ' + ('@user 请查看' if mentions else '普通消息'),
                mentions=json.dumps(['user'] if mentions else []), thread_id=thread,
                ts=f'2026-10-11 10:{mid // 60 % 60:02}:{mid % 60:02}', attachments=[])


def main():
    messages = [message(i, i in (15, 30), 7 if i == 30 else None) for i in range(1, 61)]
    messages[20]['content'] = 'Quoted text @user'
    messages[40].update(sender_type='engine', mentions='["user"]')
    state = {'fail': False, 'hold': False}
    errors, held = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(req):
            parsed = urlparse(req.request.url)
            path, query = parsed.path, parse_qs(parsed.query)
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                if file.is_file():
                    req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                else:
                    req.fulfill(status=404)
                return
            data = {}
            if path == '/messages':
                if 'before_id' in query and query.get('limit') == ['1']:
                    if state['hold']:
                        held.append(req)
                        return
                    if state['fail']:
                        req.fulfill(status=503, json={'detail': 'offline'})
                        return
                project = query.get('project', ['p'])[0]
                since = int(query.get('since_id', [0])[0])
                before = int(query.get('before_id', [10**9])[0])
                rows = [m for m in messages if m['project'] == project and since < m['id'] < before]
                if 'thread_id' in query:
                    rows = [m for m in rows if m['thread_id'] == int(query['thread_id'][0])]
                limit = int(query.get('limit', [len(rows) or 1])[0])
                data = {'has_more': len(rows) > limit,
                        'messages': rows[-limit:] if since == 0 and 'limit' in query else rows[:limit]}
            elif path == '/threads':
                data = {'threads': [dict(id=7, project='p', title='Topic', status='open',
                                        members=[], first_message_id=30, created_at=messages[29]['ts'])]}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            req.fulfill(json=data)

        page.route('**/*', route)

        def open_project(project='p', target=None):
            (target or page).evaluate('''async project => {
                showLogin(false); window.testAlerts = []; window.showAlert = msg => testAlerts.push(msg);
                S.currentProject = project;
                S.threads = [{id:7, project:'p', title:'Topic', status:'open', members:[],
                              first_message_id:30, created_at:'2026-10-11 10:00:30'}];
                S.collapsedThreads.add(7);
                await selectThread(null); await messageNavigation.refresh();
            }''', project)

        page.goto('http://navigation.test/app/index.html')
        open_project()
        at = page.locator('#message-mentions')
        bottom = page.locator('#message-bottom')
        assert at.inner_text() == '有人 @你 · 2'
        assert bottom.is_hidden(), 'initial bottom still shows @ alert'
        page.evaluate("document.querySelector('#msg-list').scrollTop = 0")
        bottom.wait_for(state='visible')
        bounds = page.locator('#message-navigation').bounding_box()
        b, a = bottom.bounding_box(), at.bounding_box()
        assert abs((b['x'] + b['width']/2) - (bounds['x'] + bounds['width']/2)) < 2
        assert abs(b['y'] - a['y']) < 2 and a['x'] > b['x'] + b['width']
        at.click()
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        assert page.locator('[data-message-id="15"]').is_visible()
        bottom.click()
        assert at.inner_text() == '有人 @你 · 1', 'bottom does not consume mentions'
        at.click()
        at.wait_for(state='hidden')
        assert 'collapsed' not in page.locator('.msg-group[data-thread="7"]').get_attribute('class')
        page.evaluate('refreshMessages()')
        assert at.is_hidden(), 'polling must not restore consumed messages'

        messages.extend([message(61, True), message(62, True, 7), message(63, True, project='q')])
        page.evaluate('async () => { await refreshMessages(); await messageNavigation.refresh(); }')
        assert at.inner_text() == '有人 @你 · 2'
        page.reload()
        open_project()
        assert at.inner_text() == '有人 @你 · 2', 'reload persists unread IDs'
        page.evaluate('async () => { await selectThread(7); await messageNavigation.refresh(); }')
        assert at.inner_text() == '有人 @你 · 1', 'topic scope'
        open_project('q')
        assert at.inner_text() == '有人 @你 · 1', 'project isolation'
        open_project()
        assert at.inner_text() == '有人 @你 · 2'

        state['fail'] = True
        at.click()
        page.wait_for_function('testAlerts.length > 0')
        assert at.inner_text() == '有人 @你 · 2', 'failure retains pending record'
        state['fail'] = False
        messages[-3]['retract_by'] = 'bot-1'
        at.click()
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        state['hold'] = True
        at.click()
        page.wait_for_timeout(50)
        open_project('q')
        held.pop().fulfill(json={'messages': [messages[-2]]})
        state['hold'] = False
        open_project()
        assert at.inner_text() == '有人 @你 · 1', 'stale navigation cannot acknowledge'

        # More than one page arrives while the browser is closed. The earliest
        # pending ID must be loadable even beyond the new latest-100 window.
        messages.extend(message(i, i in (100, 620)) for i in range(64, 665))
        page.reload()
        open_project()
        assert at.inner_text() == '有人 @你 · 3', 'offline multi-page catch-up'
        at.click()
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('2')")
        assert page.locator('[data-message-id="62"]').is_visible()
        at.click()
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        node = page.locator('[data-message-id="100"]').bounding_box()
        viewport = page.locator('#msg-list').bounding_box()
        assert viewport['y'] <= node['y'] < viewport['y'] + viewport['height']

        # Verify both themes and constrained column width without real services.
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            for width in (1280, 1024):
                page.set_viewport_size({'width': width, 'height': 900})
                page.evaluate("document.querySelector('#msg-list').scrollTop = 0")
                page.wait_for_timeout(80)
                b, a = bottom.bounding_box(), at.bounding_box()
                assert abs(b['y'] - a['y']) < 2
                assert a['x'] >= b['x'] + b['width'], (width, b, a)
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.evaluate("applyTheme('dark')")
        output = ROOT / 'cache' / 'message-navigation.png'
        output.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(output))

        at.click()
        at.wait_for(state='hidden')
        messages.append(message(665, True, 7))
        page.evaluate('async () => { await refreshMessages(); await messageNavigation.refresh(); '
                      'S.threads[0].hidden = true; renderMessages(); }')
        at.click()
        at.wait_for(state='hidden')
        assert page.evaluate('S.currentThread') == 7, 'hidden topic opens for navigation'
        assert page.evaluate('S.threads[0].hidden'), 'topic visibility setting retained'

        open_project('empty')
        assert at.is_hidden()
        messages.extend(message(i, i in (701, 1200), project='empty') for i in range(700, 1220))
        page.evaluate('async () => { await refreshMessages(); await messageNavigation.refresh(); }')
        assert at.inner_text() == '有人 @你 · 2', 'zero cursor must not skip the first page'
        # Work overview and chat/topic share the same project queue.
        messages.extend(message(i, i in (2002, 2029), 7, 'read') for i in range(2001, 2031))
        open_project('read')
        assert page.evaluate('document.hasFocus()')
        page.wait_for_timeout(1200)
        assert at.inner_text() == '有人 @你 · 2', 'collapsed topic stays unread'
        page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable:true, get:()=>'hidden'})")
        page.evaluate('async () => { await selectThread(7); await messageNavigation.refresh(); }')
        assert page.locator('#message-navigation').is_visible(), 'chat has navigation row'
        page.wait_for_timeout(1200)
        assert at.inner_text() == '有人 @你 · 2', 'background chat stays unread'
        page.evaluate("delete document.visibilityState; document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(650)
        assert at.inner_text() == '有人 @你 · 2', 'less than one second stays unread'
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        page.evaluate("document.querySelector('#msg-list').scrollTop = 0")
        bottom.wait_for(state='visible')
        # Switch before the dwell interval so only the bottom mention was read.
        open_project('read')
        assert at.inner_text() == '有人 @你 · 1', 'chat read decrements work count'
        page.evaluate('''() => {
            S.collapsedThreads.delete(7); renderMessages();
            document.querySelector('[data-message-id="2002"]').scrollIntoView({block:'center'});
        }''')
        at.wait_for(state='hidden')
        page.evaluate('async () => { await selectThread(7); await messageNavigation.refresh(); }')
        assert at.is_hidden(), 'work read decrements chat count'
        page.reload()
        open_project('read')
        assert at.is_hidden(), 'visible-read acknowledgement survives reload'

        # Two real pages share localStorage. A stale polling page must preserve
        # acknowledgements, while its own unread messages remain navigable.
        messages.extend(message(i, i in (4002, 4029), 7, 'shared') for i in range(4001, 4031))
        open_project('shared')
        other = page.context.new_page()
        other.route('**/*', route)
        other.on('pageerror', lambda error: errors.append(str(error)))
        other.goto('http://navigation.test/app/index.html')
        open_project('shared', other)
        assert other.locator('#message-mentions').inner_text() == '有人 @你 · 2'
        at.click()
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        other.evaluate('messageNavigation.observe([])')
        other.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        page.reload()
        open_project('shared')
        assert at.inner_text() == '有人 @你 · 1', 'another page cannot restore a consumed mention on reload'
        other.close()

        # Keep the older shared snapshot intact while simulating quota failure.
        # The same tab must restore its newer acknowledgement from sessionStorage.
        messages.extend(message(i, i in (5002, 5029), 7, 'quota') for i in range(5001, 5031))
        open_project('quota')
        page.evaluate('''() => {
            const original = Storage.prototype.setItem;
            Storage.prototype.setItem = function(key, value) {
                if (this === localStorage && key.startsWith('ge_mentions_v1:'))
                    throw new DOMException('full', 'QuotaExceededError');
                return original.call(this, key, value);
            };
        }''')
        at.click()
        page.wait_for_function("document.querySelector('#message-mentions').textContent.endsWith('1')")
        assert len(page.evaluate("JSON.parse(localStorage.getItem('ge_mentions_v1:quota')).pending")) == 2
        page.reload()
        open_project('quota')
        assert at.inner_text() == '有人 @你 · 1', 'session backup preserves acknowledgement after quota failure'
        at.click()
        at.wait_for(state='hidden')
        page.reload()
        open_project('quota')
        assert at.is_hidden(), 'all acknowledgements survive reload'
        assert not errors, errors
        browser.close()
    print('PASS: layout, independent controls, ordered navigation, collapse, scopes, reload, '
          'failure, retraction, stale response, offline pagination, both themes, shared pages and quota fallback')


if __name__ == '__main__':
    main()
