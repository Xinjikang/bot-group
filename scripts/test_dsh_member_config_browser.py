"""DSH member configuration editor in real Chrome, with synthetic API only."""
import argparse
from copy import deepcopy
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', default='cache/dsh-member-config')
    parser.add_argument('--plugins-json', help='real GET /members/<profile>/config payload from the backend')
    args = parser.parse_args()
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)

    writes, errors = [], []
    project = dict(name='p', root='E:/sample/repo', directory_mode='shared')
    member = dict(profile='bot-dsh', host_profile='desktop', display_name='DSH 成员',
                  runtime='dsh', active=1, role='', directory_mode='shared',
                  work_session_exists=False,
                  workspace=dict(path=project['root'], valid=True))
    config = dict(
        runtime='dsh', member_profile='bot-dsh', description='DSH 描述',
        persona='你是 DSH 人格', instructions='先思考再回答',
        env='DEEPSEEK_API_KEY=test\n', model='deepseek-flash',
        provider='deepseek-account', reasoning_effort='max', auto_review='off',
        revision='a' * 64,
        config_path='C:/dsh/profiles/desktop/cordis.patch.yml',
    )
    project_model = dict(project='p', member_profile='bot-dsh', runtime='dsh',
                         model='', provider='', reasoning_effort='', revision='b' * 64)
    state = {'config_conflict': False, 'model_conflict': False}
    real_plugins = None
    if args.plugins_json:
        real_plugins = json.loads(Path(args.plugins_json).read_text(encoding='utf-8'))['plugins']

    def config_response():
        data = deepcopy(config)
        data['plugins'] = deepcopy(state.get('plugins', {}))
        return data

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(),
                            content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if path == '/members/bot-dsh/config':
                if req.request.method == 'PUT':
                    body = req.request.post_data_json
                    writes.append((path, body))
                    if state['config_conflict']:
                        req.fulfill(status=409, json={'detail': '配置已被其他操作修改'})
                        return
                    assert body['revision'] == config['revision']
                    for key in ('description', 'persona', 'instructions', 'env', 'model',
                                'provider', 'reasoning_effort', 'auto_review'):
                        assert key in body, key
                    config.update({key: body[key] for key in
                                   ('description', 'persona', 'instructions', 'env', 'model',
                                    'provider', 'reasoning_effort', 'auto_review')})
                    config['revision'] = 'c' * 64
                    req.fulfill(json={'saved': True, 'backup': ['backup']})
                else:
                    req.fulfill(json=config_response())
                return
            if path == '/projects/p/members/bot-dsh/model':
                if req.request.method == 'PUT':
                    body = req.request.post_data_json
                    writes.append((path, body))
                    if state['model_conflict']:
                        req.fulfill(status=409, json={'detail': '项目模型已被修改'})
                        return
                    project_model.update(body)
                    project_model['revision'] = 'd' * 64
                    req.fulfill(json=deepcopy(project_model))
                else:
                    req.fulfill(json=deepcopy(project_model))
                return
            if path == '/members':
                req.fulfill(json={'members': [member]})
                return
            if path == '/projects':
                req.fulfill(json={'projects': [project]})
                return
            if path == '/projects/p/workspaces':
                req.fulfill(json={'project': project, 'members': [member], 'history': []})
                return
            req.fulfill(json={'projects': [], 'members': [], 'messages': [], 'threads': [],
                              'tasks': [], 'requirements': [], 'commands': [],
                              'roles': [], 'state': {}, 'entries': []})

        page.route('**/*', route)
        page.goto('http://dsh-config.test/app/index.html')
        page.evaluate("""([p, m]) => {
            S.projects=[p]; S.members=[m]; S.currentProject=p.name;
            showLogin(false); renderProjectBar();
        }""", [project, member])

        def open_editor(project_name=None):
            page.evaluate('(args) => openProfileEditor(args.member, args.project)',
                          {'member': member, 'project': project_name})
            page.wait_for_function('profileEditor?.revision != null')

        def close_editor():
            page.locator('#profile-cancel').click()
            page.wait_for_function('profileEditor === null')

        # DSH fields, tabs and fixed effort levels.
        open_editor()
        assert page.locator('#profile-tabs [role=tab]:visible').count() == 6
        assert page.locator('#profile-tab-soul').inner_text() == '人格'
        assert page.locator('#profile-tab-instructions').is_visible()
        assert page.locator('#profile-tab-env').is_visible()
        assert page.locator('#profile-tab-model').inner_text() == '模型'
        assert page.locator('#profile-tab-auto').inner_text() == 'Auto'
        assert '.group-engine-description' in page.locator('#profile-panel-description .profile-help').inner_text()
        assert 'personaPrefix' in page.locator('#profile-panel-soul .profile-help').inner_text()
        assert 'AGENTS.md' in page.locator('#profile-panel-instructions .profile-help').inner_text()
        assert 'agent-default-model' in page.locator('#profile-panel-model .profile-help').first.inner_text()
        assert '.group-engine-auto-review' in page.locator('#profile-auto-help').inner_text()
        assert page.locator('#profile-soul').input_value() == config['persona']
        assert page.locator('#profile-instructions').input_value() == config['instructions']
        assert page.locator('#profile-env').input_value() == config['env']
        assert not page.locator('#profile-auto-review').is_checked()
        assert page.locator('#profile-auto-text').inner_text() == '已关闭'
        choices = page.locator('#profile-reasoning_effort option').evaluate_all(
            '(options) => options.map(o => o.value)')
        assert choices == ['', 'minimal', 'low', 'medium', 'high', 'max', 'ultra'], choices

        # All seven fields save through the dsh field names (persona/instructions, no soul).
        page.locator('#profile-description').fill('新的 DSH 描述')
        page.locator('#profile-tab-soul').click()
        page.locator('#profile-soul').fill('新的 DSH 人格')
        page.locator('#profile-tab-instructions').click()
        page.locator('#profile-instructions').fill('新的指令')
        page.locator('#profile-tab-env').click()
        page.locator('#profile-env').fill('DEEPSEEK_API_KEY=new\n')
        page.locator('#profile-tab-model').click()
        page.locator('#profile-model').fill('deepseek-reasoner')
        page.locator('#profile-reasoning_effort').select_option('high')
        page.locator('#profile-tab-auto').click()
        page.locator('.profile-switch-track').click()
        assert page.locator('#profile-auto-review').is_checked()
        assert page.locator('#profile-auto-text').inner_text() == '已开启'
        page.locator('#profile-save').click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        path, body = writes[-1]
        assert path == '/members/bot-dsh/config'
        assert body['member_profile'] == 'bot-dsh'
        assert body['description'] == '新的 DSH 描述'
        assert body['persona'] == '新的 DSH 人格'
        assert body['instructions'] == '新的指令'
        assert body['env'].replace('\r\n', '\n') == 'DEEPSEEK_API_KEY=new\n'
        assert body['model'] == 'deepseek-reasoner'
        assert body['provider'] == 'deepseek-account'
        assert body['reasoning_effort'] == 'high'
        assert body['auto_review'] == 'on'
        assert 'soul' not in body
        assert 'config_path' not in body
        page.wait_for_function('profileEditor === null')

        # Auto readback: on is checked after reload; turning it off writes off.
        open_editor()
        page.locator('#profile-tab-auto').click()
        assert page.locator('#profile-auto-review').is_checked()
        assert page.locator('#profile-auto-text').inner_text() == '已开启'
        page.locator('.profile-switch-track').click()
        assert not page.locator('#profile-auto-review').is_checked()
        page.locator('#profile-save').click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        off_body = writes[-1][1]
        assert off_body['auto_review'] == 'off'
        page.wait_for_function('profileEditor === null')

        # Project scope: model fields go to the project; persona/instructions stay global.
        state['model_conflict'] = True
        open_editor('p')
        assert '模型与推理应用到全局' in page.locator('#profile-global-text').inner_text()
        page.locator('#profile-description').fill('全局描述已保存')
        page.locator('#profile-tab-model').click()
        assert '人格、指令、描述、.env 和 Auto 始终写入全局 profile' in page.locator('#profile-model-scope').inner_text()
        page.locator('#profile-model').fill('project-model')
        page.locator('#profile-save').click()
        page.wait_for_function('document.querySelector("#profile-status").textContent.includes("已保存全局字段")')
        status = page.locator('#profile-status').inner_text()
        assert '描述' in status and '项目模型保存失败' in status, status
        assert page.locator('#dlg-profile').is_visible()
        assert page.locator('#profile-save').is_enabled()
        global_write = next(w for w in writes if w[0] == '/members/bot-dsh/config')
        assert global_write[1]['model'] == config['model']  # project model didn't leak into global
        state['model_conflict'] = False
        page.locator('#profile-save').click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        model_write = next(w for w in writes if w[0].endswith('/model'))
        assert model_write[1]['model'] == 'project-model'
        page.wait_for_function('profileEditor === null')

        # Revision conflict: no field is reported saved.
        state['config_conflict'] = True
        open_editor()
        page.locator('#profile-description').fill('冲突描述')
        page.locator('#profile-save').click()
        page.wait_for_function('document.querySelector("#profile-status").textContent.includes("未保存任何字段")')
        assert page.locator('#dlg-profile').is_visible()
        assert page.locator('#profile-description').is_enabled()
        assert page.locator('#profile-save').is_enabled()
        close_editor()
        state['config_conflict'] = False

        # Plugin missing/disabled/load failure: only the dependent fields are disabled.
        if real_plugins:
            state['plugins'] = dict(real_plugins)
        else:
            state['plugins'] = {
                'persona': {'state': 'disabled', 'plugin': 'system-prompt',
                            'reason': '插件已禁用', 'path': 'cordis.patch.yml#system-prompt'},
                'instructions': {'state': 'missing', 'plugin': 'dsh-agent-instructions',
                                 'reason': '未安装', 'path': 'cordis.patch.yml#dsh-agent-instructions'},
                'model': {'state': 'load_failed', 'plugin': 'agent-default-model',
                          'reason': '加载失败', 'path': 'cordis.patch.yml#agent-default-model'},
                'provider': {'state': 'load_failed', 'plugin': 'agent-default-model',
                             'reason': '加载失败', 'path': 'cordis.patch.yml#agent-default-model'},
                'reasoning_effort': {'state': 'load_failed', 'plugin': 'agent-default-model',
                                     'reason': '加载失败', 'path': 'cordis.patch.yml#agent-default-model'},
                'auto_review': {'state': 'disabled', 'plugin': 'auto-review',
                                'reason': 'auto-review 插件已禁用', 'path': 'cordis.patch.yml#auto-review'},
            }
        open_editor()
        plugin_status = page.locator('#profile-plugin-status')
        assert plugin_status.is_visible()
        text = plugin_status.inner_text()
        assert '人格' in text and '插件已禁用' in text and 'system-prompt' in text
        assert '模型' in text and '加载失败' in text and 'agent-default-model' in text
        assert page.locator('#profile-soul').is_disabled()
        assert page.locator('#profile-model').is_disabled()
        assert page.locator('#profile-provider').is_disabled()
        assert page.locator('#profile-reasoning_effort').is_disabled()
        assert page.locator('#profile-description').is_enabled()
        assert page.locator('#profile-env').is_enabled()
        assert page.locator('#profile-save').is_enabled()
        if real_plugins:
            # 未覆盖的 instructions 继承 bundle 默认插件，状态为 ok，保持可编辑。
            assert '指令' not in text
            assert page.locator('#profile-instructions').is_enabled()
            assert 'Auto' not in text
            assert page.locator('#profile-auto-review').is_enabled()
        else:
            assert '指令' in text and '未安装' in text and 'dsh-agent-instructions' in text
            assert page.locator('#profile-instructions').is_disabled()
            assert 'Auto 审批' in text and 'auto-review' in text
            assert page.locator('#profile-auto-review').is_disabled()
        page.locator('#profile-description').fill('插件缺失下仍可保存描述')
        page.locator('#profile-tab-auto').click()
        page.screenshot(path=str(evidence / 'dsh-config-plugins-1280-dark.png'))
        page.evaluate("applyTheme('light')")
        page.screenshot(path=str(evidence / 'dsh-config-plugins-1280-light.png'))
        page.evaluate("applyTheme('dark')")
        page.locator('#profile-save').click()
        page.wait_for_function('!document.querySelector("#dlg-profile").open')
        plugin_write = writes[-1]
        assert plugin_write[1]['persona'] == config['persona']
        assert plugin_write[1]['instructions'] == config['instructions']
        assert plugin_write[1]['provider'] == config['provider']
        assert plugin_write[1]['reasoning_effort'] == config['reasoning_effort']
        assert plugin_write[1]['auto_review'] == config['auto_review']
        assert plugin_write[1]['description'] == '插件缺失下仍可保存描述'
        page.wait_for_function('profileEditor === null')
        state['plugins'] = {}

        # 390px, both themes, no horizontal overflow.
        page.set_viewport_size({'width': 390, 'height': 844})
        open_editor()
        box = page.locator('#dlg-profile').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= 390
        assert page.locator('#profile-tabs [role=tab]:visible').count() == 6
        overflow = page.locator('#profile-tabs').evaluate('(el) => el.scrollWidth - el.clientWidth')
        assert overflow <= 0, overflow
        page.locator('#profile-tab-auto').click()
        assert page.locator('.profile-switch-track').is_visible()
        assert page.locator('#profile-auto-text').inner_text() in ('已关闭', '已开启')
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            page.screenshot(path=str(evidence / f'dsh-config-390-{theme}.png'))
            page.screenshot(path=str(evidence / f'dsh-config-auto-390-{theme}.png'))
        page.evaluate("applyTheme('dark')")
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.screenshot(path=str(evidence / 'dsh-config-1280-dark.png'))
        close_editor()

        assert not errors, errors
        browser.close()
    print('PASS: DSH member config fields, persona/instructions mapping, Auto on/off and plugin gate, '
          'project scope, partial failure, revision conflict, plugin status and 390px themes')


if __name__ == '__main__':
    main()
