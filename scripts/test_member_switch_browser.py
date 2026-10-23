"""Isolated member switch and role warning interactions in real Chrome."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    project = {'name': 'p', 'display_name': '测试项目', 'root': 'E:/sample', 'directory_mode': 'shared'}
    member = {'profile': 'dev', 'display_name': '开发成员', 'active': 1, 'role': '开发', 'directory_mode': 'shared',
              'workspace': {'path': 'E:/sample', 'valid': True}}
    roles = [{'id': 1, 'name': '开发', 'member_count': 1}, {'id': 2, 'name': '审查', 'member_count': 0}]
    state = {'fail': False, 'held': [], 'hold': False, 'posts': 0, 'deletes': 0}
    errors = []
    def route(req):
        path = urlparse(req.request.url).path
        method = req.request.method
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            return
        data = {}
        if path == '/auth/status':
            data = {'configured': False}
        elif path == '/projects':
            data = {'projects': [project]}
        elif path == '/members':
            data = {'members': [member]}
        elif path == '/projects/p/workspaces':
            data = {'project': project, 'members': [member]}
        elif path == '/projects/p/roles':
            data = {'roles': roles}
        elif path.startswith('/projects/p/roles/'):
            role = next(r for r in roles if r['id'] == int(path.rsplit('/', 1)[1]))
            if method == 'DELETE':
                state['deletes'] += 1
                roles.remove(role)
            elif method == 'PATCH':
                role['name'] = req.request.post_data_json['name']
            data = role
        elif path == '/members/dev/active':
            state['posts'] += 1
            if state['fail']:
                req.fulfill(status=503, json={'detail': '测试：启停保存失败'})
                return
            member['active'] = int(req.request.post_data_json['active'])
            if state['hold']:
                state['held'].append(req)
                return
        elif path == '/projects/p/permissions':
            data = {'project': 'p', 'revision': 'v1', 'permissions': [{'key': 'task.create', 'label': '创建任务'}],
                    'roles': roles, 'members': [member], 'grants': {'task.create': ['role:1']}}
        elif path == '/user/display-name':
            data = {'profile': 'user', 'display_name': '测试用户'}
        elif path == '/system/updates':
            data = {'status': 'unavailable', 'update_available': False}
        elif path == '/tasks':
            data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        elif path == '/threads':
            data = {'threads': []}
        elif path == '/messages':
            data = {'messages': []}
        elif path == '/appearance/background':
            req.fulfill(status=204, headers={'X-Background-Configured': 'true'})
            return
        req.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://members.test/app/')
        page.locator('[data-project="p"]').click()
        page.locator('#pj-tab-members').click()
        switch = page.get_by_role('switch', name='成员启用状态')
        switch.wait_for()
        assert switch.inner_text() == '启用' and switch.get_attribute('aria-checked') == 'true'
        state['hold'] = True
        switch.click()
        page.wait_for_timeout(100)
        assert switch.is_disabled() and state['posts'] == 1
        state['held'].pop().fulfill(json={'active': False})
        state['hold'] = False
        page.wait_for_function('document.querySelector(".project-member-badge").textContent === "停用"')
        assert switch.get_attribute('aria-checked') == 'false'
        assert page.evaluate('projectMembersCache.get("p").members[0].active') == 0
        state['fail'] = True
        switch.click()
        page.get_by_text('测试：启停保存失败', exact=True).wait_for()
        assert switch.inner_text() == '停用' and switch.is_enabled()
        assert switch.get_attribute('aria-checked') == 'false'
        state['fail'] = False
        switch.focus()
        page.keyboard.press('Space')
        page.wait_for_function('document.querySelector(".project-member-badge").textContent === "启用"')
        for theme in ('light', 'dark'):
            page.evaluate('applyTheme', theme)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.evaluate('anchorDialog(document.querySelector("#dlg-project-detail"), document.querySelector("#chip-all"))')
            rect = switch.bounding_box()
            assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 390
            page.locator('#dlg-project-detail').screenshot(path=str(evidence / f'member-switch-390-{theme}.png'))
        page.locator('#pj-tab-roles').click()
        assert page.locator('.pj-role-row').first.get_by_role('button', name='删除', exact=True).is_disabled()
        page.locator('.pj-role-row').last.get_by_role('button', name='删除', exact=True).click()
        assert '派工、审查、验收' in page.locator('#app-alert-message').inner_text()
        assert 'AGENTS.md 中根据调整后的角色分工修改协作流程' in page.locator('#app-alert-message').inner_text()
        assert '重新明确各项职责的承担者' in page.locator('#app-alert-message').inner_text()
        assert state['deletes'] == 0 and len(roles) == 2
        page.locator('#app-alert').screenshot(path=str(evidence / 'role-delete-confirm-390.png'))
        page.locator('#app-alert').get_by_role('button', name='取消', exact=True).click()
        assert len(roles) == 2 and state['deletes'] == 0
        page.locator('.pj-role-row').last.get_by_role('button', name='删除', exact=True).click()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelectorAll(".pj-role-row").length === 1')
        assert state['deletes'] == 1
        page.locator('.pj-role-row').first.get_by_role('button', name='改名', exact=True).click()
        assert '协作正文' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='确定', exact=True).click()
        page.locator('.pj-role-rename').fill('实现')
        page.locator('.pj-role-rename').press('Enter')
        page.wait_for_function('document.querySelector("#pj-role-status").textContent.includes("角色已改名")')
        assert roles[0]['name'] == '实现'
        page.locator('#pj-tab-permissions').click()
        page.locator('#pj-permissions-list summary').click()
        page.locator('#pj-permissions-list input[value="role:1"]').uncheck()
        assert '有权限的执行人' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='确定', exact=True).click()
        assert not page.locator('#pj-permissions-list input[value="role:1"]').is_checked()
        assert not errors, errors
        browser.close()
    print('PASS: switch pending/disabled, cache, failure recovery, keyboard, 390px themes; role delete/cancel/rename/permission notices and existing member-count guard')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
