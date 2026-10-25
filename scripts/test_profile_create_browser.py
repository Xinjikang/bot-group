"""Shared member selection and standalone creation with isolated real HTTP/SQLite/Git."""
import argparse
import mimetypes
import os
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_import import ImportTests

ROOT = Path(__file__).resolve().parents[1]


def check_member_cards(page, container, output, name):
    for theme in ('dark', 'light'):
        for width in (1280, 390):
            page.evaluate('applyTheme', theme)
            page.set_viewport_size({'width': width, 'height': 900})
            cards = container.locator('.import-member')
            assert cards.count() > 0
            assert cards.evaluate_all('''rows => rows.every(row => {
                const name = row.querySelector('strong').getBoundingClientRect();
                const hint = row.querySelector('small').getBoundingClientRect();
                const style = getComputedStyle(row);
                return hint.top >= name.bottom && Math.abs(hint.left - name.left) < 1
                    && style.borderRadius === '12px' && style.borderTopWidth === '1px'
                    && row.scrollWidth <= row.clientWidth;
            })''')
            container.screenshot(path=str(output / f'{name}-{width}-{theme}.png'))
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.evaluate('applyTheme', 'dark')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    output = parser.parse_args().evidence
    output.mkdir(parents=True, exist_ok=True)
    fixture = ImportTests(); fixture.setUp()
    errors, creates, enrollments = [], [], []
    state = {'fail': True, 'lose': False, 'join_fail': False}
    host = fixture.base / 'host'; host.mkdir()
    (host / 'config.yaml').write_text('model:\n  default: base-model\n')
    (host / '.env').write_text('OPENAI_API_KEY=synthetic-key\n')
    from scripts.test_profile_credentials import seed_catalog
    seed_catalog(host)
    codex = fixture.base / 'codex'
    (codex / 'config.toml').write_text('model="test-model"\n')
    (codex / 'auth.json').write_text('{"OPENAI_API_KEY":"synthetic-key"}')
    try:
        with patch.dict(os.environ, {'CODEX_HOME': str(codex)}), sync_playwright() as pw:
            project = fixture.submit().json()['project']
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route(req):
                path = urlparse(req.request.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                    return
                if path == '/profiles/create':
                    creates.append(req.request.post_data_json)
                    if state['fail']:
                        req.fulfill(status=503, json={'detail': '模拟创建失败'})
                        return
                if path == '/bots/register' and req.request.post_data_json.get('project'):
                    enrollments.append(req.request.post_data_json)
                    if state['join_fail'] and req.request.post_data_json['profile'] == 'new-codex':
                        req.fulfill(status=503, json={'detail': '模拟入组失败'})
                        return
                if path.startswith(('/projects', '/profiles', '/discovery', '/bots')) or path in ('/members', '/messages', '/auth/status', '/auth/events', '/user/display-name'):
                    response = fixture.client.request(req.request.method, req.request.url,
                        json=req.request.post_data_json if req.request.method in ('POST', 'PUT', 'PATCH') else None)
                    if path == '/profiles/create' and state['lose'] and response.status_code == 200:
                        state['lose'] = False
                        req.fulfill(status=503, json={'detail': '模拟已创建后丢失响应'})
                        return
                    req.fulfill(status=response.status_code, body=response.content, headers=dict(response.headers))
                else:
                    req.fulfill(json={'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'pending_approval': [], 'status': 'unavailable'})
            page.route('**/*', route)
            page.goto('http://profiles.test/app/')
            page.wait_for_function('S.projects.length > 0')
            page.evaluate('p => { openProjectDetail(p, document.querySelector("#chip-all")); selectProjectTab("members"); }', project)
            page.locator('#pj-members-add').click()
            candidates = page.locator('#member-picker-list')
            candidates.locator('input[data-profile]').first.wait_for()
            for bot_id in fixture.ids:
                assert candidates.locator(f'input[data-bot-id="{bot_id}"]').count() == 0
            page.locator('#dlg-member-picker').screenshot(path=str(output / 'unjoined-members.png'))
            check_member_cards(page, candidates, output, 'picker-cards')
            existing = candidates.locator('input[data-host="hermes"][data-profile="default"]')
            existing.check()
            page.wait_for_function('!document.querySelector("#member-picker-join").disabled')
            with page.expect_response(lambda response: '/profiles/candidates?' in response.url):
                candidates.locator('.member-picker-refresh').click()
            page.wait_for_function('!document.querySelector("#member-picker-list .member-picker-refresh").disabled')
            assert existing.is_checked()
            for bot_id in fixture.ids:
                assert candidates.locator(f'input[data-bot-id="{bot_id}"]').count() == 0
            assert not enrollments
            candidates.locator('.member-picker-create').click()
            assert '会保留' in page.locator('#dlg-member-add').inner_text()
            assert page.locator('#member-add-next').count() == 0
            page.locator('[name="member-add-host"][value="codex"]').check()
            page.wait_for_function('document.querySelectorAll("#member-create-source option").length > 1')
            assert page.locator('#member-create-auth').count() == 0
            assert '加入项目时' in page.locator('#member-create-auth-help').inner_text()
            profile = page.locator('#member-create-profile')
            for value in ('测试', 'Upper', '_worker', 'default', 'con'):
                profile.fill(value)
                assert profile.get_attribute('aria-invalid') == 'true'
                assert page.locator('#member-add-join').is_disabled()
            profile.fill('new-codex')
            page.locator('#member-create-name').fill('新建成员')
            page.locator('.member-create-fields summary').click()
            page.locator('#member-create-instructions').fill('核对证据。')
            page.locator('#member-add-join').click()
            page.get_by_text('创建失败：模拟创建失败。填写内容已保留，可修改后重试。', exact=True).wait_for()
            assert page.locator('#member-create-instructions').input_value() == '核对证据。'
            state.update(fail=False, lose=True)
            page.locator('#member-add-join').click()
            page.wait_for_function('document.querySelector("#member-add-status").textContent.includes("丢失响应")')
            assert (codex / 'profiles/new-codex').is_dir()
            count = fixture.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0]
            for theme in ('light', 'dark'):
                page.evaluate('applyTheme', theme); page.set_viewport_size({'width': 390, 'height': 844})
                assert page.locator('#dlg-member-add').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                page.locator('#dlg-member-add').screenshot(path=str(output / f'profile-create-390-{theme}.png'))
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.locator('#member-add-join').click()
            page.locator('#dlg-member-add').wait_for(state='hidden')
            assert creates[0] == creates[1] == creates[2]
            assert 'project' not in creates[0] and 'reuse_auth' not in creates[0]
            assert candidates.locator('input[data-profile="new-codex"]').is_checked()
            assert existing.is_checked()
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0] == count
            page.locator('#member-picker-close').click()
            assert (codex / 'profiles/new-codex').is_dir()
            page.locator('#pj-members-add').click()
            candidates.locator('input[data-host="hermes"][data-profile="default"]').check()
            candidates.locator('input[data-profile="new-codex"]').check()
            state['join_fail'] = True
            page.locator('#member-picker-join').click()
            page.wait_for_function('document.querySelector("#member-picker-status").textContent.includes("模拟入组失败")')
            assert candidates.locator('input[data-host="hermes"][data-profile="default"]').count() == 0
            assert candidates.locator('input[data-profile="new-codex"]').is_checked()
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0] == count + 1
            state['join_fail'] = False
            page.locator('#member-picker-join').click()
            page.locator('#dlg-member-picker').wait_for(state='hidden')
            assert [item['profile'] for item in enrollments] == ['default', 'new-codex', 'new-codex']
            count += 2
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0] == count

            # Create a project through the same picker and enroll only at final confirmation.
            page.evaluate('closeDialogAnchored(document.querySelector("#dlg-project-detail")); openProjectNew()')
            page.locator('#project-entry-new').click()
            fresh = fixture.base / 'new-project'
            page.locator('#import-root').fill(str(fresh))
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            page.locator('#import-next').click()
            page.locator('#import-role-step').wait_for(state='visible')
            page.locator('#import-role-name').fill('临时测试角色')
            page.locator('#import-role-add').click()
            removable = page.locator('#import-roles .import-card').filter(has_text='临时测试角色')
            removable.get_by_role('button', name='移除角色').click()
            assert '根据调整后的角色分工修改协作流程' in page.locator('#app-alert-message').inner_text()
            assert removable.count() == 1
            page.locator('#app-alert').get_by_role('button', name='取消', exact=True).click()
            assert removable.count() == 1
            removable.get_by_role('button', name='移除角色').click()
            page.get_by_role('button', name='确定', exact=True).click()
            removable.wait_for(state='detached')
            assert removable.count() == 0
            page.locator('#import-next').click()
            page.locator('#import-agents-step').wait_for(state='visible')
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            wizard = page.locator('#import-members')
            check_member_cards(page, wizard, output, 'wizard-cards')
            wizard.locator(f'input[data-bot-id="{fixture.ids[0]}"]').check()
            wizard.locator('select[aria-label="成员角色"]').select_option('项目负责人')
            wizard.locator('.member-picker-create').click()
            page.wait_for_function('!memberAdd.scanning')
            assert page.locator('#member-create-auth').count() == 0
            assert '主 profile' in page.locator('#member-create-auth-help').inner_text()
            page.locator('#member-create-source').select_option('')
            page.locator('#member-create-profile').fill('wizard-hermes')
            page.locator('.profile-soul-template-open').last.click()
            page.locator('#profile-soul-template-apply').wait_for(state='visible')
            page.wait_for_function('!document.querySelector("#profile-soul-template-apply").disabled')
            page.locator('#profile-soul-template-apply').click()
            assert page.locator('#member-create-instructions').input_value()
            page.locator('#member-add-join').click()
            page.locator('#dlg-member-add').wait_for(state='hidden')
            assert wizard.locator('input[data-profile="wizard-hermes"]').is_checked()
            assert wizard.locator(f'input[data-bot-id="{fixture.ids[0]}"]').is_checked()
            assert wizard.locator('select[aria-label="成员角色"]').first.input_value() == '项目负责人'
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0] == count
            wizard.locator('input[data-profile="wizard-hermes"]').uncheck()
            assert (host / 'profiles/wizard-hermes').is_dir()
            wizard.locator('input[data-profile="wizard-hermes"]').check()
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            page.locator('#import-submit').click()
            page.wait_for_function('p => S.currentProject && S.currentProject !== p', arg=project)
            new_project = page.evaluate('S.currentProject')
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_members WHERE project=?', (new_project,)).fetchone()[0] == 2
            assert (fresh / 'AGENTS.md').is_file()
            from engine import execution_errors
            execution_errors.report('wizard-hermes', new_project, None, 'browser-failure',
                                    "No usable credentials found for provider 'ollama-cloud'. Set OLLAMA_API_KEY.")
            page.reload()
            page.wait_for_function('p => S.projects.some(x => x.name === p)', arg=new_project)
            page.evaluate('p => selectProject(p)', new_project)
            notification = page.locator('.msg-content').filter(has_text='配置不完整或无效，请检查配置。')
            notification.wait_for()
            assert 'OLLAMA' not in notification.inner_text()
            for theme in ('dark', 'light'):
                page.evaluate('applyTheme', theme)
                page.set_viewport_size({'width': 1280, 'height': 900})
                assert notification.bounding_box()['width'] > 250
                notification.screenshot(path=str(output / f'error-{theme}.png'))
            assert not errors, errors
            browser.close()
        print('PASS: shared list, registered/scanned candidates, standalone Codex/Hermes, validation, failure drafts, lost-response idempotency, cancellation retention, selection preservation, SOUL, deferred enrollment/authorization, real project creation, reload, 390px themes')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
