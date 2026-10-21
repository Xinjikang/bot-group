"""Profile targets and effort choices in real Chrome, with synthetic API only."""
from copy import deepcopy
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    writes, errors = [], []
    project = dict(name='p', root='E:/sample/repo', directory_mode='shared')
    members = [
        dict(profile='bot-36', host_profile='default', display_name='知夏', runtime='codex'),
        dict(profile='bot-37', host_profile='dev-01', display_name='江澈', runtime='codex'),
        dict(profile='default', host_profile='default', display_name='Hermes', runtime='hermes'),
    ]
    for member in members:
        member.update(active=1, role='', directory_mode='shared',
                      work_session_exists=False, workspace=dict(path=project['root'], valid=True))
    configs = {}
    for member in members:
        codex = member['runtime'] == 'codex'
        home = 'E:/codex/profiles/dev-01' if member['profile'] == 'bot-37' else 'E:/codex'
        configs[member['profile']] = dict(
            runtime=member['runtime'], member_profile=member['profile'],
            description='Role description', soul='# Instructions',
            env='' if codex else 'EXAMPLE=1',
            model='jiang-model' if member['profile'] == 'bot-37' else 'claire-model',
            provider='openai', reasoning_effort='medium' if member['profile'] == 'bot-37' else 'xhigh',
            revision='a' * 64, agent_name=member['host_profile'],
            description_path='E:/codex/config.toml', config_path=home + '/config.toml')
    mismatched_response = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1100, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            data = {}
            if path.endswith('/config'):
                handle = path.split('/')[2]
                if req.request.method == 'PUT':
                    body = req.request.post_data_json
                    writes.append((path, body))
                    configs[handle].update({key: body[key] for key in
                                          ('description', 'soul', 'env', 'model', 'provider', 'reasoning_effort')})
                    data = {'saved': True}
                else:
                    data = deepcopy(configs[handle])
                    if mismatched_response:
                        data['member_profile'] = 'bot-36'
            elif path.endswith('/model'):
                handle = path.split('/')[-2]
                data = dict(project='p', member_profile=handle, runtime=configs[handle]['runtime'],
                            model='', provider='', reasoning_effort='', revision='b' * 64)
            elif path == '/members':
                data = {'members': members}
            elif path == '/projects':
                data = {'projects': [project]}
            elif path == '/projects/p/workspaces':
                data = {'project': project, 'members': members, 'history': []}
            elif path == '/projects/p/roles':
                data = {'roles': []}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/messages':
                data = {'messages': []}
            req.fulfill(json=data)
        page.route('**/*', route)
        page.goto('http://config.test/app/index.html')
        page.evaluate('''([p, members]) => {
            S.projects=[p]; S.members=members; S.currentProject=p.name;
            showLogin(false); renderProjectBar();
        }''', [project, members])
        def loaded():
            page.wait_for_function('profileEditor?.revision != null')
        def closed():
            page.locator('#dlg-profile').wait_for(state='hidden')
            # close() hides the dialog before its queued close handler resets the editor.
            page.wait_for_function('profileEditor === null')
        for runtime, handle, tabcount, filename in [('codex', 'bot-36', 3, 'AGENTS.md'), ('hermes', 'default', 4, 'SOUL.md'), ('codex', 'bot-36', 3, 'AGENTS.md')]:
            page.evaluate('(m) => openProfileEditor(m)', next(m for m in members if m['profile'] == handle))
            loaded()
            assert page.locator('#profile-tabs [role=tab]:visible').count() == tabcount
            assert page.locator('#profile-tab-soul').inner_text() == filename
            assert page.locator('#profile-model').input_value() == configs[handle]['model']
            choices = page.locator('#profile-reasoning_effort option').evaluate_all('(options) => options.map(o => o.value)')
            assert choices == (['', 'low', 'medium', 'high', 'xhigh', 'ultra'] if runtime == 'codex'
                               else ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
            page.locator('#profile-tab-soul').click()
            page.locator('#profile-tab-soul').press('ArrowRight')
            assert page.locator('#profile-tab-model' if runtime == 'codex' else '#profile-tab-env').get_attribute('aria-selected') == 'true'
            page.locator('#profile-tab-description').click()
            page.locator('#profile-description').fill('Updated description')
            page.locator('#profile-save').click()
            closed()
            path, body = writes[-1]
            assert path == f'/members/{handle}/config'
            assert body['member_profile'] == handle
            assert body['description'] == 'Updated description'
            assert body['env'] == ('' if runtime == 'codex' else 'EXAMPLE=1')
            if runtime == 'codex':
                assert body['config_path'] == configs[handle]['config_path']

        # Click actual project-detail cards, then save model/effort only in both directions.
        page.evaluate("openProjectDetail('p'); selectProjectTab('members')")
        for handle, other, model, effort in [
            ('bot-37', 'bot-36', 'jiang-new', 'ultra'),
            ('bot-36', 'bot-37', 'claire-new', 'low'),
        ]:
            untouched = deepcopy(configs[other])
            card = page.locator(f'#pj-member-list [data-profile="{handle}"]')
            card.locator('.edit').click()
            loaded()
            name = next(m['display_name'] for m in members if m['profile'] == handle)
            assert name in page.locator('#profile-title').inner_text()
            assert page.locator('#profile-model').input_value() == configs[handle]['model']
            page.locator('#profile-tab-model').click()
            page.locator('#profile-apply-global').check()
            assert configs[handle]['config_path'] in page.locator('#profile-panel-model .profile-help').first.inner_text()
            page.locator('#profile-model').fill(model)
            page.locator('#profile-reasoning_effort').select_option(effort)
            page.locator('#profile-save').click()
            closed()
            path, body = writes[-1]
            assert path == f'/members/{handle}/config'
            assert body['member_profile'] == handle
            assert body['config_path'] == configs[handle]['config_path']
            assert configs[handle]['model'] == model
            assert configs[handle]['reasoning_effort'] == effort
            assert configs[other] == untouched
        page.locator('#dlg-pj-detail-close').click()

        # An old unsupported effort requires an explicit choice before any save.
        configs['bot-37']['reasoning_effort'] = 'max'
        page.evaluate('(m) => openProfileEditor(m)', members[1])
        loaded()
        assert 'max' in page.locator('#profile-status').inner_text()
        before = len(writes)
        page.locator('#profile-description').fill('Legacy effort edit')
        page.locator('#profile-save').click()
        page.wait_for_function("document.querySelector('#profile-tab-model').getAttribute('aria-selected') === 'true'")
        assert len(writes) == before
        assert page.locator('#profile-reasoning_effort').evaluate('(el) => el.validity.customError')
        page.locator('#profile-reasoning_effort').select_option('')
        assert page.locator('#profile-reasoning_effort').evaluate('(el) => el.validity.valid')
        page.locator('#profile-save').click()
        closed()
        assert configs['bot-37']['reasoning_effort'] == ''

        # A foreign member response leaves save disabled and reports the mismatch.
        mismatched_response = True
        page.evaluate('(m) => openProfileEditor(m)', members[1])
        page.wait_for_function("document.querySelector('#profile-status').textContent.includes('读取失败')")
        assert page.locator('#profile-save').is_disabled()
        assert page.locator('#profile-model').is_disabled()
        assert '当前成员' in page.locator('#profile-status').inner_text()
        page.locator('#profile-cancel').click()
        closed()
        assert not errors, errors
        browser.close()
    print('Codex/Hermes editor targets, five tiers and legacy-value browser checks passed')


if __name__ == '__main__':
    main()
