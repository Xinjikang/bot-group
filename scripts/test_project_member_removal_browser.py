"""Real Chrome member menus and removal UI with isolated HTTP fixtures."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(output):
    project = {'name': 'p', 'display_name': '测试项目', 'root': 'E:/sample', 'directory_mode': 'shared'}
    member = {'profile': 'dev', 'host_profile': 'dev', 'bot_id': 9, 'display_name': '开发成员',
              'active': 1, 'role': '开发', 'runtime': 'codex', 'host_type': 'codex',
              'work_session_exists': True, 'directory_mode': 'shared',
              'workspace': {'path': 'E:/sample', 'valid': True}}
    state = {'removed': False, 'blockers': [], 'race': False, 'fail': False, 'deletes': 0, 'resets': 0}
    errors = []

    def route(req):
        path, method = urlparse(req.request.url).path, req.request.method
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
            data = {'members': [{**member, 'active': 0 if state['removed'] else 1}]}
        elif path == '/projects/p/workspaces':
            data = {'project': project, 'members': [] if state['removed'] else [member]}
        elif path == '/projects/p/roles':
            data = {'roles': [{'id': 1, 'name': '开发', 'member_count': int(not state['removed'])}]}
        elif path.endswith('/dev/removal'):
            data = {'can_remove': not state['blockers'], 'blockers': state['blockers']}
        elif path == '/projects/p/members/dev' and method == 'DELETE':
            state['deletes'] += 1
            if state['race']:
                req.fulfill(status=409, json={'detail': {'message': '新增待交接任务',
                            'blockers': [{'kind': 'task', 'id': 12, 'title': '任务 #12：需要交接'}]}})
                return
            if state['fail']:
                req.fulfill(status=503, json={'detail': '临时服务错误'})
                return
            state['removed'] = True
            data = {'removed': True}
        elif path.endswith('/dev/reset-session'):
            state['resets'] += 1
            data = {'handled': True, 'ok': True, 'detail': '会话已重置'}
        elif path == '/profiles/candidates':
            data = {'candidates': [{**member, 'already_project_member': not state['removed']}]}
        elif path == '/bots/register':
            state['removed'] = False
            data = {'bot_id': 9}
        elif path == '/user/display-name':
            data = {'profile': 'user', 'display_name': '测试用户'}
        elif path == '/tasks':
            data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        elif path == '/threads':
            data = {'threads': []}
        elif path == '/messages':
            data = {'messages': []}
        elif path == '/system/updates':
            data = {'status': 'unavailable', 'update_available': False}
        elif path == '/appearance/background':
            req.fulfill(status=204, headers={'X-Background-Configured': 'true'})
            return
        req.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://member-remove.test/app/')
        page.locator('[data-project="p"]').click()
        page.locator('#pj-tab-members').click()
        card = page.locator('#pj-member-list .project-member-card').first
        more = card.locator('.member-more')
        menu = card.locator('.project-member-menu')
        more.wait_for()
        assert card.locator('.workspace').is_visible()
        assert card.get_by_role('switch').is_visible()
        assert card.locator('.edit').is_hidden()
        for theme in ('dark', 'light'):
            for width in (1280, 390):
                page.evaluate('applyTheme', theme)
                page.set_viewport_size({'width': width, 'height': 844})
                page.evaluate('anchorDialog(document.querySelector("#dlg-project-detail"), document.querySelector("#chip-all"))')
                more.click()
                menu.wait_for(state='visible')
                rect = menu.bounding_box()
                assert 0 <= rect['x'] and rect['x'] + rect['width'] <= width
                assert 0 <= rect['y'] and rect['y'] + rect['height'] <= 844
                assert menu.get_by_role('menuitem').all_text_contents() == ['编辑 profile', '重置会话', '移出项目']
                assert '共用此 profile' in menu.locator('.edit').get_attribute('title')
                page.screenshot(path=str(output / f'member-menu-{width}-{theme}.png'))
                page.keyboard.press('End')
                assert menu.locator('.member-remove').evaluate('el => el === document.activeElement')
                page.keyboard.press('Escape')
                assert menu.is_hidden()
                assert more.get_attribute('aria-expanded') == 'false'
        more.click()
        menu.locator('.reset-session').click()
        page.get_by_text('会话已重置', exact=True).wait_for()
        assert state['resets'] == 1

        def open_removal():
            more.click()
            menu.locator('.member-remove').click()
            page.locator('#dlg-member-remove .recheck').wait_for()
            page.wait_for_function('!document.querySelector("#dlg-member-remove .recheck").disabled')
            return page.locator('#dlg-member-remove')

        dialog = open_removal()
        assert dialog.locator('.confirm').is_enabled()
        assert dialog.locator('.cancel').evaluate('el => el === document.activeElement')
        dialog.locator('.cancel').click()
        assert state['deletes'] == 0 and card.is_visible()
        state['blockers'] = [{'kind': 'owner', 'title': '请先更换项目负责人'},
                             {'kind': 'task', 'id': 8, 'title': '任务 #8：待交接'},
                             {'kind': 'runtime', 'title': '运行中'}]
        dialog = open_removal()
        assert dialog.locator('.confirm').is_disabled()
        assert dialog.locator('.member-remove-blocker').count() == 3
        dialog.screenshot(path=str(output / 'member-removal-blocked-390.png'))
        dialog.get_by_role('button', name='更换负责人').click()
        assert page.locator('#pj-d-owner').is_visible()
        page.locator('#pj-tab-members').click()
        state['blockers'] = []
        state['race'] = True
        dialog = open_removal()
        dialog.locator('.confirm').click()
        dialog.get_by_text('任务 #12：需要交接', exact=True).wait_for()
        assert card.is_visible() and dialog.locator('.confirm').is_disabled()
        state['race'] = False
        state['fail'] = True
        dialog.locator('.recheck').click()
        page.wait_for_function('!document.querySelector("#dlg-member-remove .confirm").disabled')
        dialog.locator('.confirm').click()
        dialog.get_by_text('移出失败：临时服务错误', exact=True).wait_for()
        assert card.is_visible()
        state['fail'] = False
        dialog.locator('.recheck').click()
        page.wait_for_function('!document.querySelector("#dlg-member-remove .confirm").disabled')
        dialog.screenshot(path=str(output / 'member-removal-confirm-390.png'))
        dialog.locator('.confirm').click()
        dialog.wait_for(state='detached')
        page.wait_for_function('document.querySelectorAll("#pj-member-list .project-member-card").length === 0')
        assert state['removed']
        assert page.locator('#ms-options input[value="dev"]').count() == 0
        assert page.evaluate('projectMembersCache.get("p").members.length') == 0
        page.reload()
        page.locator('[data-project="p"]').click()
        page.locator('#pj-tab-members').click()
        page.wait_for_function('document.querySelector("#pj-members-status").textContent === "状态已更新"')
        assert page.locator('#pj-member-list .project-member-card').count() == 0
        page.locator('#pj-members-add').click()
        candidate = page.locator('#member-picker-list input[data-profile="dev"]')
        candidate.check()
        page.locator('#member-picker-join').click()
        page.locator('#dlg-member-picker').wait_for(state='hidden')
        page.locator('#pj-member-list .project-member-card').wait_for()
        assert not state['removed']
        assert not errors, errors
        browser.close()
    print('PASS: menu themes/viewport/keyboard, reset, cancel, blockers, stale preview, failure/retry, removal, reload, re-enrollment')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
