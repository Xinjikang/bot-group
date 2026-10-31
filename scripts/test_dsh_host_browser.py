"""Real Chrome, synthetic HTTP: DSH host entry, scan feedback and HOME fallback (req#137 task#263).

覆盖：
- 宿主固定三项 Hermes/Codex/DSH；选择 DSH 自动扫描。
- 扫描成功：只列 dsh 候选、自动带出 profile，创建前调用 /bots/validate-dsh，再走 /bots/register。
- 扫描为空/异常：保留入口，提示原因，提供选目录、手动 HOME、重新扫描；空态可继续校验接入。
- 校验错误逐项展示并保留已填 HOME/profile；不会自动绑定到其他 profile。
- 已创建 dsh Bot 的目录/profile 失效时，成员卡片显示绑定失效。
- 深色/浅色 × 390px：三项宿主与手动路径不溢出。
"""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DSH_ROOT = 'C:/dsh-home'
MANUAL_ROOT = 'C:/manual-dsh'


def run(evidence):
    state = {'scan': 'ok', 'validate_ok': True, 'candidates': []}
    scans, validates, registers, creates = [], [], [], []

    def route(request):
        path = urlparse(request.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            if file.is_file():
                request.fulfill(body=file.read_bytes(),
                                content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                request.fulfill(status=404)
            return
        if path == '/auth/status':
            request.fulfill(json={'configured': False}); return
        if path == '/discovery/scan':
            scans.append(path)
            if state['scan'] == 'error':
                request.fulfill(status=503, json={'detail': '模拟扫描失败'}); return
            candidates = [{'host_type': 'hermes', 'profile': 'default', 'config_root': 'C:/hermes'}]
            if state['scan'] == 'ok':
                candidates.append({'host_type': 'dsh', 'profile': 'dsh-a', 'config_root': DSH_ROOT})
            request.fulfill(json={'candidates': candidates}); return
        if path == '/bots/validate-dsh':
            body = request.request.post_data_json
            validates.append(body)
            errors = [] if state['validate_ok'] else ['目录不存在', 'profile 目录不存在']
            request.fulfill(json={'ok': not errors, 'errors': errors,
                                  'config_root': body.get('config_root', ''),
                                  'profile': body.get('profile', '')}); return
        if path == '/bots/register':
            body = request.request.post_data_json
            registers.append(body)
            request.fulfill(json={'bot_id': 77, 'created': True, 'profile': body.get('profile'),
                                  'member_profile': None, 'project': None}); return
        if path == '/profiles/create':
            creates.append(request.request.post_data_json)
            request.fulfill(status=500, json={'detail': '不应走 profile_create'}); return
        if path == '/profiles/candidates':
            request.fulfill(json={'candidates': state['candidates']}); return
        if path == '/projects/p/workspaces':
            member = {'profile': 'bot-77', 'display_name': 'DSH Bot', 'host_profile': 'dsh-a',
                      'runtime': 'dsh', 'bot_id': 77, 'active': 1, 'role': '',
                      'workspace': {'valid': True, 'path': 'E:/work/dsh-a'},
                      'directory_mode': 'shared'}
            request.fulfill(json={'project': {'name': 'p', 'directory_mode': 'shared', 'worktree_root': ''},
                                  'members': [member], 'history': [], 'repository': {}}); return
        if path == '/thinking':
            request.fulfill(json={'runs': [], 'thinking': []}); return
        if path == '/events':
            request.fulfill(body=': ok\n\n', content_type='text/event-stream'); return
        if path == '/hud' or path.startswith('/hud/'):
            request.fulfill(json={'id': 'hud-test', 'status': 'idle', 'state': {}, 'entries': [],
                                  'revision': 1, 'project': None}); return
        request.fulfill(json={'projects': [], 'members': [], 'threads': [], 'messages': [], 'tasks': [],
                              'requirements': [], 'pending_approval': [], 'runs': []})

    def open_dialog(page):
        page.evaluate("""() => { window.__created = null;
            openProfileCreate({project: 'p', onCreated: item => { window.__created = item; }}); }""")
        page.wait_for_function("document.querySelector('#dlg-member-add')?.open === true")
        page.wait_for_function('memberAdd && !memberAdd.scanning')

    def select_dsh(page):
        page.locator('[name="member-add-host"][value="dsh"]').check()
        page.wait_for_function("memberAdd.host === 'dsh' && !memberAdd.scanning")

    def status(page):
        return page.locator('#member-add-status').text_content()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://dsh.test/app/')
        page.wait_for_function('typeof openProfileCreate === "function"')

        # ---- ① 固定三项宿主 + 扫描成功 ----
        open_dialog(page)
        hosts = page.locator('[name="member-add-host"]')
        assert hosts.count() == 3
        assert [hosts.nth(i).get_attribute('value') for i in range(3)] == ['hermes', 'codex', 'dsh']
        select_dsh(page)
        assert '已发现 1 个 DSH profile' in status(page), status(page)
        assert page.locator('#member-create-source option').count() == 1
        assert 'dsh-a' in page.locator('#member-create-source').inner_text()
        assert page.locator('#member-create-profile').input_value() == 'dsh-a'
        assert page.locator('#member-create-profile').evaluate('e => e.readOnly')
        assert page.locator('#member-create-dsh-home').count() == 0
        assert page.locator('#member-add-join').inner_text() == '校验并接入'
        assert not page.locator('#member-add-join').is_disabled()
        page.locator('#member-add-join').click()
        page.wait_for_function('window.__created !== null')
        assert validates == [{'config_root': DSH_ROOT, 'profile': 'dsh-a'}], validates
        assert registers == [{'host_type': 'dsh', 'profile': 'dsh-a',
                              'config_root': DSH_ROOT, 'display_name': ''}], registers
        assert not creates, creates
        created = page.evaluate('window.__created')
        assert created['bot_id'] == 77 and created['host_type'] == 'dsh'
        assert created['profile'] == 'dsh-a' and created['config_root'] == DSH_ROOT
        assert page.locator('#dlg-member-add').is_hidden()

        # ---- ② 扫描为空：提示原因、手动 HOME + 重新扫描、校验失败保留输入 ----
        state['scan'] = 'empty'
        state['validate_ok'] = False
        open_dialog(page)
        select_dsh(page)
        assert '未发现 DSH 环境配置' in status(page) and 'DSH_HOME' in status(page), status(page)
        assert page.locator('#member-create-source-block').is_hidden()
        assert page.locator('#member-create-dsh-home').count() == 1
        assert page.locator('#member-create-dsh-home').input_value() == ''
        assert page.locator('[aria-label="选择 DSH HOME 目录"]').count() == 1
        assert page.locator('#member-add-join').is_disabled()
        before = len(scans)
        page.locator('#member-create-dsh-rescan').click()
        page.wait_for_function('!memberAdd.scanning')
        assert len(scans) == before + 1
        page.locator('#member-create-dsh-home').fill(MANUAL_ROOT)
        page.locator('#member-create-profile').fill('manual-a')
        assert not page.locator('#member-add-join').is_disabled()
        page.locator('#member-add-join').click()
        page.wait_for_function('document.querySelector("#member-add-status").textContent.includes("校验失败")')
        fail_text = status(page)
        assert '目录不存在' in fail_text and 'profile 目录不存在' in fail_text, fail_text
        assert '已保留' in fail_text, fail_text
        assert page.locator('#member-create-dsh-home').input_value() == MANUAL_ROOT
        assert page.locator('#member-create-profile').input_value() == 'manual-a'
        assert validates[-1] == {'config_root': MANUAL_ROOT, 'profile': 'manual-a'}, validates
        assert len(registers) == 1, registers
        page.locator('#dlg-member-add').screenshot(path=str(evidence / 'dsh-scan-empty-validation.png'))

        # 校验通过后继续接入，使用手动路径。
        state['validate_ok'] = True
        page.locator('#member-add-join').click()
        page.wait_for_function('window.__created !== null')
        assert validates[-1] == {'config_root': MANUAL_ROOT, 'profile': 'manual-a'}, validates
        assert registers[-1] == {'host_type': 'dsh', 'profile': 'manual-a',
                                 'config_root': MANUAL_ROOT, 'display_name': ''}, registers
        assert page.evaluate('window.__created')['config_root'] == MANUAL_ROOT

        # ---- ③ 扫描异常：保留入口并给出异常原因 ----
        state['scan'] = 'error'
        open_dialog(page)
        select_dsh(page)
        assert 'DSH 扫描失败' in status(page) and '模拟扫描失败' in status(page), status(page)
        assert page.locator('#member-create-dsh-home').count() == 1
        assert page.locator('#member-add-join').is_disabled()
        for theme in ('dark', 'light'):
            page.evaluate('theme => { document.documentElement.dataset.theme = theme }', theme)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_timeout(120)
            assert page.locator('#dlg-member-add').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
            assert page.locator('[name="member-add-host"]').count() == 3
            assert page.locator('#member-create-dsh-home').count() == 1
            page.locator('#dlg-member-add').screenshot(path=str(evidence / f'dsh-scan-error-390-{theme}.png'))
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.locator('#member-add-close').click()

        # ---- ④ 已创建 dsh Bot 的绑定资源失效 ----
        state['candidates'] = []
        page.evaluate("""async () => {
            projectMembersCache.clear(); projectMembersVersions.clear();
            document.querySelector('#pj-detail-form').dataset.origName = 'p';
            document.querySelector('#dlg-project-detail').showModal();
            document.querySelector('#pj-tab-members').hidden = false;
            selectProjectTab('members');
            await loadProjectMembers();
        }""")
        marker = page.locator('[data-binding-invalid="true"]')
        marker.wait_for()
        assert '宿主绑定失效' in marker.inner_text(), marker.inner_text()
        assert '成员与历史保留' in marker.inner_text(), marker.inner_text()
        page.locator('#pj-member-list').screenshot(path=str(evidence / 'dsh-binding-invalid.png'))
        state['candidates'] = [{'host_type': 'dsh', 'profile': 'dsh-a', 'config_root': DSH_ROOT,
                                'bot_id': 77, 'bound': True, 'already_project_member': True}]
        page.evaluate("""async () => {
            projectMembersCache.clear(); projectMembersVersions.clear();
            await loadProjectMembers();
        }""")
        page.wait_for_function('!document.querySelector("[data-binding-invalid=\\"true\\"]")')
        assert page.locator('#pj-member-list').inner_text().find('宿主绑定失效') == -1
        page.locator('#pj-member-list').screenshot(path=str(evidence / 'dsh-binding-invalid-and-recovered.png'))

        assert not errors, errors
        browser.close()

    print('PASS: DSH host entry, scan success/empty/error, manual HOME + rescan, validate-before-create, '
          'input retention, register payload, binding invalidation, 390px dark/light')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
