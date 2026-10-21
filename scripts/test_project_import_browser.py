"""Chromium wizard against actual isolated import APIs; no worker or production writes."""
import mimetypes
import argparse
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_import import ImportTests, fingerprints
from engine import taskpool

ROOT = Path(__file__).resolve().parents[1]


def main(screenshot_dir=None):
    fixture = ImportTests()
    fixture.setUp()
    try:
        (fixture.root / 'doc' / 'design.md').write_text('<img src=x onerror=window.previewInjection=1> Existing document', encoding='utf-8')
        before = fingerprints(fixture.root)
        state = {'submits': 0, 'refresh_fail': False, 'race': False}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route(request):
                req = request.request
                path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                    return
                if path == '/projects' and state['refresh_fail']:
                    request.fulfill(status=503, json={'detail': 'refresh unavailable'})
                    return
                if path in ('/projects/import/prepare', '/projects/import/preview', '/projects/import', '/projects', '/members') or '/workspaces' in path:
                    if path == '/projects/import':
                        state['submits'] += 1
                        if state['race']:
                            taskpool.project_add('race-winner', req.post_data_json['root'])
                    response = fixture.client.request(req.method, req.url, json=req.post_data_json if req.method in ('POST', 'PUT') else None)
                    request.fulfill(status=response.status_code, json=response.json())
                    return
                empty = {'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'pending_approval': []}
                request.fulfill(json=empty)

            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            page.evaluate('showLogin(false); S.projects=[]; S.members=[]; renderProjectBar();')
            page.evaluate('openProjectNew()')
            page.locator('#project-entry-import').click()
            page.locator('#import-root').fill('E:/Code/html-studio/')
            assert page.locator('#import-name').input_value() == 'html-studio'
            page.locator('#import-root').fill('E:\\Code\\中文项目\\')
            assert page.locator('#import-name').input_value() == '中文项目'
            page.locator('#import-name').fill('自定义名称')
            page.locator('#import-root').fill('E:/Code/another')
            assert page.locator('#import-name').input_value() == '自定义名称'
            page.locator('#import-name').fill('')
            page.locator('#import-root').fill('E:/Code/html-studio')
            assert page.locator('#import-name').input_value() == 'html-studio'
            # Exercise Chromium's autofill pseudo state in both actual themes.
            cdp = page.context.new_cdp_session(page)
            cdp.send('DOM.enable')
            cdp.send('CSS.enable')
            document = cdp.send('DOM.getDocument')
            node = cdp.send('DOM.querySelector', {'nodeId': document['root']['nodeId'], 'selector': '#import-name'})
            cdp.send('CSS.forcePseudoState', {'nodeId': node['nodeId'], 'forcedPseudoClasses': ['autofill']})
            for theme in ('dark', 'light'):
                page.evaluate('(theme) => applyTheme(theme)', theme)
                styles = page.locator('#import-name').evaluate('''input => {
                    const style = getComputedStyle(input);
                    const sample = document.createElement('span');
                    sample.style.cssText = 'color:var(--fg);background:var(--bg)';
                    input.parentElement.append(sample);
                    const expected = getComputedStyle(sample);
                    const result = {autofill:input.matches(':autofill'), shadow:style.boxShadow,
                        text:style.webkitTextFillColor, fg:expected.color, bg:expected.backgroundColor};
                    sample.remove(); return result;
                }''')
                assert styles['autofill']
                assert styles['bg'] in styles['shadow'], styles
                assert styles['text'] == styles['fg'], styles
                if screenshot_dir:
                    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                    page.locator('#dlg-project-import').screenshot(path=str(Path(screenshot_dir) / f'import-name-{theme}.png'))
            cdp.send('CSS.forcePseudoState', {'nodeId': node['nodeId'], 'forcedPseudoClasses': []})
            cdp.detach()
            page.evaluate("applyTheme('dark')")
            page.locator('#import-root').fill('relative')
            page.locator('#import-preview').click()
            page.wait_for_function("document.querySelector('#import-status').textContent.includes('预览失败')")
            page.locator('#import-root').fill(str(fixture.root))
            page.locator('#import-name').fill('Chosen imported name')
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            page.locator('#import-supplement').uncheck()
            assert page.locator('#import-git-init').is_checked()
            page.locator('#import-git-init').uncheck()
            page.locator('#import-config > details > summary').click()
            page.locator('#import-documents details').filter(has_text='doc/design.md').locator('summary').click()
            assert 'Existing document' in page.locator('#import-documents').inner_text()
            assert page.locator('#import-documents img').count() == 0
            assert page.evaluate('window.previewInjection') is None
            assert '第 2 步' in page.locator('#import-step').inner_text()
            assert page.locator('#import-role-step').is_hidden()
            page.select_option('#import-mode', 'worktree')
            assert page.locator('#import-worktree').input_value().endswith('-worktrees')
            page.locator('#import-worktree').fill('')
            page.locator('#import-next').click()
            assert '请填写 worktree' in page.locator('#import-status').inner_text()
            page.select_option('#import-mode', 'shared')
            page.locator('#import-next').click()
            assert '第 3 步' in page.locator('#import-step').inner_text()
            page.locator('#import-role-name').fill('editor')
            page.locator('#import-role-add').click()
            editor = page.locator('#import-roles .import-card').filter(has=page.locator('strong', has_text='editor'))
            assert editor.locator('details').get_attribute('open') is None
            editor.locator('summary').click()
            editor.locator('input[value="task.create"]').check()
            if screenshot_dir:
                Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                page.locator('#dlg-project-import').screenshot(path=str(Path(screenshot_dir) / 'import-roles-desktop.png'))
            page.locator('#import-next').click()
            assert '第 4 步' in page.locator('#import-step').inner_text()
            assert 'Keep this workflow' not in page.locator('#import-agents-content').input_value()
            page.locator('#import-next').click()
            assert '请编辑并确认' in page.locator('#import-status').inner_text()
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            assert '第 5 步' in page.locator('#import-step').inner_text()
            # Selecting the owner role also selects the notification owner.
            assert page.locator('#import-owner').count() == 0
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[0]}"]').check()
            page.locator('#import-members select').first.select_option('项目负责人')
            assert page.locator(f'#import-members input[data-bot-id="{fixture.ids[0]}"]').is_checked()
            assert page.locator('#import-members select').input_value() == '项目负责人'
            if screenshot_dir:
                page.locator('#dlg-project-import').screenshot(path=str(Path(screenshot_dir) / 'import-owner-default.png'))
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[0]}"]').uncheck()
            assert page.locator('input[data-owner-bot-id]:checked').count() == 0
            for bot_id in fixture.ids:
                page.locator(f'#import-members input[data-bot-id="{bot_id}"]').check()
            selects = page.locator('#import-members select')
            for i in range(2):
                selects.nth(i).select_option('editor')
            personal = page.locator('#import-members .import-member').nth(0)
            personal.locator('summary').click()
            personal.locator('input[value="req.create"]').check()
            selects.first.select_option('项目负责人')
            selects.nth(1).select_option('项目负责人')
            assert selects.first.input_value() == ''
            selects.first.select_option('项目负责人')
            selects.nth(1).select_option('editor')
            assert page.locator('input[data-owner-bot-id]').count() == 0
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            assert page.locator('#import-resources > details').count() == 1
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            assert '创建任务' in page.locator('#import-review').inner_text()
            assert '创建需求' in page.locator('#import-review').inner_text()
            page.locator('#import-back').click()
            page.evaluate("document.querySelector('#import-submit').click()")
            assert state['submits'] == 0, 'Returning to edit must prevent stale confirmation submission'
            assert page.locator('#import-name').input_value() == 'Chosen imported name'
            assert selects.first.input_value() == '项目负责人'
            page.locator('#import-back').click()
            page.locator('#import-back').click()
            page.locator('#import-back').click()
            assert editor.locator('input[value="task.create"]').is_checked()
            page.locator('#import-next').click()
            page.locator('#import-next').click()
            assert personal.locator('input[value="req.create"]').is_checked()
            for _ in range(4): page.locator('#import-back').click()
            assert page.locator('#import-name').input_value() == 'Chosen imported name'
            assert page.locator('#import-root').input_value() == str(fixture.root)
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            page.locator('#import-next').click(); page.locator('#import-next').click()
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            assert selects.first.input_value() == '项目负责人'
            assert personal.locator('input[value="req.create"]').is_checked()
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            page.locator('#import-next').click()
            page.locator('#import-submit').wait_for(state='visible')
            page.evaluate("document.querySelector('#import-submit').click(); document.querySelector('#import-submit').click();")
            page.wait_for_function("S.currentProject && S.currentProject.startsWith('p_')")
            assert state['submits'] == 1
            project = page.evaluate('S.currentProject')
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_members WHERE project=?', (project,)).fetchone()[0] == 2
            page.locator('#app-alert .primary').click()
            after = fingerprints(fixture.root)
            for path, digest in before.items():
                if path != 'AGENTS.md': assert after[path] == digest, path
            assert next(fixture.root.glob('AGENTS.md.backup-*')).read_bytes() == b'# Original rules\r\nKeep this workflow\r\n'
            page.evaluate('''async project => {
              const data = await api(`/projects/${project}/workspaces`);
              projectMembersCache.set(project, data);
              openWorkspaceEditor(project, data.members[0]);
            }''', project)
            resource_dialog = page.locator('dialog').filter(has=page.locator('h3', has_text='工作区资源配置'))
            resource_dialog.get_by_label('分配浏览器调试资源').uncheck()
            resource_dialog.get_by_role('button', name='保存资源配置').click()
            resource_dialog.get_by_role('status').filter(has_text='已保存').wait_for()
            resource_dialog.get_by_role('button', name='关闭', exact=True).click()
            import json
            assert json.loads(fixture.conn.execute('SELECT config FROM workspace_resources WHERE project=?', (project,)).fetchone()[0])['browser'] is None

            # A registered directory opens directly by stable ID.
            taskpool.project_add('legacy-record', str(fixture.root))
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(fixture.root).swapcase())
            page.locator('#import-preview').click()
            page.wait_for_function("S.currentProject === 'legacy-record'")

            # Preview/submit race: real second directory registration wins the writer lock.
            racing = fixture.base / 'racing'
            racing.mkdir()
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(racing))
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            for _ in range(5):
                if page.locator('#import-agents-step').is_visible(): page.locator('#import-agents-reviewed').check()
                page.locator('#import-next').click()
            state['race'] = True
            page.locator('#import-submit').click()
            page.locator('#import-existing button').wait_for()
            assert 'race-winner' in page.locator('#import-existing').inner_text()
            assert page.locator('#import-submit').is_hidden()
            state['race'] = False
            page.locator('#import-existing button').click()
            page.wait_for_function("S.currentProject === 'race-winner'")

            # Registration has committed; retry opening after list refresh fails.
            third = fixture.base / 'third'
            third.mkdir()
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(third))
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            for _ in range(5):
                if page.locator('#import-agents-step').is_visible(): page.locator('#import-agents-reviewed').check()
                page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            assert '新增：AGENTS.md' in page.locator('#import-review').inner_text()
            state['refresh_fail'] = True
            page.locator('#import-submit').click()
            page.wait_for_function("document.querySelector('#import-status').textContent.includes('打开失败')")
            assert page.locator('#import-submit').is_hidden()
            assert page.locator('#import-existing button').count() == 1
            state['refresh_fail'] = False
            page.locator('#app-alert .primary').click()
            page.locator('#import-existing button').click()
            page.locator('#dlg-project-import').wait_for(state='hidden')
            assert fixture.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 4
            assert (third / 'AGENTS.md').is_file()
            assert (third / 'docs' / 'INDEX.md').is_file()
            assert (third / '.git' / 'HEAD').is_file()
            assert (third / '.gitignore').is_file()

            page.set_viewport_size({'width': 390, 'height': 844})
            page.evaluate('openProjectImport()')
            assert page.evaluate("document.querySelector('#dlg-project-import').scrollWidth <= document.querySelector('#dlg-project-import').clientWidth")
            page.locator('#import-root').fill(str(third))
            page.locator('#import-preview').click()
            page.locator('#dlg-project-import').wait_for(state='hidden')
            fourth = fixture.base / 'fourth'
            fourth.mkdir()
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(fourth))
            page.locator('#import-template').select_option('research')
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            page.locator('#import-next').click()
            assert '调研负责人' in page.locator('#import-roles').inner_text()
            page.locator('#import-roles .import-card').filter(has=page.locator('strong', has_text='资料研究员')).get_by_role('button', name='移除角色').click()
            assert '资料研究员' not in page.locator('#import-roles').inner_text()
            page.locator('#import-role-name').fill('LongRole' * 12)
            page.locator('#import-role-add').click()
            for i in range(12):
                page.locator('#import-role-name').fill(f'Role {i}')
                page.locator('#import-role-add').click()
            footer = page.locator('.import-footer').bounding_box()
            page.locator('.import-body').evaluate('(el) => el.scrollTop = el.scrollHeight')
            assert page.locator('.import-footer').bounding_box() == footer
            assert page.evaluate("(() => { const b=document.querySelector('#import-next').getBoundingClientRect(); return b.bottom <= innerHeight && document.elementFromPoint(b.x+b.width/2,b.y+b.height/2).id==='import-next'; })()")
            page.locator('#import-next').click()
            page.locator('#import-agents-content').fill('# Custom workflow\n项目负责人安排 LongRole 执行。')
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-back').click()
            page.locator('#import-role-name').fill('New specialist')
            page.locator('#import-role-add').click()
            page.locator('#import-next').click()
            assert 'Custom workflow' in page.locator('#import-agents-content').input_value()
            assert not page.locator('#import-agents-reviewed').is_checked()
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            page.locator(f'#import-members input[data-bot-id="{fixture.ids[0]}"]').check()
            page.locator('#import-members select').select_option('LongRole' * 12)
            page.locator('#import-members summary').click()
            for theme in ('dark', 'light'):
                page.evaluate('(theme) => applyTheme(theme)', theme)
                checkbox = page.locator('#import-members input[data-bot-id]').first
                assert checkbox.evaluate('(el) => getComputedStyle(el).appearance') == 'none'
                assert checkbox.evaluate('(el) => getComputedStyle(el).width') == '14px'
                if screenshot_dir:
                    page.locator('#dlg-project-import').screenshot(path=str(Path(screenshot_dir) / f'import-members-390-{theme}.png'))
            assert page.evaluate("document.querySelector('#dlg-project-import').scrollWidth <= document.querySelector('#dlg-project-import').clientWidth")
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            page.locator('#import-next').click()
            assert page.evaluate("document.querySelector('#dlg-project-import').scrollWidth <= document.querySelector('#dlg-project-import').clientWidth")
            page.locator('#import-submit').click()
            page.locator('#dlg-project-import').wait_for(state='hidden')
            assert (fourth / 'AGENTS.md').read_text(encoding='utf-8').startswith('# Custom workflow')
            page.locator('#app-alert .primary').click()
            fifth = fixture.base / 'fifth'
            fifth.mkdir()
            original_agents = b'# Existing workflow\r\nOriginal rules\r\n'
            (fifth / 'AGENTS.md').write_bytes(original_agents)
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(fifth))
            page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            page.locator('#import-next').click()
            page.locator('#import-next').click()
            page.locator('#import-agents-content').fill('# Updated workflow\n项目负责人协调交付。\n')
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible')
            assert '修改：AGENTS.md' in page.locator('#import-review').inner_text()
            assert '+# Updated workflow' in page.locator('#import-review').inner_text()
            assert (fifth / 'AGENTS.md').read_bytes() == original_agents
            page.locator('#import-submit').click()
            page.locator('#dlg-project-import').wait_for(state='hidden')
            assert (fifth / 'AGENTS.md').read_text(encoding='utf-8').startswith('# Updated workflow')
            assert next(fifth.glob('AGENTS.md.backup-*')).read_bytes() == original_agents
            assert not errors, errors
            browser.close()
        print('PASS: seven steps, shared resource editor, editable rules, role deletion, retained permissions, unchanged CRLF file, race, retry, 390px')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--screenshot-dir'); args = parser.parse_args()
    main(args.screenshot_dir)
