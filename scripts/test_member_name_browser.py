"""Real Chromium, synthetic API: shared host labels and inline name editing."""
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = {'name': 'p', 'display_name': 'Test', 'root': 'E:/sample', 'directory_mode': 'shared'}
    members = [dict(profile=handle, host_profile='default', bot_id=bot_id, display_name=host,
                    active=1, role='', directory_mode='shared', work_session_exists=False,
                    workspace={'path': 'E:/sample', 'valid': True})
               for handle, bot_id, host in [('default', 1, 'Hermes'), ('bot-36', 36, 'Codex')]]
    writes, errors = [], []
    state = {'fail': False, 'all_joined': False}
    scans = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            data = {}
            if path.endswith('/display-name'):
                writes.append((path, req.request.post_data_json))
                if state['fail']:
                    req.fulfill(status=503, json={'detail': 'offline'})
                    return
                member = next(m for m in members if path == f"/members/{m['profile']}/display-name")
                member['display_name'] = req.request.post_data_json['display_name']
                data = member
            elif path == '/projects':
                data = {'projects': [project]}
            elif path == '/members':
                data = {'members': members}
            elif path == '/discovery/scan':
                scans.append(req.request.url)
                data = {'candidates': [dict(host_type=host, profile=profile, config_root=f'E:/{host}/{profile}',
                                           bound=True, already_member=True,
                                           already_project_member=joined or state['all_joined'])
                                       for host in ('hermes', 'codex')
                                       for profile, joined in [('active', True), ('disabled', True), ('other-project', False)]]}
            elif path.endswith('/workspaces'):
                data = {'project': project, 'members': members, 'history': [], 'repository': {}}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/messages':
                data = {'messages': []}
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://member.test/app/index.html')
        page.evaluate('''p => { S.projects=[p]; S.currentProject=p.name; showLogin(false); renderProjectBar(); }''', project)
        page.evaluate('loadMembers()')
        assert page.locator('#ms-options').text_content().find('Codex（default）') >= 0
        assert page.locator('#ms-options input[value="bot-36"]').count() == 1
        page.locator('[data-project="p"]').click()
        page.locator('#pj-tab-members').click()
        page.locator('.member-name-edit').nth(1).wait_for()
        assert page.locator('.project-member-name small').all_text_contents() == ['default', 'default']
        # Both hosts use the same control, while writes retain the internal handle.
        for index in (0, 1):
            button = page.locator('.member-name-edit').nth(index)
            button.click()
            field = page.locator('.member-name-input')
            field.fill(f'Name {index}')
            field.press('Enter')
            field.wait_for(state='detached')
            assert button.inner_text() == f'Name {index}'
        assert [path for path, _ in writes] == ['/members/default/display-name', '/members/bot-36/display-name']
        button = page.locator('.member-name-edit').nth(1)
        button.click()
        field = page.locator('.member-name-input')
        field.fill('Cancelled')
        field.press('Escape')
        assert len(writes) == 2 and button.inner_text() == 'Name 1'
        button.click()
        field.fill('   ')
        field.press('Enter')
        assert field.get_attribute('aria-invalid') == 'true' and len(writes) == 2
        field.fill('Blur save')
        field.evaluate('(el) => el.blur()')
        field.wait_for(state='detached')
        assert button.inner_text() == 'Blur save' and len(writes) == 3
        button.click()
        field.fill('Retry name')
        state['fail'] = True
        field.press('Enter')
        page.wait_for_function("document.querySelector('.member-name-input').disabled === false")
        assert field.input_value() == 'Retry name' and button.text_content() == 'Blur save'
        state['fail'] = False
        field.press('Enter')
        field.wait_for(state='detached')
        assert button.inner_text() == 'Retry name'
        assert page.evaluate("S.members.find(m=>m.profile==='bot-36').display_name") == 'Retry name'
        button.click()
        field.fill('中文名字')
        field.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
        assert field.is_visible() and len(writes) == 5
        rect = field.bounding_box()
        assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 390
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'member-name-edit-mobile.png'))
        field.press('Escape')
        page.locator('#pj-members-add').click()
        for host in ('hermes', 'codex'):
            page.locator(f'[name="member-add-host"][value="{host}"]').check()
            page.locator('#member-add-next').click()
            page.wait_for_function("!memberAdd.scanning")
            assert page.locator('#member-add-steps input[type=radio]').count() == 1
            assert 'other-project' in page.locator('#member-add-steps').inner_text()
            assert 'disabled' not in page.locator('#member-add-steps').inner_text()
            assert page.locator('#member-add-join').is_disabled()
            page.locator('#member-add-steps input[type=radio]').check()
            assert page.locator('#member-add-join').is_enabled()
            page.locator('#member-add-back').click()
        state['all_joined'] = True
        page.locator('#member-add-next').click()
        page.wait_for_function("!memberAdd.scanning")
        assert page.locator('#member-add-status').inner_text() == '暂无可添加成员'
        assert page.locator('#member-add-steps input[type=radio]').count() == 0
        assert page.locator('#member-add-join').is_disabled()
        assert len(scans) == 3 and all('project=p' in url for url in scans)
        assert not errors, errors
        browser.close()
    print('Member identity browser checks passed')


if __name__ == '__main__':
    main()
