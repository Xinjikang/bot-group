"""Real Chromium UI with synthetic API responses; no engine workers or production data."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
import tempfile
import sys
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.project_permissions import PERMISSIONS


def main():
    project = {'name': 'p', 'display_name': '项目显示名', 'root': 'E:/sample/repo',
               'directory_mode': 'worktree', 'worktree_root': 'E:/sample/trees'}
    member = {'profile': 'dev', 'display_name': '开发者', 'active': 1, 'role': '',
              'directory_mode': 'worktree', 'independent_path': 'E:/sample/trees/dev',
              'workspace': {'path': 'E:/sample/trees/dev', 'valid': False, 'error': '尚未创建'}}
    requests = []
    workspace_queries = []
    failures = {'workspace': False, 'scan': False, 'register': None}
    state = {'roles': [{'id': 1, 'name': 'orchestrator', 'member_count': 1},
                       {'id': 2, 'name': '数据分析', 'member_count': 2}]}  # req#96 #166：权限 tab 的角色候选与角色 tab 同源
    register_calls = []
    scan_candidates = [
        {'host_type': 'hermes', 'profile': 'fe-dev-02', 'config_root': 'E:/hermes/fe-dev-02',
         'display_name': '沈清', 'bot_id': 12, 'bound': True, 'already_member': False},
        {'host_type': 'hermes', 'profile': 'new-bot', 'config_root': 'E:/hermes/new-bot',
         'display_name': '', 'bot_id': None, 'bound': False, 'already_member': False},
        {'host_type': 'codex', 'profile': 'claire', 'config_root': 'C:/Users/x/.codex',
         'display_name': '', 'bot_id': None, 'bound': False, 'already_member': True},
    ]
    permissions = {'project': 'p', 'revision': 'v1',
                   'permissions': [{'key': key, 'label': label} for key, label in PERMISSIONS.items()],
                   'members': [{'profile': 'custom-lead', 'display_name': '项目负责人', 'active': 1},
                               {'profile': 'custom-dev', 'display_name': '开发成员', 'active': 1},
                               {'profile': 'paused', 'display_name': '暂停成员', 'active': 0}]}
    permissions['grants'] = {p['key']: [] for p in permissions['permissions']}
    # req#96 #166：权限tab授权对象支持角色（后端 035445a：view 返 roles、grants 含 role:<id>）
    permissions['roles'] = [{'id': 1, 'name': 'orchestrator', 'member_count': 1},
                            {'id': 2, 'name': '数据分析', 'member_count': 2}]
    permissions['grants']['task.create'] = ['role:2']
    permission_conflict = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            body = req.request.post_data_json if req.request.method in ('POST', 'PUT', 'PATCH') else None
            requests.append((path, body))
            data = {}
            if path == '/projects/p/roles':
                # req#96 #165：角色实体 CRUD mock（角色 tab + 成员卡片候选来源）
                roles = state.get('roles', [{'id': 1, 'name': 'orchestrator', 'member_count': 1}])
                if req.request.method == 'POST':
                    if any(r['name'] == body['name'] for r in roles):
                        req.fulfill(status=409, json={'detail': '该项目已存在同名角色'})
                        return
                    roles.append({'id': len(roles) + 100, 'name': body['name'], 'member_count': 0})
                data = {'roles': roles, 'count': len(roles)}
            elif path.startswith('/projects/p/roles/') and req.request.method == 'PATCH':
                role = next(r for r in state['roles']
                            if r['id'] == int(path.rsplit('/', 1)[1]))
                role['name'] = body['name']
                data = {'id': role['id'], 'name': role['name']}
            elif path.startswith('/projects/p/roles/') and req.request.method == 'DELETE':
                state['roles'] = [r for r in state['roles'] if r['id'] != int(path.rsplit('/', 1)[1])]
                data = {'deleted': True}
            elif path == '/projects/p/members/dev/role':
                member['role'] = body['role']
                member['role_id'] = next((r['id'] for r in state['roles'] if r['name'] == body['role']), None)
                data = {'project': 'p', 'profile': 'dev', 'role': member['role'], 'role_id': member['role_id']}
            elif path.endswith('/permissions'):
                if body:
                    if permission_conflict:
                        req.fulfill(status=409, json={'detail': '权限已被修改，请重新加载后保存'})
                        return
                    permissions['grants'] = body['grants']
                    permissions['revision'] = 'v2'
                data = permissions
            elif path.endswith('/agents-md'):
                data = {'content': '# 项目约定', 'exists': True, 'revision': 'a' * 64}
            elif path.endswith('/workspaces'):
                workspace_queries.append(req.request.url)
                if failures['workspace']:
                    req.fulfill(status=503, json={'detail': 'temporary offline'})
                    return
                data = {'project': project, 'members': [member,
                         {'profile': 'orchestrator', 'display_name': '协调者', 'active': 1, 'role': 'orchestrator',
                          'directory_mode': 'worktree', 'independent_path': 'E:/sample/trees/orchestrator',
                          'workspace': {'path': 'E:/sample/trees/orchestrator', 'valid': True, 'head': 'a' * 40,
                                        'branch': 'main', 'detached': False, 'dirty': False, 'error': ''}}],
                        'history': [],
                        'repository': {'head': 'a' * 40, 'branch': 'main', 'refs': ['main', 'release/v1']}}
            elif path.endswith('/workspaces/dev'):
                if body['action'] == 'set_mode':
                    member['directory_mode'] = body['mode']
                    member['workspace']['path'] = project['root'] if body['mode'] == 'shared' else member['assigned_workspace_path']
                else:
                    member['workspace'] = {'path': body['path'], 'valid': True, 'head': 'a' * 40, 'detached': True, 'dirty': False}
                    member['workspace_assigned'] = True
                    member['assigned_workspace_path'] = body['path']
                    member['directory_mode'] = 'worktree'
                data = {'ok': True}
            elif path == '/projects/p' and body:
                project.update(body)
                data = {'project': 'p'}
            elif path == '/projects':
                if body:
                    project.update(body)
                    project['name'] = 'p_generated'
                    data = {'project': project['name']}
                else:
                    # req#107 #185：默认只回活跃；include_archived=1 回全部（活跃+归档）
                    include_archived = 'include_archived=1' in urlparse(req.request.url).query
                    rows = [project]
                    if include_archived:
                        rows.append({'name': 'p_archived', 'display_name': '旧仓库', 'root': 'E:/old',
                                     'directory_mode': 'shared', 'archived': 1})
                    else:
                        rows = [p for p in rows if not p.get('archived')]
                    data = {'projects': rows}
            elif path == '/projects/p/archive':
                # req#105 #182：归档确认流 mock（pjDropOutside app-alert 豁免回归）
                project['archived'] = 1 if (body or {}).get('archived') else 0
                data = {'ok': True}
            elif path == '/members/dev/config':
                data = {'description': '测试成员', 'soul': '# Soul', 'env': '', 'model': 'model',
                        'provider': 'provider', 'reasoning_effort': 'medium', 'revision': 'b' * 64}
            elif path == '/discovery/scan':
                if failures['scan']:
                    req.fulfill(status=503, json={'detail': 'scan offline'})
                    return
                data = {'candidates': scan_candidates, 'count': None}
            elif path == '/bots/register':
                if failures['register'] is not None:
                    req.fulfill(status=400, json={'detail': failures['register']})
                    return
                register_calls.append(body)
                data = {'bot_id': 77, 'member_profile': 'bot-77', 'created': not body.get('config_root', '').endswith('/reuse'),
                        'profile': body['profile'], 'project': body.get('project')}
            elif path == '/members':
                data = {'members': [member]}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/messages':
                data = {'messages': []}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://workspace.test/app/index.html')
        page.evaluate('''p => { S.projects=[p]; S.currentProject=p.name; S.members=[{profile:'dev',display_name:'开发者'}]; showLogin(false); renderProjectBar(); }''', project)
        page.locator('[data-project="p"]').click()
        assert page.locator('#pj-d-id').input_value() == 'p'
        assert page.locator('#pj-d-id').get_attribute('readonly') is not None
        page.locator('#pj-tab-permissions').click()
        page.locator('#pj-permissions-save').wait_for(state='visible')
        page.wait_for_function("!document.querySelector('#pj-permissions-save').disabled")
        assert page.locator('#pj-permissions-list input[value="paused"]').count() == 0
        assert page.locator('.project-permission-row').count() == 10
        assert page.locator('#pj-permissions-list legend').all_text_contents() == [
            '创建需求', '更新需求', '恢复需求', '关闭需求', '撤回需求',
            '创建任务', '更新任务', '恢复任务', '关闭任务', '撤回任务']
        for key in ('req.resume', 'task.resume'):
            row = page.locator(f'[data-permission="{key}"]')
            row.locator('summary').click()
            row.locator('input[value="role:2"]').check()
            assert '派工、审查、验收' in page.locator('#app-alert-message').inner_text()
            page.get_by_role('button', name='确定', exact=True).click()
            row.locator('input[value="custom-dev"]').check()
            row.locator('summary').click()
        # req#96 #166：授权对象=bot ∪ 角色——task.create 预授权 role:2（数据分析 2 名成员）
        tc_row = page.locator('[data-permission="task.create"]')
        tc_row.locator('summary').click()
        assert tc_row.locator('input[value="role:2"]').is_checked()      # 预授权回显
        assert tc_row.locator('input[value="role:1"]').count() == 1      # 角色项与 bot 项同列可选
        assert tc_row.locator('input[value="role:2"]').locator('xpath=..').inner_text() == '角色：数据分析（2 名成员）'
        tc_row.locator('input[value="role:2"]').uncheck()                # 取消角色授权（多选改动不影响 bot 项）
        page.get_by_role('button', name='确定', exact=True).click()
        tc_row.locator('input[value="custom-lead"]').check()             # 同一项权限可同时勾角色和 bot
        page.locator('#pj-permissions-save').click()
        page.wait_for_function("document.querySelector('#pj-permissions-status').textContent.includes('权限已保存')")
        assert permissions['grants']['task.create'] == ['custom-lead']   # role:2 已取消、bot 已加入
        for key in ('req.resume', 'task.resume'):
            assert set(permissions['grants'][key]) == {'role:2', 'custom-dev'}
        update = page.locator('[data-permission="task.update"]')
        update.locator('summary').click()
        update.locator('[value="custom-lead"]').check()
        update.locator('[value="custom-dev"]').check()
        assert len([1 for path, body in requests if path.endswith('/permissions') and body]) == 1  # 仅 task.create 已保存
        page.locator('#pj-tab-basic').click()
        page.locator('#pj-tab-permissions').click()
        assert update.locator('input:checked').count() == 2
        page.locator('#pj-permissions-save').click()
        page.wait_for_function("document.querySelector('#pj-permissions-status').textContent.includes('权限已保存')")
        assert permissions['grants']['task.update'] == ['custom-lead', 'custom-dev']
        assert permissions['grants']['task.create'] in (['custom-lead'], ['custom-lead', 'role:2'])  # req#96 #166：本轮取消又未重勾，视保存次序而定
        assert all(not v for k, v in permissions['grants'].items()
                   if k not in ('task.update', 'task.create', 'req.resume', 'task.resume'))
        permission_conflict = True
        update.locator('summary').click()
        update.locator('[value="custom-dev"]').uncheck()
        page.locator('#pj-permissions-save').click()
        page.wait_for_function("document.querySelector('#pj-permissions-status').textContent.includes('保存失败')")
        assert not update.locator('[value="custom-dev"]').is_checked()
        assert len(permissions['grants']['task.update']) == 2
        permission_conflict = False
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'group-permissions-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 720})
        page.evaluate("anchorDialog(document.querySelector('#dlg-project-detail'), document.querySelector('#chip-all'))")
        assert page.locator('#pj-panel-permissions').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'group-permissions-mobile.png'))
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.evaluate("anchorDialog(document.querySelector('#dlg-project-detail'), document.querySelector('#chip-all'))")
        page.locator('#pj-tab-agents').click()
        page.wait_for_function("document.querySelector('#pj-agents-status').textContent === '已加载'")
        assert page.locator('#pj-agents-content').input_value() == '# 项目约定'
        page.locator('#pj-tab-basic').click()
        page.locator('[name="project-directory-mode"][value="shared"]').check()
        assert page.locator('#pj-d-root').is_visible()
        assert not page.locator('#pj-worktree-options').is_visible()
        page.locator('[name="project-directory-mode"][value="worktree"]').check()
        page.locator('#pj-tab-members').click()
        page.locator('#pj-member-list .workspace').first.click()
        assert page.locator('#workspace-form').is_hidden()
        assert page.locator('#workspace-remove').is_hidden()
        page.locator('#workspace-create').click()
        assert page.locator('#workspace-submit').inner_text() == '确认创建'
        assert not any(path.endswith('/workspaces/dev') for path, body in requests)
        page.locator('#workspace-close').click()
        page.locator('#pj-member-list .workspace').first.click()
        assert page.locator('#workspace-form').is_hidden()
        page.locator('#workspace-create').click()
        page.locator('#workspace-submit').click()
        page.wait_for_function("document.querySelector('#pj-member-list').textContent.includes('detached')")
        operation = next(body for path, body in requests if path.endswith('/workspaces/dev'))
        assert operation == {'action': 'create', 'path': 'E:/sample/trees/dev'}
        assert any('?profile=dev' in url for url in workspace_queries)
        assert page.locator('#dlg-workspace').evaluate('el => el.open')
        assert page.locator('#workspace-resources').is_enabled()
        assert page.locator('#workspace-resources').is_visible()
        page.locator('#workspace-close').click()
        page.locator('#pj-member-list .workspace').first.click()
        assert page.locator('#workspace-options').is_hidden()
        assert page.locator('#workspace-remove').is_visible()
        page.locator('#workspace-remove').click()
        assert 'E:/sample/trees/dev' in page.locator('#workspace-help').inner_text()
        assert page.locator('#workspace-submit').inner_text() == '确认移除工作区'
        page.keyboard.press('Escape')
        assert len([body for path, body in requests if path.endswith('/workspaces/dev')]) == 1
        # Radio changes stay local until saved; main-directory choice retains the worktree.
        page.locator('#pj-member-list .workspace').first.click()
        page.locator('[name="member-directory-mode"][value="shared"]').check()
        assert page.locator('#workspace-save-mode').is_visible()
        page.locator('#workspace-close').click()
        assert len([body for path, body in requests if path.endswith('/workspaces/dev')]) == 1
        page.locator('#pj-member-list .workspace').first.click()
        assert page.locator('[name="member-directory-mode"][value="worktree"]').is_checked()
        page.locator('[name="member-directory-mode"][value="shared"]').check()
        page.locator('#workspace-save-mode').click()
        page.wait_for_function("document.querySelector('.project-member-caption').textContent === '项目主目录'")
        assert member['workspace_assigned']
        page.locator('#pj-member-list .workspace').first.click()
        assert '已有 worktree 保留' in page.locator('#workspace-current').inner_text()
        page.locator('[name="member-directory-mode"][value="worktree"]').check()
        page.locator('#workspace-save-mode').click()
        page.wait_for_function("document.querySelector('.project-member-caption').textContent === '独立 worktree'")
        failures['workspace'] = True
        page.locator('#pj-tab-basic').click()
        page.locator('#pj-tab-members').click()
        page.wait_for_function("document.querySelector('#pj-members-status').textContent.includes('刷新失败')")
        assert page.locator('#pj-member-list .workspace').first.is_visible()
        assert 'detached' in page.locator('#pj-member-list').inner_text()
        failures['workspace'] = False
        page.locator('#pj-members-refresh').click()
        page.wait_for_function("document.querySelector('#pj-members-status').textContent === '状态已更新'")
        page.locator('#pj-member-list .edit').first.click()
        assert page.locator('#profile-tabs [role=tab]').count() == 4
        page.locator('#profile-tab-model').click()
        page.locator('#profile-reasoning_effort').select_option('xhigh')
        page.locator('#profile-save').click()
        page.wait_for_function("!document.querySelector('#dlg-profile').open")
        saved = next(body for path, body in requests if path.endswith('/config') and body)
        assert saved['reasoning_effort'] == 'xhigh'
        page.locator('#pj-panel-members').wait_for(state='visible')
        page.locator('#pj-tab-basic').click()
        page.locator('#pj-d-name').fill('改后的显示名')
        page.locator('#pj-detail-form button[type=submit]').click()
        page.wait_for_function("!document.querySelector('#dlg-project-detail').open")
        changed = next(body for path, body in requests if path == '/projects/p' and body)
        assert changed['display_name'] == '改后的显示名'
        assert 'new_name' not in changed and 'name' not in changed
        assert page.locator('[data-project="p"]').inner_text() == '改后的显示名'
        page.locator('[data-project="p"]').click()
        page.locator('#pj-tab-members').click()
        page.wait_for_function("document.querySelector('#pj-member-list').textContent.includes('detached')")
        # Verify the project window stays usable on a narrow viewport too.
        page.set_viewport_size({'width': 390, 'height': 720})
        page.evaluate("anchorDialog(document.querySelector('#dlg-project-detail'), document.querySelector('#chip-all'))")
        assert page.locator('#btn-members').count() == 0
        box = page.locator('#dlg-project-detail').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= 391
        page.locator('#pj-member-list .workspace').first.click()
        workspace_box = page.locator('#dlg-workspace').bounding_box()
        assert workspace_box['x'] >= 0 and workspace_box['x'] + workspace_box['width'] <= 391
        page.locator('#workspace-close').click()
        page.evaluate('''() => {
          const data = projectMembersCache.get('p');
          data.members = [{...data.members[0], profile:'default', directory_mode:'shared',
            workspace_assigned:false, assigned_workspace_path:'', independent_path:'E:/sample/trees/default',
            workspace:{path:'E:/sample/repo',valid:true}}];
          renderProjectMembers('p', data);
        }''')
        page.locator('#pj-member-list .workspace').first.click()
        assert page.locator('[name="member-directory-mode"][value="shared"]').is_checked()
        assert page.locator('#workspace-options').is_hidden()
        page.locator('[name="member-directory-mode"][value="worktree"]').check()
        page.locator('#workspace-create').click()
        assert page.locator('#workspace-path').input_value() == 'E:/sample/trees/default'
        page.locator('#workspace-close').click()
        page.evaluate('openProjectDetail(null)')
        assert not page.locator('#pj-id-info').is_visible()
        assert page.locator('[name="project-directory-mode"][value="shared"]').is_checked()
        page.locator('[name="project-directory-mode"][value="shared"]').focus()
        page.keyboard.press('ArrowRight')
        assert page.locator('#pj-d-mode').input_value() == 'worktree'
        assert page.locator('#pj-worktree-options').is_visible()
        page.keyboard.press('ArrowLeft')
        page.locator('#pj-d-name').fill('新建项目')
        page.locator('#pj-d-root').fill('E:/sample/new')
        page.locator('#pj-detail-form button[type=submit]').click()
        page.wait_for_function("S.currentProject === 'p_generated'")
        created = next(body for path, body in requests if path == '/projects' and body)
        assert created['display_name'] == '新建项目' and 'name' not in created
        # 加入只选择宿主/profile；角色与权限由项目分别配置。
        # 上一段以 openProjectDetail(null)（新建模式）结束：成员 tab 在新建模式是隐藏的，先回到项目详情。
        page.evaluate("() => openProjectDetail('p')")
        page.wait_for_function("document.querySelector('#pj-tab-members').hidden === false")
        page.locator('#pj-tab-members').click()
        page.locator('#pj-members-add').wait_for(state='visible')
        # req#93 #159：添加成员按钮右侧独立（刷新/状态在左），DOM 顺序刷新→状态→添加且用 margin-left:auto 顶到最右
        order = page.evaluate("() => [...document.querySelectorAll('#pj-panel-members > div:first-of-type > *')].map(e => e.id)")
        assert order == ['pj-members-refresh', 'pj-members-status', 'pj-members-add'], order
        assert 'auto' in page.evaluate("() => document.querySelector('#pj-members-add').style.marginLeft")
        # req#94 #160：底部「本项目角色」表单已删，改头部角色按钮+下拉就地保存。
        # 夹具成员 role=''（bot-36 空角色）→ 按钮显示「设置角色」；
        # role 候选=当前项目成员 DISTINCT role：dev(空)+role='fe-dev' 的第二成员 → 候选 ['fe-dev']。
        assert page.locator('.project-member-role').count() == 0, '底部角色表单已删除'
        role_btn = page.locator('.project-member-role-btn').first
        assert role_btn.inner_text() == '设置角色'
        card_height = role_btn.locator('xpath=ancestor::*[contains(@class,"project-member-card")]').bounding_box()['height']
        page.locator('.project-member-card[data-profile=\"dev\"] .project-member-role-btn, .project-member-card .project-member-role-btn').first.click()
        menu = page.locator('.project-member-role-menu')
        menu.wait_for(state='visible')
        assert menu.evaluate("el => el.matches(':popover-open')")
        assert menu.evaluate("el => getComputedStyle(el).position") == 'fixed'
        assert abs(role_btn.locator('xpath=ancestor::*[contains(@class,"project-member-card")]').bounding_box()['height'] - card_height) < 1
        bounds = menu.bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= page.viewport_size['width']
        items = menu.locator('button')
        assert items.count() == 2  # req#96 #166：候选含 数据分析（state roles 现有 2 个角色，均未被 dev 分配）
        assert items.first.inner_text() in ('fe-dev', 'orchestrator')
        role_candidates_hits = [1 for path, body in requests if path.endswith('/role') and body]
        items.first.click()
        page.wait_for_function("document.querySelectorAll('#pj-panel-members .project-member-role-menu').length === 0")
        patch_calls = [body for path, body in requests if path.endswith('/role') and body]
        assert patch_calls and patch_calls[-1]['role'] in ('fe-dev', 'orchestrator'), patch_calls
        # 就地显示所选角色名（loadMembers 重渲染后 button 文案=所存角色）
        page.wait_for_function("document.querySelector('.project-member-role-btn').textContent.match(/fe-dev|orchestrator/)")
        page.locator('.project-member-role-btn').first.click()  # 再开菜单：current 态标记在当前角色项上
        page.locator('.project-member-role-menu').wait_for(state='visible')
        assert page.locator('.project-member-role-menu button.current').count() == 1
        page.keyboard.press('Escape')  # 关菜单（outside click 路径）
        page.mouse.click(30, 30)
        page.wait_for_function("document.querySelectorAll('.project-member-role-menu').length === 0")
        # Deleted-role text may match a recreated role; NULL remains unassigned in the menu.
        page.evaluate('''() => {
          const data=projectMembersCache.get('p');
          const m=data.members.find(m => m.profile === 'dev');
          window.savedRoleId=m.role_id; m.role_id=null;
          renderProjectMembers('p',data);
        }''')
        page.locator('.project-member-role-btn').first.click()
        page.locator('.project-member-role-menu button').first.wait_for(state='visible')
        assert page.locator('.project-member-role-menu button.current').count() == 0
        page.locator('.project-member-workspace').first.click()
        page.evaluate('''() => {
          const data=projectMembersCache.get('p');
          data.members.find(m => m.profile === 'dev').role_id=savedRoleId;
          renderProjectMembers('p',data);
        }''')
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'group-project-role-mobile.png'))
        page.set_viewport_size({'width': 390, 'height': 720})
        card_box = page.locator('.project-member-card').first.bounding_box()
        assert card_box['x'] >= 0 and card_box['x'] + card_box['width'] <= 390
        page.set_viewport_size({'width': 1280, 'height': 900})
        # req#96 #165：角色 tab——列表/创建/改名/删除全链（成员卡候选来源的同一套实体）
        page.locator('#pj-tab-roles').click()
        page.wait_for_function("document.querySelector('#pj-role-list').textContent.includes('orchestrator')")
        assert page.locator('.pj-role-row').count() == 2  # req#96 #166：同源数据已含 数据分析（权限 tab 断言复用）
        row = page.locator('.pj-role-row').first
        # 改名（走 id：稳定关联）
        row.locator('button', has_text='改名').click()
        page.get_by_role('button', name='确定', exact=True).click()
        rename = page.locator('.pj-role-rename')
        rename.wait_for(state='visible')
        rename.fill('orchestrator-v2')
        rename.press('Enter')
        page.wait_for_function("document.querySelector('#pj-role-list').textContent.includes('orchestrator-v2')")
        assert '角色已改名' in page.locator('#pj-role-status').inner_text()
        # 创建角色
        page.locator('#pj-role-new-name').fill('发布协调')
        page.locator('#pj-role-create').click()
        page.wait_for_function("document.querySelectorAll('.pj-role-row').length === 3")
        assert '角色已创建' in page.locator('#pj-role-status').inner_text()
        rename_calls = [(path, body) for path, body in requests if path.startswith('/projects/p/roles/') and body]
        assert any('/roles/1' in path and body['name'] == 'orchestrator-v2' for path, body in rename_calls), rename_calls
        assert any(body['name'] == '发布协调' for path, body in requests if path == '/projects/p/roles' and body)
        # 新建角色 0 成员：删除不置灰；有成员的角色：删除置灰
        new_row = page.locator('.pj-role-row').nth(2)  # 发布协调（0 成员）是新建的第 3 行
        assert new_row.locator('button', has_text='删除').is_enabled()
        assert page.locator('.pj-role-row').first.locator('button', has_text='删除').is_disabled()
        # 删除空角色走 confirm+DELETE
        new_row.locator('button', has_text='删除').click()
        page.wait_for_timeout(300)  # showConfirm 弹窗（app-alert dialog）
        page.locator('#app-alert button.primary').click()
        page.wait_for_timeout(800)
        assert '角色已删除' in page.locator('#pj-role-status').inner_text()
        assert page.locator('.pj-role-row').count() == 2
        permission_writes = len([body for path, body in requests if path.endswith('/permissions') and body])
        page.locator('#pj-tab-members').click()  # 回成员 tab：添加成员入口在成员面板
        page.locator('#pj-members-add').click()
        page.wait_for_function("document.querySelector('#member-add-title').textContent.includes('1/2')")
        # 步1 未选宿主时「下一步」禁用（选中 radio 立即解锁）
        assert page.locator('#member-add-next').is_disabled()
        # 扫接口失败降级空态不阻塞（验收②）：上一步可返回重试
        failures['scan'] = True
        page.locator('[name="member-add-host"][value="hermes"]').check()
        page.locator('#member-add-next').click()
        page.wait_for_function("document.querySelector('#member-add-title').textContent.includes('2/2')")
        page.wait_for_function("document.querySelector('#member-add-status').textContent.includes('扫描失败')")
        failures['scan'] = False
        # 上一步回步1，换宿主重扫（scan 成功 → codex 候选过滤，验收①）
        page.locator('#member-add-back').click()
        page.locator('[name="member-add-host"][value="codex"]').check()
        page.locator('#member-add-next').click()
        page.wait_for_function("document.querySelectorAll('input[name=member-add-profile]').length === 1")
        assert page.locator('input[name="member-add-profile"]').count() == 1  # codex 候选过滤
        assert '已是花名册成员' in page.locator('#member-add-steps').inner_text()
        page.locator('#member-add-back').click()
        page.locator('[name="member-add-host"][value="hermes"]').check()
        page.locator('#member-add-next').click()
        page.wait_for_function("document.querySelectorAll('input[name=member-add-profile]').length === 2")
        # 绑定候选：复用 Bot 提示；未绑定候选：注册新 Bot 提示
        page.locator('input[name="member-add-profile"]').first.check()
        page.wait_for_function("document.querySelector('#member-add-steps').textContent.includes('直接复用')")
        assert '沈清' in page.locator('#member-add-steps').inner_text()
        page.locator('input[name="member-add-profile"]').nth(1).check()
        page.wait_for_function("document.querySelector('#member-add-steps').textContent.includes('注册新 Bot')")
        assert '2/2' in page.locator('#member-add-title').inner_text()
        assert page.locator('#member-add-role, #member-add-steps input[type="checkbox"]').count() == 0
        assert not page.locator('#member-add-next').is_visible()
        # 加入失败上屏不关弹窗（接口报错降级，验收②）
        failures['register'] = '该 profile 尚未绑定 Bot 且缺少 config_root'
        page.locator('#member-add-join').click()
        page.wait_for_function("document.querySelector('#member-add-status').textContent.includes('加入失败')")
        assert page.locator('#dlg-member-add').evaluate('el => el.open')
        failures['register'] = None
        # 加入成功仅注册身份并加入当前项目，不写入角色或授权。
        page.locator('#pj-panel-members').wait_for(state='attached')
        page.locator('#member-add-join').click()
        page.wait_for_function("!document.querySelector('#dlg-member-add').open")
        assert register_calls[-1] == {'project': 'p', 'host_type': 'hermes', 'profile': 'new-bot',
                                      'config_root': 'E:/hermes/new-bot'}
        assert len([body for path, body in requests if path.endswith('/permissions') and body]) == permission_writes
        # req#105 #182：项目下拉窗归档确认流——app-alert 点确定不把 pjDrop 关掉置 null
        # （修复前：pjDropOutside 误关 pjDrop → renderProjectDropList 对 null 调 querySelector 崩溃）
        page.evaluate("() => { S.currentProject=''; closeDialogAnchored($('#dlg-project-detail')); closeProjectDrop(); renderProjectBar(); }")
        project['name'] = 'p'  # detached-creation 流程把 mock 项目改名 p_generated，复原名供归档流使用（mock python 侧）
        # req#107 #185：默认下拉窗只显示活跃项目，不出现已归档
        page.locator('#chip-all').click()
        page.locator('#pj-dropdown').wait_for(state='visible')
        rows = page.locator('.pj-drop-row')
        assert rows.count() == 1, f'默认应只显示 1 个活跃项目，实际 {rows.count()}'
        assert '旧仓库' not in page.locator('#pj-drop-list').inner_text(), '默认流出现已归档项目'
        assert page.evaluate("() => !!document.querySelector('#pj-drop-toggle-archived')"), '缺查看归档切换按钮'
        # ②查看归档按钮在接入项目按钮前（DOM 顺序断言，比 y 坐标稳），点击后切换为显示归档项目含恢复入口
        order = page.evaluate("() => document.querySelector('#pj-drop-toggle-archived').compareDocumentPosition(document.querySelector('#pj-drop-add')) & 4")
        assert order == 4, '查看归档按钮应在接入项目按钮之前（DOM 顺序）'
        page.locator('#pj-drop-toggle-archived').click()
        page.wait_for_timeout(600)
        assert '旧仓库' in page.locator('#pj-drop-list').inner_text() and '恢复' in page.locator('#pj-drop-list').inner_text(), '归档流未显示归档项目/恢复入口'
        assert page.locator('.pj-drop-row', has_text='旧仓库').count() == 1
        # ③可切回只显示活跃
        page.locator('#pj-drop-toggle-archived').click()
        page.wait_for_timeout(600)
        assert '旧仓库' not in page.locator('#pj-drop-list').inner_text(), '切回后归档项目仍在'
        # 归档确认流（req#105 #182 保持）：活跃项目 → 弹归档确认框
        page.locator('.pd-arch[data-name="p"]').click()
        page.wait_for_function("document.querySelector('#app-alert')?.open")
        page.locator('#app-alert button.primary').click()
        page.wait_for_timeout(900)
        assert page.evaluate("() => !!document.querySelector('#pj-dropdown')"), 'pjDrop 被误关（app-alert 豁免未生效）'
        assert project['archived'] == 1, '归档接口未被调用或未生效'
        # 活跃流归档后条目消失（req#107 #185：保留当前展示模式）；「查看归档」按钮切回「查看归档」态可见
        assert page.locator('.pj-drop-row').count() == 0, '活跃流归档后条目应消失'
        page.locator('#pj-drop-toggle-archived').click()  # 切归档流
        page.wait_for_timeout(600)
        assert '已归档' in page.locator('.pj-drop-row').first.inner_text(), '归档流未刷新为已归档'
        # 恢复（已归档 → 直接成功无确认框）；归档流下恢复后条目消失（保留展示模式）
        page.locator('.pd-arch[data-name="p"]').click()
        page.wait_for_timeout(900)
        assert page.evaluate("() => !!document.querySelector('#pj-dropdown')")
        assert page.locator('.pj-drop-row').count() == 1, '归档流恢复后应只剩 p_archived 一行'
        # 顺带切回活跃流：项目回列表
        page.locator('#pj-drop-toggle-archived').click()
        page.wait_for_timeout(600)
        assert '活跃' in page.locator('.pj-drop-row').first.inner_text()
        # 取消流：再切活跃制造确认框 → 点取消 → 状态不变 pjDrop 仍在
        page.locator('.pd-arch[data-name="p"]').click()
        page.wait_for_function("document.querySelector('#app-alert')?.open")
        page.locator('#app-alert button:not(.primary)').click()
        page.wait_for_timeout(600)
        assert page.evaluate("() => !!document.querySelector('#pj-dropdown')")
        assert project['archived'] == 0  # 取消不改状态（恢复后仍是活跃）
        assert '活跃' in page.locator('.pj-drop-row').first.inner_text()
        # 点下拉窗外 = 正常关闭（豁免不误伤普通外部点击）
        page.mouse.click(30, 500)
        page.wait_for_timeout(300)
        assert page.evaluate("() => !document.querySelector('#pj-dropdown')")
        assert not errors, errors
        browser.close()
    print("PASS: project modes, detached creation, four profile tabs, reasoning strength, stable ID, narrow layout, member add wizard (req#87 #151), project roles tab (req#96 #165), drop archive confirm (req#105 #182), drop archived toggle (req#107 #185)")


if __name__ == '__main__':
    main()
