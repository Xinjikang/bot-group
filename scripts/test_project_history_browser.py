"""Removed-project structured defaults, fresh rules, preserved assets and reminder."""
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_import import ImportTests

ROOT = Path(__file__).resolve().parents[1]


def main():
    fixture = ImportTests(); fixture.setUp()
    try:
        original = (fixture.root / 'AGENTS.md').read_bytes()
        (fixture.root / 'scripts').mkdir()
        (fixture.root / 'scripts' / 'test_user.py').write_bytes(b'# Existing test\r\n')
        assets = {name: (fixture.root / name).read_bytes() for name in ('doc/design.md', 'scripts/test_user.py', 'code.bin')}
        old = fixture.submit().json()['project']
        from engine import project_team
        old_owner = project_team.create_role(fixture.conn, old, '项目负责人', 'user')['id']
        current_owner = project_team.create_role(fixture.conn, old, '调研负责人', 'user')['id']
        project_team.save_owner_role(fixture.conn, old, old_owner)
        for role_id, permission in ((old_owner, 'req.create'), (current_owner, 'task.create')):
            fixture.conn.execute('INSERT INTO project_permissions(project,profile,permission) VALUES(?,?,?)',
                                 (old, 'role:' + str(role_id), permission))
        fixture.conn.execute("INSERT INTO project_config_events(project,actor,action,detail,created_at) VALUES(?,'user','project.import',?,'now')",
                             (old, json.dumps({'template': {'id': 'research', 'version': 'old'}})))
        from engine import project_workspaces
        resources = {'services': {}, 'browser': None, 'test_data_directory': str(fixture.base / 'historical-test-data')}
        fixture.conn.execute('INSERT INTO workspace_resources VALUES(?,?,?,?,1)',
                             (old, project_workspaces.key(str(fixture.root)), 'history', json.dumps(resources)))
        fixture.conn.commit()
        assert fixture.client.post(f'/projects/{old}/remove').status_code == 200
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 950})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            state = {'resource_conflict': True}
            def route(request):
                req = request.request; path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                elif path.startswith('/projects') or path == '/members':
                    if path == '/projects/import/preview' and 'workspace_resources' in (req.post_data_json or {}) and state['resource_conflict']:
                        state['resource_conflict'] = False
                        request.fulfill(status=409, json={'detail': 'historical resource conflict'}); return
                    response = fixture.client.request(req.method, req.url, json=req.post_data_json if req.post_data else None)
                    request.fulfill(status=response.status_code, json=response.json())
                else:
                    request.fulfill(json={'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'members': []})
            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            page.evaluate('showLogin(false); openProjectImport()')
            page.locator('#import-root').fill(str(fixture.root)); page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible')
            assert page.locator('#import-name').input_value() == fixture.body['display_name']
            assert page.locator('#import-description').input_value() == fixture.body['description']
            assert page.locator('#import-template').input_value() == 'research'
            page.locator('#import-next').click()
            assert 'editor' in page.locator('#import-roles').inner_text()
            assert page.locator('#import-roles strong').all_text_contents() == ['editor', '调研负责人']
            owner_card = page.locator('#import-roles > section').filter(has_text='调研负责人')
            assert '已启用 2 项' in owner_card.inner_text()
            assert owner_card.get_by_role('button', name='移除角色').count() == 0
            page.locator('#import-next').click()
            page.locator('#import-agents-step').wait_for(state='visible')
            assert 'Original rules' not in page.locator('#import-agents-content').input_value()
            page.locator('#import-agents-reviewed').check(); page.locator('#import-next').click()
            assert page.locator('#import-members input[data-bot-id]:checked').count() == 2
            assert page.locator('#import-members select').first.input_value() == 'editor'
            page.locator('#import-next').click()
            page.locator('#import-resource-step').wait_for(state='visible')
            assert 'historical resource conflict' in page.locator('#import-status').inner_text()
            assert page.locator('#import-resources').get_by_label('测试数据目录', exact=True).input_value() == resources['test_data_directory']
            page.locator('#import-next').click()
            page.locator('#import-review').wait_for(state='visible'); page.locator('#import-submit').click()
            page.locator('#app-alert').wait_for(state='visible')
            assert '请核对' in page.locator('#app-alert-message').inner_text()
            assert 'AGENTS.md' in page.locator('#app-alert-message').inner_text()
            for name, content in assets.items(): assert (fixture.root / name).read_bytes() == content, name
            assert next(fixture.root.glob('AGENTS.md.backup-*')).read_bytes() == original
            new = page.evaluate('S.currentProject'); assert new and new != old
            page.locator('#app-alert .primary').click()
            # Archived directories stay on the first step and offer no restore action.
            assert fixture.client.post(f'/projects/{new}/archive', json={'archived': True}).status_code == 200
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(fixture.root)); page.locator('#import-preview').click()
            page.wait_for_function("document.querySelector('#import-status').textContent.includes('已归档')")
            assert page.locator('#import-existing button').count() == 0
            assert '第 1 步' in page.locator('#import-step').inner_text()
            assert not errors, errors
            browser.close()
        print('PASS: historical name/description/template/roles/members, fresh rules backup, preserved docs/tests, completion reminder, archived notice')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
