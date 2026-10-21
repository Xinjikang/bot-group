"""Project SOUL drafts, plus real creation/reference API integration in Chrome."""
import mimetypes
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_project_import_supplement import SupplementTests


def main():
    writes, reads, pending, errors = [], [], [], []
    state = {'fail': False, 'delay': False, 'mismatch': False, 'real_client': None}
    soul = '# 当前项目负责人\n只协调本项目。\n<img src=x onerror="window.badSoul=true">'
    def template(project):
        return {'project': project, 'template': None if project == 'blank' else {
            'id': 'software', 'name': '软件项目', 'version': 'latest-version', 'references': [
                {'role_id': 'lead', 'name': project + ' 负责人', 'responsibilities': '协调与验收',
                 'content': soul if project == 'p' else '# 其他项目的专用 SOUL'},
                {'role_id': 'dev', 'name': project + ' 开发', 'responsibilities': '按职责开发', 'content': '# 当前项目开发模板'},
            ]}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(req):
            path = unquote(urlparse(req.request.url).path)
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if req.request.method in ('POST', 'PUT', 'PATCH'):
                writes.append((path, req.request.post_data_json))
            data = {}
            if path.endswith('/template-reference'):
                reads.append(path)
                if state['real_client']:
                    response = state['real_client'].get(path)
                    req.fulfill(status=response.status_code, json=response.json())
                    return
                if state['delay']:
                    pending.append(req)
                    return
                if state['fail']:
                    req.fulfill(status=503, json={'detail': '暂时不可用'})
                    return
                data = template('foreign' if state['mismatch'] else path.split('/')[2])
            elif path.startswith('/projects/templates'):
                raise AssertionError('Profile editors must never load the global template catalogue')
            elif path == '/discovery/scan':
                data = {'candidates': []}
            elif path.endswith('/config'):
                handle = path.split('/')[2]
                data = {'revision': 'saved', 'member_profile': handle, 'runtime': 'codex' if handle == 'c' else 'hermes',
                        'soul': '已有人格正文', 'description': '', 'env': '', 'model': '', 'provider': '', 'reasoning_effort': ''}
            elif path.endswith('/model'):
                data = {'project': path.split('/')[2], 'member_profile': path.split('/')[4], 'revision': 'model-revision',
                        'model': '', 'provider': '', 'reasoning_effort': ''}
            elif path == '/profiles/create':
                data = {'created': True, 'notice': '已创建并加入项目'}
            elif path.endswith('/workspaces') or path == '/members':
                data = {'members': []}
            elif path == '/projects':
                data = {'projects': []}
            elif path in ('/messages', '/threads', '/tasks'):
                data = {'messages': [], 'threads': [], 'tasks': [], 'requirements': []}
            req.fulfill(json=data)
        page.route('**/*', route)
        page.goto('http://soul.test/app/index.html')
        page.evaluate("() => { showLogin(false); S.currentProject='p'; S.projects=[{name:'p',root:'E:/test',directory_mode:'shared'}]; openProjectDetail('p', $('#chip-all')); selectProjectTab('members'); }")
        page.locator('#pj-members-add').click()
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.wait_for_function("!memberAdd.scanning")
        page.locator('[name=member-add-mode][value=create]').check()
        page.locator('#member-create-profile').fill('new-person')
        page.locator('#member-create-soul-templates button').click()
        page.locator('#profile-soul-template-apply').wait_for(state='visible')
        page.wait_for_function("!document.querySelector('#profile-soul-template-apply').disabled")
        assert page.locator('#profile-soul-template-preview').text_content() == soul
        assert page.locator('#profile-soul-template-preview img').count() == 0
        assert page.locator('#member-create-instructions').input_value() == ''
        assert writes == []
        out = ROOT / 'cache' / 'profile-soul-templates'
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / 'desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate("applyTheme('light')")
        page.screenshot(path=str(out / 'mobile-light.png'))
        assert page.locator('#dlg-profile-soul-template').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        page.locator('#profile-soul-template-apply').click()
        page.locator('#dlg-profile-soul-template').wait_for(state='hidden')
        assert page.locator('#member-create-instructions').is_visible()
        assert page.locator('#member-create-instructions').input_value() == soul
        assert page.locator('#dlg-project-detail').evaluate('el => el.open')
        assert writes == []
        page.locator('#member-create-instructions').fill(soul + '\n补充要求')
        page.locator('#member-add-join').click()
        page.locator('#dlg-member-add').wait_for(state='hidden')
        assert writes[-1][1]['instructions'] == soul + '\n补充要求'
        page.evaluate("closeDialogAnchored($('#dlg-project-detail'))")

        def edit(project='p', runtime='hermes'):
            page.evaluate("([project,runtime]) => { S.currentProject=project; openProfileEditor({profile:runtime==='codex'?'c':'h',runtime}, project || null); }", [project, runtime])
            page.wait_for_function('profileEditor?.revision != null')
            page.locator('#profile-tab-soul').click()
        def close_editor():
            page.locator('#profile-cancel').click()
            page.wait_for_function('profileEditor === null')
        def picker():
            page.locator('#profile-soul-templates button').click()
            page.wait_for_function("!document.querySelector('#profile-soul-template-retry').disabled")

        # Reading and a cancelled overwrite preserve the existing SOUL for both runtimes.
        for runtime in ('hermes', 'codex'):
            edit(runtime=runtime)
            before = len(writes)
            picker()
            assert 'foreign' not in page.locator('#profile-soul-template-role').inner_text()
            page.locator('#profile-soul-template-apply').click()
            page.locator('#app-alert button', has_text='取消').click()
            assert page.locator('#profile-soul').input_value() == '已有人格正文'
            assert len(writes) == before
            page.locator('#profile-soul-template-role').select_option('1')
            page.locator('#profile-soul-template-apply').click()
            page.locator('#app-alert button.primary').click()
            page.locator('#dlg-profile-soul-template').wait_for(state='hidden')
            assert page.locator('#profile-soul').input_value() == '# 当前项目开发模板'
            assert len(writes) == before
            page.locator('#profile-soul').fill('# 当前项目开发模板\n可继续编辑')
            page.locator('#profile-save').click()
            page.wait_for_function('profileEditor === null')
            assert writes[-1][1]['soul'] == '# 当前项目开发模板\n可继续编辑'

        edit('blank'); picker()
        assert page.locator('#profile-soul-template-apply').is_disabled()
        assert '没有可用' in page.locator('#profile-soul-template-status').inner_text()
        page.locator('#profile-soul-template-close').click(); close_editor()
        edit('')
        assert page.locator('#profile-soul-templates button').is_disabled()
        close_editor()
        edit()
        state['fail'] = True; picker()
        assert '读取失败' in page.locator('#profile-soul-template-status').inner_text()
        state.update(fail=False, mismatch=True)
        page.locator('#profile-soul-template-retry').click()
        page.wait_for_function("document.querySelector('#profile-soul-template-status').textContent.includes('不一致')")
        assert page.locator('#profile-soul-template-apply').is_disabled()
        state['mismatch'] = False
        page.locator('#profile-soul-template-retry').click()
        page.wait_for_function("!document.querySelector('#profile-soul-template-apply').disabled")
        page.locator('#profile-soul-template-close').click(); close_editor()
        # A pending response from p must never populate a newly opened q editor.
        edit()
        state['delay'] = True
        page.locator('#profile-soul-templates button').click()
        page.wait_for_function("document.querySelector('#profile-soul-template-retry').disabled")
        page.locator('#profile-soul-template-close').click(); close_editor()
        state['delay'] = False
        edit('q'); picker()
        pending.pop().fulfill(json=template('p'))
        assert page.locator('#profile-soul-template-preview').text_content() == '# 其他项目的专用 SOUL'
        assert all(path in ('/projects/p/template-reference', '/projects/q/template-reference', '/projects/blank/template-reference') for path in reads)
        page.locator('#profile-soul-template-close').click(); close_editor()

        # Real current creation flow persists only a template type; both entry
        # points must populate from the actual project-scoped reference endpoint.
        fixture = SupplementTests(); fixture.setUp()
        try:
            root = fixture.base / 'new-research'; root.mkdir()
            body, _ = fixture.reviewed({**fixture.configured('research'), 'root': str(root), 'creation_mode': 'new'})
            created = fixture.submit(body)
            assert created.status_code == 200, created.text
            project = created.json()['project']
            assert fixture.conn.execute('SELECT COUNT(*) FROM project_initializations').fetchone()[0] == 0
            expected = fixture.client.get('/projects/templates/research').json()['references']
            state['real_client'] = fixture.client
            page.evaluate("p => { S.currentProject=p; S.projects=[{name:p,root:'E:/test',directory_mode:'shared'}]; openProjectDetail(p, $('#chip-all')); selectProjectTab('members'); }", project)
            page.locator('#pj-members-add').click()
            page.locator('[name=member-add-host][value=codex]').check()
            page.locator('#member-add-next').click()
            page.wait_for_function('!memberAdd.scanning')
            page.locator('[name=member-add-mode][value=create]').check()
            page.locator('#member-create-soul-templates button').click()
            page.wait_for_function("!document.querySelector('#profile-soul-template-retry').disabled")
            assert page.locator('#profile-soul-template-role option').all_text_contents() == [r['name'] for r in expected]
            assert '最新参考' in page.locator('#profile-soul-template-project').inner_text()
            page.locator('#profile-soul-template-apply').click()
            assert page.locator('#member-create-instructions').input_value() == expected[0]['content']
            page.locator('#member-add-close').click()
            page.evaluate("closeDialogAnchored($('#dlg-project-detail'))")
            for runtime in ('hermes', 'codex'):
                edit(project, runtime); picker()
                assert page.locator('#profile-soul-template-role option').all_text_contents() == [r['name'] for r in expected]
                page.locator('#profile-soul-template-role').select_option('1')
                assert page.locator('#profile-soul-template-preview').text_content() == expected[1]['content']
                page.locator('#profile-soul-template-apply').click()
                page.locator('#app-alert button.primary').click()
                page.locator('#dlg-profile-soul-template').wait_for(state='hidden')
                assert page.locator('#profile-soul').input_value() == expected[1]['content']
                close_editor()
        finally:
            state['real_client'] = None
            fixture.doCleanups()
        assert not errors, errors
        browser.close()
    print('PASS: project-only SOUL references, create/edit drafts, preview, confirmation, retry, stale responses and narrow layout')


if __name__ == '__main__':
    main()
