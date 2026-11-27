"""Unified editor in real Chrome with synthetic HTTP, no production writes."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path, default=ROOT / 'cache' / 'unified-model-editor.png')
    screenshot = parser.parse_args().screenshot
    writes, errors = [], []
    state = {'conflict': False, 'runtime': 'codex'}
    global_values = dict(description='description', soul='instructions', env='', model='global-old',
                         provider='openai', reasoning_effort='medium', revision='a' * 64,
                         member_profile='bot-36', config_path='E:/fixture/config.toml')
    project_values = dict(project='p', member_profile='bot-36', model='project-old',
                          provider='', reasoning_effort='high', revision='b' * 64)
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
            if path.endswith('/config') or path.endswith('/model'):
                values = global_values if path.endswith('/config') else project_values
                if req.request.method == 'PUT':
                    body = req.request.post_data_json
                    writes.append((path, body))
                    if path.endswith('/model') and state['conflict']:
                        req.fulfill(status=409, json={'detail': 'Conflict'})
                        return
                    assert body['revision'] == values['revision']
                    values.update(body)
                    values['revision'] = ('c' if path.endswith('/config') else 'd') * 64
                    if state.pop('lose_config_receipt', False):
                        req.fulfill(status=504, json={'detail': 'Remote receipt lost'})
                        return
                req.fulfill(json={**values, 'runtime': state['runtime']})
                return
            req.fulfill(json={'projects': [], 'members': [], 'messages': [], 'threads': [], 'commands': []})

        page.route('**/*', route)
        page.goto('http://project-model.test/app/')
        page.evaluate("""() => {
          renderProjectMembers('p', {project:{directory_mode:'shared'}, members:[{
            profile:'bot-36', display_name:'知夏', runtime:'codex', active:1,
            directory_mode:'shared', workspace:{path:'E:/sample',valid:true}}]});
          document.querySelector('#dlg-project-detail').showModal();
          selectProjectTab('members');
        }""")
        assert page.locator('#pj-member-list .project-model').count() == 0
        page.locator('#pj-member-list .member-more').click()
        assert page.locator('#pj-member-list .edit').inner_text() == '编辑 profile'
        page.locator('#pj-member-list .edit').click()
        page.wait_for_function('profileEditor?.revision != null')
        page.evaluate('profileProjectReturn = null')
        page.locator('#profile-tab-model').click()
        model, checkbox, save = (page.locator(selector) for selector in
                                ('#profile-model', '#profile-apply-global', '#profile-save'))
        assert not checkbox.is_checked() and model.input_value() == 'project-old'
        assert page.locator('#profile-provider').input_value() == 'openai'
        model.fill('project-new')
        save.click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        assert len(writes) == 1 and writes[0][0].endswith('/model')
        assert writes[0][1]['provider'] == ''
        assert global_values['model'] == 'global-old'

        def open_editor():
            page.wait_for_function('profileEditor === null')
            page.evaluate('(runtime) => openProfileEditor({profile:"bot-36",runtime}, "p")', state['runtime'])
            page.wait_for_function('profileEditor?.revision != null')
            page.locator('#profile-tab-model').click()

        open_editor()
        model.fill('global-new')
        checkbox.check()
        assert model.input_value() == 'global-new'
        state['conflict'] = True
        save.click()
        page.wait_for_function('document.querySelector("#profile-status").textContent.includes("项目模型保存失败")')
        assert model.input_value() == 'global-new' and save.is_enabled()
        assert global_values['model'] == 'global-new'
        before = len(writes)
        state['conflict'] = False
        save.click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        assert len(writes) == before + 1 and writes[-1][0].endswith('/model')
        assert all(project_values[field] == '' for field in ('model', 'provider', 'reasoning_effort'))

        state['runtime'] = 'hermes'
        open_editor()
        assert model.input_value() == 'global-new' and not checkbox.is_checked()
        hint = page.locator('#profile-model-scope')
        for width in (1280, 390):
            page.set_viewport_size({'width': width, 'height': 844})
            assert hint.is_visible() and '下次唤醒该成员时应用' in hint.inner_text()
            assert hint.evaluate('(el) => el.scrollWidth <= el.clientWidth')
        model.fill('hermes-project')
        page.locator('#profile-tab-description').click()
        page.locator('#profile-description').fill('new description')
        save.click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        assert global_values['description'] == 'new description'
        assert global_values['model'] == 'global-new' and project_values['model'] == 'hermes-project'
        open_editor()
        bounds = page.locator('#dlg-profile').bounding_box()
        cb = checkbox.bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 390
        assert cb['width'] == 14 and cb['height'] == 14
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            assert checkbox.evaluate('(el) => getComputedStyle(el).appearance') == 'none'
            assert checkbox.evaluate('(el) => getComputedStyle(el).borderRadius') == '3px'
            plain = checkbox.evaluate('(el) => getComputedStyle(el).backgroundColor')
            checkbox.check()
            assert checkbox.evaluate('(el) => getComputedStyle(el).backgroundColor') != plain
            assert checkbox.evaluate('(el) => getComputedStyle(el, "::after").content') == '""'
            checkbox.uncheck()
        page.evaluate("applyTheme('dark')")
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot))
        page.locator('#profile-cancel').click()
        global_values.update(remote_profile=True, env_editable=False)
        global_values.pop('env', None)
        global_values['capabilities'] = {'env': {'available': False, 'reason': 'WSS required'}}
        open_editor()
        assert page.locator('#profile-env').is_disabled()
        page.locator('#profile-tab-description').click()
        page.locator('#profile-description').fill('verified after reconnect')
        state['lose_config_receipt'] = True
        before = len(writes)
        save.click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        assert len(writes) == before + 1 and 'env' not in writes[-1][1]
        assert global_values['description'] == 'verified after reconnect'
        assert not errors, errors
        browser.close()
    print('PASS: unified entry, project/global scopes, inheritance, partial failure retry, both runtimes and layout')


if __name__ == '__main__':
    main()
