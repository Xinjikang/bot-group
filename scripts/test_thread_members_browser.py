"""Project-scoped topic membership UI with intercepted APIs and isolated Chrome."""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    output = parser.parse_args().evidence.resolve()
    output.mkdir(parents=True, exist_ok=True)
    members = [dict(profile=p, host_profile=p, display_name=n, active=a)
               for p, n, a in [('local', '本项目成员', 1), ('paused', '停用成员', 0),
                               ('paused-in', '话题内停用成员', 0), ('foreign', '项目外成员', 1)]]
    topic = dict(id=7, project='p', title='项目话题', members=json.dumps(['user', 'paused-in']), status='open')
    state = {'fail': False, 'empty': False, 'hold': False}
    pending, requests, posts, errors = [], [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(error.stack))

        def route(req):
            url = urlparse(req.request.url)
            path, query = url.path, parse_qs(url.query)
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if path.startswith('/hud'):
                req.fulfill(status=503, json={'detail': '测试未启用项目助手'})
                return
            data = {}
            if path == '/members':
                if query.get('enrolled_only') == ['true']:
                    requests.append(query)
                    if state['hold']:
                        pending.append(req)
                        return
                    if state['fail']:
                        req.fulfill(status=503, json={'detail': '模拟读取失败'})
                        return
                    data = {'members': [] if state['empty'] else members[:3]}
                else:
                    data = {'members': members}
            elif path == '/threads':
                data = {'threads': [topic]}
            elif path == '/thread/7/members':
                body = req.request.post_data_json
                posts.append(body)
                participants = [p for p in json.loads(topic['members']) if p not in body['remove']]
                participants += [p for p in body['add'] if p not in participants]
                topic['members'] = json.dumps(participants)
                data = {'members': participants}
            elif path == '/projects':
                data = {'projects': []}
            elif path == '/messages':
                data = {'messages': []}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            elif path == '/auth/status':
                data = {'configured': False}
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://thread-members.test/app/index.html')
        page.evaluate('''members => {
            showLogin(false); S.currentProject = 'other'; S.members = members;
            showConfirm = async () => true;
        }''', members)

        def open_topic(value=topic):
            page.evaluate('t => openThreadMemberDlg(t, null)', value)

        open_topic()
        page.wait_for_function("document.querySelectorAll('#thread-member-list .tm-row').length === 3")
        assert requests[-1]['project'] == ['p']
        assert '项目外成员' not in page.locator('#thread-member-list').inner_text()
        assert page.locator('#thread-member-list button[data-profile="paused"]').is_disabled()
        assert page.locator('#thread-member-list button[data-profile="paused-in"]').is_enabled()
        assert page.evaluate('S.members.length') == 4
        page.locator('#thread-member-list button[data-profile="local"]').click()
        page.wait_for_function("document.querySelector('#thread-member-list button[data-profile=local]')?.textContent === '移出'")
        assert posts[-1] == {'add': ['local'], 'remove': []}
        page.locator('#thread-member-list button[data-profile="paused-in"]').click()
        page.wait_for_function("document.querySelector('#thread-member-list button[data-profile=paused-in]')?.disabled")
        assert posts[-1] == {'add': [], 'remove': ['paused-in']}

        for theme in ('dark', 'light'):
            for width in (1280, 390):
                page.set_viewport_size({'width': width, 'height': 900})
                page.evaluate('applyTheme', theme)
                open_topic()
                page.wait_for_function("document.querySelectorAll('#thread-member-list .tm-row').length === 3")
                box = page.locator('#dlg-thread-members').bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1, box
                page.screenshot(path=str(output / f'thread-members-{theme}-{width}.png'))

        state['fail'] = True
        open_topic()
        page.get_by_role('button', name='重试', exact=True).wait_for()
        assert page.locator('#thread-member-list .tm-row').count() == 0
        state['fail'] = False
        page.get_by_role('button', name='重试', exact=True).click()
        page.wait_for_function("document.querySelectorAll('#thread-member-list .tm-row').length === 3")
        state['empty'] = True
        open_topic()
        page.get_by_text('本项目暂无 Bot 成员', exact=True).wait_for()
        state['empty'] = False

        state['hold'] = True
        open_topic()
        page.wait_for_function("document.querySelector('#thread-member-list').textContent.includes('正在读取')")
        page.wait_for_timeout(100)
        state['hold'] = False
        open_topic({**topic, 'id': 8, 'project': 'q', 'title': '另一话题'})
        page.wait_for_function("document.querySelectorAll('#thread-member-list .tm-row').length === 3")
        assert pending, 'first topic request must be in flight'
        pending.pop().fulfill(json={'members': [members[-1]]})
        page.wait_for_timeout(100)
        assert '项目外成员' not in page.locator('#thread-member-list').inner_text()
        assert '另一话题' in page.locator('#tm-thread-title').inner_text()
        assert requests[-1]['project'] == ['q']
        page.locator('#dlg-tm-close').click()
        open_topic()
        page.wait_for_function("document.querySelectorAll('#thread-member-list .tm-row').length === 3")
        assert not errors, errors
        browser.close()
    print('PASS: project scope, disabled members, add/remove, themes/widths, failure/retry, empty roster, stale responses, reopen')


if __name__ == '__main__':
    main()
