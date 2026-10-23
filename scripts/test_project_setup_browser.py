"""Real Chrome and isolated HTTP/SQLite/Git for the shared seven-step wizard."""
import mimetypes
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_import import ImportTests
from engine import project_setup_git as git

ROOT = Path(__file__).resolve().parents[1]


def main():
    fixture = ImportTests()
    fixture.setUp()
    output = ROOT / 'cache' / 'project-setup-browser'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            state = {'lost': False, 'submits': 0}
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route(request):
                req = request.request
                path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                elif path in ('/projects', '/members', '/profiles/candidates') or path.startswith('/projects/') or '/workspaces' in path:
                    response = fixture.client.request(req.method, req.url, json=req.post_data_json if req.method in ('POST', 'PUT') else None)
                    if path == '/projects/import':
                        state['submits'] += 1
                        if state['lost'] and response.status_code == 200:
                            request.fulfill(status=503, json={'detail': 'lost response after commit'})
                            return
                    request.fulfill(status=response.status_code, json=response.json())
                else:
                    request.fulfill(json={'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'pending_approval': []})

            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            page.evaluate('showLogin(false); S.projects=[]; S.members=[]; renderProjectBar(); openProjectNew();')
            page.locator('#project-entry-new').click()
            assert page.locator('#import-title').inner_text() == '新建项目'
            page.locator('#import-root').fill(str(fixture.root))
            page.locator('#import-preview').click()
            page.wait_for_function("document.querySelector('#import-status').textContent.includes('不能在已有项目上新建')")
            assert '第 1 步' in page.locator('#import-step').inner_text()

            fresh = fixture.base / 'created-project'
            page.locator('#import-root').fill(str(fresh))
            assert page.locator('#import-name').input_value() == 'created-project'
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            assert not fresh.exists(), 'Preview must not create directories'
            supplement = page.locator('#import-supplement')
            if supplement.is_visible():
                supplement.hover()
                assert supplement.evaluate('n => getComputedStyle(n).cursor') != 'wait', 'Idle mandatory checkbox shows an endless wait cursor'
            assert supplement.is_hidden(), 'New projects describe required files without a disabled checkbox'
            page.locator('#import-mode').select_option('worktree')
            assert page.locator('#import-baseline').is_hidden()
            assert '创建首次提交' in page.locator('#import-baseline-initial').inner_text()
            page.locator('#import-next').click()
            page.locator('#import-role-step').wait_for(state='visible')
            page.locator('#import-back').click()
            assert page.get_by_role('checkbox', name='提交 AGENTS.md', exact=True).is_checked()
            page.locator('#dlg-project-import').screenshot(path=str(output / 'new-git.png'))
            page.locator('#import-next').click()
            assert '第 3 步' in page.locator('#import-step').inner_text()
            page.locator('#import-role-name').fill('自定义角色')
            page.locator('body').click(position={'x': 2, 'y': 2})
            assert page.locator('#dlg-project-import').is_visible()
            assert page.locator('#import-role-name').input_value() == '自定义角色'
            page.locator('#import-role-add').click()
            custom = page.locator('#import-roles .import-card').filter(has_text='自定义角色')
            custom.locator('summary').click()
            custom.locator('input[type=checkbox]').first.check()
            assert '派工、审查、验收' in page.locator('#app-alert-message').inner_text()
            page.get_by_role('button', name='确定', exact=True).click()
            custom.locator('input[type=checkbox]').first.uncheck()
            page.get_by_role('button', name='确定', exact=True).click()
            custom.get_by_role('button', name='移除角色').click()
            assert '角色绑定' in page.locator('#app-alert-message').inner_text()
            page.get_by_role('button', name='确定', exact=True).click()
            assert page.locator('#import-roles .import-card').filter(has_text='自定义角色').count() == 0
            page.locator('#import-role-name').fill('自定义角色')
            page.locator('#import-role-add').click()
            page.locator('#import-next').click()
            page.locator('#import-agents-step').wait_for(state='visible')
            page.locator('#import-agents-content').fill(page.locator('#import-agents-content').input_value() + '\nReviewed custom rule.\n')
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[0]}"]').check()
            page.locator('#import-members select').first.select_option('项目负责人')
            assert page.locator('input[data-owner-bot-id]').count() == 0
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            assert page.locator('#import-resources > details').count() == 1
            assert fixture.conn.execute('SELECT COUNT(*) FROM workspace_resources').fetchone()[0] == 0
            resource = page.locator('#import-resources')
            resource.get_by_label('服务名称', exact=True).first.fill('application')
            saved_port = resource.get_by_label('服务端口', exact=True).first.input_value()
            page.locator('#import-back').click()
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[1]}"]').check()
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            assert page.locator('#import-resources > details').count() == 2
            assert resource.get_by_label('服务名称', exact=True).first.input_value() == 'application'
            assert resource.get_by_label('服务端口', exact=True).first.input_value() == saved_port
            page.locator('#import-back').click()
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[1]}"]').uncheck()
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            assert page.locator('#import-resources > details').count() == 1
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => applyTheme(theme)', theme)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.locator('#dlg-project-import').evaluate('d => d.scrollWidth <= d.clientWidth')
                page.locator('#dlg-project-import').screenshot(path=str(output / f'resources-390-{theme}.png'))
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            assert 'Project Setup' in page.locator('#import-review').inner_text()
            role_summary = page.locator('#import-review p').filter(has_text='角色：自定义角色').first
            assert '角色权限：无管理权限' in role_summary.inner_text()
            assert '已选成员（0）：未分配成员' in role_summary.inner_text()
            owner_summary = page.locator('#import-review p').filter(has_text='角色：项目负责人').first
            assert '已选成员（1）：' in owner_summary.inner_text()
            assert '个人附加权限：无附加权限' in page.locator('#import-review').inner_text()
            assert '准备工作区' in page.locator('#import-review').inner_text()
            page.locator('#import-back').click()
            page.evaluate("document.querySelector('#import-submit').click()")
            assert state['submits'] == 0
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => applyTheme(theme)', theme)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.locator('#dlg-project-import').evaluate('d => d.scrollWidth <= d.clientWidth')
                footer = page.locator('.import-footer').bounding_box()
                assert footer['y'] + footer['height'] <= 844
                page.locator('#dlg-project-import').screenshot(path=str(output / f'new-review-390-{theme}.png'))
            state['lost'] = True
            page.evaluate("document.querySelector('#import-submit').click(); document.querySelector('#import-submit').click()")
            page.wait_for_function("document.querySelector('#import-status').textContent.includes('导入结果')")
            assert 'lost response' in page.locator('#import-status').inner_text(), page.locator('#import-status').inner_text()
            assert state['submits'] == 1
            assert 'Reviewed custom rule.' in git.checked(fresh, 'show', 'HEAD:AGENTS.md')
            assert git.checked(fresh, 'status', '--porcelain') == ''
            project = fixture.conn.execute('SELECT name FROM projects').fetchone()[0]
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_workspaces').fetchone()[0] == 1
            # Reopening starts fresh; a completed registration opens by directory.
            page.reload()
            page.evaluate('showLogin(false); openProjectInitialization()')
            assert page.locator('#import-resume').count() == 0
            assert page.locator('#dlg-project-initialization').count() == 0
            page.locator('#import-root').fill(str(fresh))
            page.locator('#import-preview').click()
            page.wait_for_function('p => S.currentProject === p', arg=project)
            assert fixture.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 1
            assert state['submits'] == 1

            if '--role-notices-only' in sys.argv:
                assert not errors, errors
                browser.close()
                print('PASS: role removal/permission notices, rule review, real isolated Git/SQLite submission, reload and 390px themes')
                return

            # Existing independent workspace uses the same editor, with persisted values.
            page.evaluate('''async project => {
              const data = await api(`/projects/${project}/workspaces`);
              projectMembersCache.set(project, data);
              openWorkspaceEditor(project, {...data.members[0], workspace_assigned: false});
            }''', project)
            assert page.locator('#workspace-resources').is_hidden()
            assert '创建工作区或使用已有工作区后' in page.locator('#workspace-resources-hint').inner_text()
            page.locator('#workspace-close').click()
            page.evaluate('''async project => {
              const data = await api(`/projects/${project}/workspaces`);
              projectMembersCache.set(project, data);
              openWorkspaceEditor(project, data.members[0]);
            }''', project)
            assert page.locator('#workspace-resources-hint').is_hidden()
            page.locator('#workspace-resources').click()
            editor_dialog = page.locator('dialog').filter(has=page.locator('h3', has_text='工作区资源配置'))
            editor_dialog.get_by_label('服务名称', exact=True).first.wait_for()
            assert editor_dialog.get_by_label('服务名称', exact=True).first.input_value() == 'application'
            editor_dialog.get_by_label('服务名称', exact=True).first.fill('application-updated')
            editor_dialog.get_by_role('button', name='保存资源配置').click()
            editor_dialog.get_by_role('status').filter(has_text='已保存').wait_for()
            editor_dialog.get_by_role('button', name='关闭', exact=True).click()
            page.locator('#workspace-close').click()
            stored = fixture.conn.execute('SELECT config FROM workspace_resources WHERE project=?', (project,)).fetchone()[0]
            assert 'application-updated' in stored

            # Repositories with a commit offer both baseline choices.
            baseline_root = fixture.base / 'baseline-existing'
            baseline_root.mkdir()
            git.checked(baseline_root, 'init')
            git.checked(baseline_root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                        'commit', '--allow-empty', '-m', 'Initial')
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(baseline_root))
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            page.locator('#import-mode').select_option('worktree')
            assert page.locator('#import-baseline').is_visible()
            assert page.locator('#import-baseline-initial').is_hidden()
            page.locator('#import-baseline').select_option('commit')
            page.locator('#import-baseline').select_option('existing')
            assert page.locator('#import-baseline').input_value() == 'existing'
            page.locator('#import-close').click()

            # Existing source with no repository: exclude secrets and one chosen file.
            state['lost'] = False
            page.set_viewport_size({'width': 1280, 'height': 900})
            (fixture.root / '.env').write_text('PRIVATE=local-only', encoding='utf-8')
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(fixture.root))
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            supplement = page.locator('#import-supplement')
            supplement.uncheck()
            supplement.hover()
            assert supplement.is_enabled()
            assert supplement.evaluate('n => getComputedStyle(n).cursor') == 'pointer'
            assert page.locator('#dlg-project-import').get_attribute('aria-busy') == 'false'
            supplement.check()
            page.locator('#import-mode').select_option('worktree')
            page.locator('#import-next').click()
            page.locator('#import-role-step').wait_for(state='visible')
            page.locator('#import-back').click()
            page.get_by_role('checkbox', name='提交 code.bin', exact=True).uncheck()
            if page.get_by_role('checkbox', name='提交 .env', exact=True).count():
                assert not page.get_by_role('checkbox', name='提交 .env', exact=True).is_checked()
            page.locator('#import-next').click()
            page.locator('#import-next').click()
            page.locator('#import-agents-step').wait_for(state='visible')
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[0]}"]').check()
            page.locator('#import-members select').first.select_option('项目负责人')
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            assert '提交：code.bin' not in page.locator('#import-review').inner_text()
            page.locator('#import-submit').click()
            page.wait_for_function('p => S.currentProject && S.currentProject !== p', arg=project)
            tracked = git.checked(fixture.root, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines()
            assert '.env' not in tracked and 'code.bin' not in tracked
            assert (fixture.root / '.env').read_text() == 'PRIVATE=local-only'

            # Template-specific owners share notification/selection behavior, with distinct grants.
            page.locator('#app-alert').wait_for(state='visible')
            page.locator('#app-alert .primary').click()
            for template, owner_name, others, grant_count in (
                    ('research', '调研负责人', ['资料研究员', '分析员', '复核员'], 10),
                    ('writing', '主编', ['资料研究员', '作者', '审校'], 10),
                    ('blank', '项目负责人', [], 5)):
                template_root = fixture.base / (template + '-owner')
                page.evaluate('openProjectNew()')
                page.locator('#project-entry-new').click()
                page.locator('#import-root').fill(str(template_root))
                page.locator('#import-template').select_option(template)
                page.locator('#import-preview').click()
                page.locator('#import-config').wait_for(state='visible')
                page.locator('#import-next').click()
                page.locator('#import-role-step').wait_for(state='visible')
                assert page.locator('#import-roles strong').all_text_contents() == [owner_name, *others]
                owner_card = page.locator('#import-roles > section').first
                assert f'已启用 {grant_count} 项' in owner_card.inner_text()
                assert '接收阻塞通知' in owner_card.inner_text()
                assert owner_card.get_by_role('button', name='移除角色').count() == 0
                for card in page.locator('#import-roles > section').all()[1:]:
                    assert '已启用 0 项' in card.inner_text()
                page.locator('#dlg-project-import').screenshot(path=str(output / (template + '-owner.png')))
                page.locator('#import-next').click()
                page.locator('#import-agents-step').wait_for(state='visible')
                agents = page.locator('#import-agents-content').input_value()
                if others:
                    assert f'{owner_name}是本项目的负责人' in agents
                    assert '接收并跟进任务阻塞通知' in agents
                    assert '的默认管理权限为空' in agents
                page.locator('#import-agents-reviewed').check()
                page.locator('#import-next').click()
                for bot_id in fixture.ids:
                    page.locator(f'#import-members input[data-bot-id="{bot_id}"]').check()
                selects = page.locator('#import-members select')
                selects.nth(0).select_option(owner_name)
                selects.nth(1).select_option(owner_name)
                assert selects.nth(0).input_value() == ''
                selects.nth(0).select_option(owner_name)
                selects.nth(1).select_option(others[0] if others else '')
                page.locator('#import-next').click()
                page.locator('#import-resource-step').wait_for(state='visible')
                page.locator('#import-next').click()
                page.locator('#import-review').wait_for(state='visible')
                assert page.locator('#import-review p').filter(has_text='角色：' + owner_name).count() == 1
                page.locator('#import-submit').click()
                page.wait_for_function('p => S.projects.find(x => x.name === S.currentProject)?.root.replaceAll("\\\\", "/") === p',
                                       arg=str(template_root).replace('\\', '/'))
                from engine import task_retry, project_permissions
                created_project = page.evaluate('S.currentProject')
                owner = task_retry.project_owner(fixture.conn, created_project)
                assert owner
                actual_grants = [p for p in project_permissions.PERMISSIONS if project_permissions.check(fixture.conn, created_project, owner, p) is None]
                assert len(actual_grants) == grant_count
                assert (template_root / 'AGENTS.md').read_text(encoding='utf-8') == agents
                roles = fixture.client.get(f'/projects/{created_project}/roles').json()['roles']
                owner_role = next(r for r in roles if r['is_owner'])
                assert owner_role['name'] == owner_name
                page.evaluate('([project, roles]) => renderProjectRoles(project, roles)', [created_project, roles])
                assert page.locator(f'#pj-role-list [data-role-id="{owner_role["id"]}"]').get_by_role('button', name='删除', exact=True).count() == 0
                page.locator('#app-alert').wait_for(state='visible')
                page.locator('#app-alert .primary').click()
            assert not errors, errors
            browser.close()
        print('PASS: seven steps, real Git, 390px themes, template owner names/grants/notification/AGENTS/role protection')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
