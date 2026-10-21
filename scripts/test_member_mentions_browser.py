"""Real Chromium, mocked APIs: visible labels and unchanged /msg identities."""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    members = [dict(profile=handle, host_profile='default', display_name=name, active=1)
               for handle, name in [('default', 'Hermes'), ('bot-36', 'Codex')]]
    posts, errors = [], []
    state = {'fail': False}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            data = {}
            if path == '/members':
                data = {'members': members}
            elif path == '/projects':
                data = {'projects': [{'name': 'p', 'display_name': 'Test'}]}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/messages':
                data = {'messages': []}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            elif path == '/commands':
                data = {'commands': []}
            elif path == '/msg':
                posts.append(req.request.post_data_json)
                if state['fail']:
                    req.fulfill(status=503, json={'detail': 'offline'})
                    return
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://mention.test/app/index.html')
        page.evaluate('''members => {
            S.members = members; S.currentProject = 'p'; S.currentThread = null;
            showLogin(false); updateComposerState();
            window.testAlerts = []; showAlert = async text => { window.testAlerts.push(text); };
        }''', members)
        field = page.locator('#composer-text')

        def choose(target, query, label):
            target.fill(query)
            target.press('End')
            target.evaluate('(el) => mpDetect(el, null)')
            page.locator('#mention-pop .mp-item').filter(has_text=label).click()

        choose(field, '@Cod', 'Codex（default）')
        assert field.input_value() == '@Codex（default） '
        field.press('End')
        field.press_sequentially('你好')
        state['fail'] = True
        field.press('Enter')
        page.wait_for_function('window.testAlerts.length === 1')
        assert posts[-1]['text'] == '@bot-36 你好'
        assert field.input_value() == '@Codex（default） 你好'
        state['fail'] = False
        field.press('Enter')
        page.wait_for_function("document.querySelector('#composer-text').value === ''")
        assert posts[-1]['text'] == '@bot-36 你好' and posts[-1]['project'] == 'p'
        choose(field, '@Her', 'Hermes（default）')
        field.press('Enter')
        page.wait_for_function("document.querySelector('#composer-text').value === ''")
        assert posts[-1]['text'] == '@default'
        choose(field, '/new @Cod', 'Codex（default）')
        field.press('Enter')
        page.wait_for_function("document.querySelector('#composer-text').value === ''")
        assert posts[-1]['text'] == '/new @bot-36'
        page.evaluate('''() => {
            S.threads = [{id: 7, members: ['bot-36'], status: 'open'}];
            const reply = buildThreadReply({threadId: 7, title: 'Test'});
            reply.id = 'test-reply'; document.body.appendChild(reply); reply.click();
        }''')
        reply = page.locator('#test-reply textarea')
        reply.fill('@')
        reply.press('End')
        reply.evaluate('(el) => mpDetect(el, 7)')
        assert page.locator('#mention-pop .mp-item').count() == 1
        page.locator('#mention-pop .mp-item').click()
        assert reply.input_value() == '@Codex（default） '
        reply.press('Control+Enter')
        page.wait_for_function("!document.querySelector('#test-reply textarea')")
        assert posts[-1]['text'] == '@bot-36' and posts[-1]['thread_id'] == 7
        # Control response ordering while rendering the real member selector.
        result = page.evaluate('''async () => {
            const original = api;
            const pending = [];
            api = (url, ...args) => url.startsWith('/members')
                ? new Promise(resolve => pending.push(resolve)) : original(url, ...args);
            try {
                S.currentThread = null; S.currentProject = 'a';
                const a = loadMembers();
                S.currentProject = 'b';
                const b = loadMembers();
                const cleared = document.querySelectorAll('#ms-options input').length === 0;
                pending[1]({members: [{profile: 'b-only', display_name: 'B member', active: 1}]});
                await b;
                pending[0]({members: [{profile: 'a-only', display_name: 'A member', active: 1}]});
                await a;
                return {cleared, profiles: [...document.querySelectorAll('#ms-options input')].map(el => el.value)};
            } finally { api = original; }
        }''')
        assert result == {'cleared': True, 'profiles': ['b-only']}, result
        assert not errors, errors
        browser.close()
    print('PASS: real browser mentions, commands, failure retry and project member response race')


if __name__ == '__main__':
    main()
