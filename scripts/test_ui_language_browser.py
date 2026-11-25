"""Verify bilingual UI switching (req#178 task#333): settings entry, immediate
effect, persistence, message originality and per-page spot checks.

Uses isolated synthetic routes (no live backend); static assets are read from
frontend/. Run: env -u PYTHONPATH py -3.10 -X utf8 -B scripts/test_ui_language_browser.py --evidence <abs dir>
"""
import argparse
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
TOPICS = [dict(id=7, project='p', title='隔离话题', status='open', members=['bot'])]
OLD = dict(id=10, project='p', thread_id=7, sender='bot', sender_type='bot',
           content='原文消息：切换语言不能改变我', ts='2026-11-11T12:00:00', mentions='[]', attachments=[])
REQS = [dict(id=1, project='p', title='示例需求', status='open', archived=0)]
TASKS = [dict(id=2, project='p', requirement_id=1, title='示例任务', status='pending',
              claimed_by='', next_bot='bot', briefing_status='ready', created_at='2026-11-25T10:00:00')]


class Fixture:
    def __init__(self):
        self.posts = 0

    def route(self, route):
        url = urlsplit(route.request.url)
        path = url.path
        if path.startswith('/app/'):
            asset = (FRONTEND / (path[5:] or 'index.html')).resolve()
            if FRONTEND not in asset.parents or not asset.is_file():
                route.fulfill(status=404)
            else:
                route.fulfill(body=asset.read_bytes(),
                              content_type=mimetypes.guess_type(asset.name)[0] or 'text/plain')
            return
        if path == '/messages':
            topic = int(parse_qs(url.query).get('thread_id', ['0'])[0])
            route.fulfill(json={'messages': [OLD] if topic in (0, 7) else []})
            return
        if path == '/msg':
            self.posts += 1
            route.fulfill(json={'message_id': 99})
            return
        if path in ('/events', '/desktop/events'):
            route.fulfill(body=': test\n\n', content_type='text/event-stream')
            return
        if path.startswith('/projects/') and path.endswith('/workspaces'):
            route.fulfill(json={'project': {'name': 'p', 'display_name': '隔离项目', 'worktree_root': 'E:/wt'},
                                'members': [{'profile': 'bot', 'display_name': 'Bot', 'host_profile': 'bot',
                                             'active': 1, 'directory_mode': 'shared', 'remote_workspace': False,
                                             'remote_session_pending': False, 'binding_invalid': False,
                                             'workspace': {'path': 'E:/wt/bot', 'valid': True, 'head': 'abc123'}}]})
            return
        if path.endswith('/removal'):
            route.fulfill(json={'can_remove': True, 'blockers': []})
            return
        if path == '/local/directory-list':
            route.fulfill(json={'path': '', 'parent': None, 'entries': [], 'total': 0,
                                'offset': 0, 'limit': 200, 'has_more': False})
            return
        data = {
            '/auth/status': {'configured': False},
            '/projects': {'projects': [{'name': 'p', 'display_name': '隔离项目'}]},
            '/threads': {'threads': TOPICS},
            '/members': {'members': [{'profile': 'bot', 'display_name': 'Bot', 'active': 1}]},
            '/tasks': {'tasks': TASKS, 'requirements': REQS},
        }.get(path, {})
        route.fulfill(json=data)

    def start(self, context):
        context.route('**/*', self.route)
        context.add_init_script('''const realInterval = window.setInterval;
            window.setInterval = (fn, ms, ...args) => ms === 10000 ? 0 : realInterval(fn, ms, ...args);''')
        page = context.new_page()
        page.goto('http://ui-language.test/app/')
        page.wait_for_function('S.messagesReady')
        page.locator('[data-message-id="10"]').wait_for(timeout=3000)
        return page


def text_of(page, selector):
    return page.locator(selector).first.inner_text().strip()


def assert_zh(page):
    assert text_of(page, '#btn-settings span[data-i18n]') == '设置'
    assert text_of(page, '.col-threads .h-title') == '话题'
    assert text_of(page, '.col-tasks .h-title') == '任务'
    assert text_of(page, '#btn-send') == '发送'
    assert text_of(page, '.task-head .th-status') == '状态'


def assert_en(page):
    assert text_of(page, '#btn-settings span[data-i18n]') == 'Settings'
    assert text_of(page, '.col-threads .h-title') == 'Topics'
    assert text_of(page, '.col-tasks .h-title') == 'Tasks'
    assert text_of(page, '#btn-send') == 'Send'
    assert text_of(page, '.task-head .th-status') == 'Status'


def open_language_dialog(page):
    page.locator('#btn-settings').click()
    assert page.locator('#btn-language').inner_text().strip() == '语言 / Language'
    page.locator('#btn-language').click()
    page.locator('#dlg-language').wait_for(state='visible')


def pick(page, value):
    page.locator(f'#dlg-language input[value="{value}"]').check()


def run(evidence):
    if not evidence.is_absolute():
        raise ValueError('--evidence must be absolute')
    evidence.mkdir(parents=True, exist_ok=False)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            fixture = Fixture()
            page = fixture.start(context)

            # 1) 初始中文 + 消息原文
            assert page.evaluate('document.documentElement.lang') == 'zh-CN'
            assert_zh(page)
            assert '原文消息：切换语言不能改变我' in page.locator('[data-message-id="10"] .msg-content').inner_text()

            # 2) 双语入口 + 选择 English 立即生效
            open_language_dialog(page)
            assert text_of(page, '#language-title') == '语言设置'
            pick(page, 'en')
            assert_en(page)
            assert page.evaluate('document.documentElement.lang') == 'en'
            assert page.evaluate("localStorage.getItem('ge_ui_language')") == 'en'
            # 消息原文不变
            assert '原文消息：切换语言不能改变我' in page.locator('[data-message-id="10"] .msg-content').inner_text()
            # 动态任务面板已重渲染
            assert text_of(page, '.task-head .th-title') == 'Tasks'
            assert text_of(page, '#dlg-language #language-title') == 'Language'
            # 切换语言会关闭打开中的弹窗（一次性渲染组件按当前语言重建）
            assert not page.locator('#dlg-language').evaluate('d => d.open')
            page.screenshot(path=str(evidence / 'chat-en-light-1280.png'))
            results.append({'case': 'switch_to_en_immediate', 'result': 'passed'})

            # 3) 重开记住选择
            page.reload()
            page.wait_for_function('S.messagesReady')
            page.locator('[data-message-id="10"]').wait_for(timeout=3000)
            assert page.evaluate('document.documentElement.lang') == 'en'
            assert_en(page)
            results.append({'case': 'persisted_after_reload', 'result': 'passed'})

            # 4) 切回中文立即恢复
            open_language_dialog(page)
            pick(page, 'zh-CN')
            assert_zh(page)
            assert page.evaluate("localStorage.getItem('ge_ui_language')") == 'zh-CN'
            results.append({'case': 'switch_back_to_zh', 'result': 'passed'})

            # 5) 设置页对照（设置菜单项 + 我的称呼弹窗）
            page.locator('#btn-settings').click()
            assert text_of(page, '#btn-background span') == '背景'
            page.locator('#btn-settings').click()
            page.evaluate("document.querySelector('#btn-user-name').click()")
            page.locator('#dlg-user-name').wait_for(state='visible')
            assert text_of(page, '#user-name-title') == '我的称呼'
            page.locator('#user-name-cancel').click()
            page.evaluate('setUILanguage("en")')
            assert text_of(page, '#btn-user-name span') == 'My name'
            page.locator('#btn-settings').click()
            page.locator('#btn-user-name').click()
            page.locator('#dlg-user-name').wait_for(state='visible')
            assert text_of(page, '#user-name-title') == 'My name'
            page.locator('#user-name-cancel').click()
            page.evaluate('setUILanguage("zh-CN")')
            results.append({'case': 'settings_page_spot_check', 'result': 'passed'})

            # 6) 管理页对照：任务面板 + 成员管理弹窗
            page.evaluate('setUILanguage("en")')
            assert text_of(page, '.task-head .th-claimed') == 'Claimed'
            assert text_of(page, '.task-head .th-next') == 'Next'
            assert '示例需求' in page.locator('#task-list').inner_text()
            page.evaluate("$('#dlg-members').showModal(); renderMemberAdmin();")
            page.locator('#dlg-members').wait_for(state='visible')
            assert text_of(page, '#dlg-members h3') == 'Group members (bot roster)'
            page.locator('#dlg-members-close').click()
            results.append({'case': 'admin_page_spot_check', 'result': 'passed'})

            # 7) 无消息空态英文 + 深浅主题截图
            page.evaluate("() => { S.msgs = []; renderMessages(); }")
            page.wait_for_function("!!document.querySelector('#msg-empty')")
            assert text_of(page, '#msg-empty') == 'No messages yet'
            page.screenshot(path=str(evidence / 'empty-en-light-1280.png'))
            page.evaluate("applyTheme('light')")
            page.screenshot(path=str(evidence / 'empty-en-light-theme-1280.png'))
            results.append({'case': 'empty_state_en', 'result': 'passed'})
            # 8) 英文模式逐个弹窗扫描：界面文案无 CJK（服务器数据豁免）
            cjk = re.compile(r'[\u4e00-\u9fff]')
            exempt = {'隔离项目', '隔离话题', '示例需求', '示例任务', '原文消息：切换语言不能改变我',
                      '语言 / Language', '简体中文', 'English'}
            dialogs = [
                ('#dlg-background', "document.querySelector('#dlg-background').showModal()"),
                ('#engine-logs', "document.querySelector('#engine-logs').showModal()"),
                ('#relay-nodes-dialog', "document.querySelector('#relay-nodes-dialog').showModal()"),
                ('#dlg-project-entry', "document.querySelector('#dlg-project-entry').showModal()"),
                ('#dlg-member-add', "document.querySelector('#dlg-member-add').showModal()"),
                ('#app-about', "document.querySelector('#app-about').showModal()"),
            ]
            leaks = []
            for selector, open_js in dialogs:
                page.evaluate(open_js)
                page.wait_for_function(f"document.querySelector('{selector}').open")
                text = page.locator(selector).inner_text()
                for line in text.split('\n'):
                    line = line.strip()
                    if cjk.search(line) and not any(e in line for e in exempt):
                        leaks.append({'dialog': selector, 'text': line[:80]})
                page.evaluate(f"document.querySelector('{selector}').close()")
            # HUD 面板展开后外壳文案
            page.evaluate("document.querySelector('#hud-toggle').click()")
            for line in page.locator('#project-hud').inner_text().split('\n'):
                line = line.strip()
                if cjk.search(line) and not any(e in line for e in exempt):
                    leaks.append({'dialog': '#project-hud', 'text': line[:80]})
            assert not leaks, f'English UI still contains CJK copy: {leaks}'
            results.append({'case': 'dialogs_and_hud_english_no_cjk', 'result': 'passed'})

            # 9) 懒创建路径：项目详情成员 tab、移出成员弹窗、composer 图片按钮
            page.evaluate("document.querySelector('#hud-toggle').click()")  # 收起 HUD，避免遮挡弹窗
            page.evaluate("openProjectDetail('p')")
            page.wait_for_function("$('#dlg-project-detail').open")
            page.evaluate("document.querySelector('#pj-tab-members').click()")
            page.wait_for_function("document.querySelectorAll('#pj-member-list .project-member-card').length > 0",
                                   timeout=5000)
            detail_leaks = []
            for line in page.locator('#dlg-project-detail').inner_text().split('\n'):
                line = line.strip()
                if cjk.search(line) and not any(e in line for e in exempt) and 'Bot' not in line:
                    detail_leaks.append(line[:80])
            assert not detail_leaks, f'project detail members tab CJK: {detail_leaks}'
            # 成员卡启用徽标/工作区/主目录断言英文
            card = page.locator('.project-member-card').first
            assert text_of(page, '.project-member-card .project-member-badge') == 'Enabled'
            assert text_of(page, '.project-member-card .workspace') == 'Workspace'
            assert 'project root' in card.inner_text()
            # 移出成员弹窗
            page.evaluate("document.querySelector('.project-member-card .member-more').click()")
            page.wait_for_timeout(300)
            remove_btn = page.locator('.project-member-menu .member-remove')
            assert remove_btn.count() and remove_btn.first.is_visible()
            remove_btn.first.click()
            page.wait_for_function("document.querySelector('#dlg-member-remove') && "
                                   "document.querySelector('#dlg-member-remove').open", timeout=3000)
            remove_leaks = []
            for line in page.locator('#dlg-member-remove').inner_text().split('\n'):
                line = line.strip()
                if cjk.search(line) and not any(e in line for e in exempt) and 'Bot' not in line:
                    remove_leaks.append(line[:80])
            assert not remove_leaks, f'member remove dialog CJK: {remove_leaks}'
            assert text_of(page, '#member-remove-title') == 'Remove from project'
            page.evaluate("document.querySelector('#dlg-member-remove').close()")
            page.evaluate("document.querySelector('#dlg-project-detail').close()")
            # composer 图片按钮 title/aria
            add = page.locator('.image-add').first
            assert add.get_attribute('title') == 'Add image'
            assert add.get_attribute('aria-label') == 'Add image'
            results.append({'case': 'lazy_rendered_paths_english', 'result': 'passed'})

            # 10) 三轮返修组件：消息导航 / SOUL 模板弹窗 / 目录选择 / 授权面板 / 命令弹层 / 策略弹窗
            page.evaluate("""() => {
              const row = document.querySelector('[aria-label="Message navigation"]');
              const btn = document.getElementById('message-bottom');
              if (btn) btn.hidden = false;
              return row ? row.innerText : '';
            }""")
            assert 'Back to bottom' in page.locator('#message-navigation').inner_text()
            assert page.evaluate("document.querySelector('#message-navigation').getAttribute('aria-label')") == 'Message navigation'
            # SOUL 模板弹窗（懒创建内容按打开时语言刷新）
            page.evaluate("""() => {
              const host = document.createElement('dialog');
              host.id = 'soul-host';
              const container = document.createElement('div');
              host.append(container);
              document.body.append(host);
              window.mountProjectSoulTemplates(container, {project: 'p', isActive: () => true,
                maxLength: 100000, getValue: () => '', setValue: () => {},
                status: {textContent: ''}});
              host.showModal();
              container.querySelector('.profile-soul-template-open').click();
            }""")
            page.wait_for_function("document.getElementById('dlg-profile-soul-template')?.open")
            soul_text = page.locator('#dlg-profile-soul-template').inner_text()
            assert 'SOUL templates for this project' in soul_text
            assert 'Apply template' in soul_text
            assert 'Reload' in soul_text
            page.evaluate("document.getElementById('dlg-profile-soul-template').close(); document.getElementById('soul-host').close()")
            # 目录选择弹窗（懒创建，英文构建）
            page.evaluate("""() => {
              const input = document.createElement('input');
              const button = document.createElement('button');
              document.body.append(input, button);
              selectProjectDirectory(input, button);
            }""")
            page.wait_for_function("document.querySelector('#dir-picker-title')")
            dir_text = page.locator('#dir-picker-title').inner_text()
            assert dir_text == 'Select directory (server directory)', dir_text
            assert 'Select this directory' in page.locator('dialog[dir-picker-layer]').inner_text()
            page.evaluate("document.querySelector('dialog[dir-picker-layer]').close()")
            # 授权面板 / 命令弹层 aria（data-i18n-aria-label 已由 applyStatic 更新）
            assert page.evaluate("document.querySelector('#hermes-approvals').getAttribute('aria-label')") == 'Hermes operation approvals'
            assert page.evaluate("document.querySelector('#command-pop').getAttribute('aria-label')") == 'Supported commands'
            # 策略弹窗静态文案
            page.evaluate("document.querySelector('#dlg-edit-policy').showModal()")
            assert page.locator('#dlg-edit-policy h3').inner_text() == 'Smart approval policy'
            page.evaluate("document.querySelector('#dlg-edit-policy').close()")
            results.append({'case': 'round3_components_english', 'result': 'passed'})

            # 11) 属性级扫描：aria-label / title / placeholder（按钮提示与读屏标签，含隐藏元素）。
            # 服务器数据、协议标记与双语语言入口豁免。
            attr_exempt = {'隔离项目', '隔离话题', '示例需求', '示例任务',
                           '原文消息：切换语言不能改变我', '语言 / Language', '简体中文',
                           'English', 'Bot'}
            attr_leaks = page.evaluate("""() => {
              const out = [];
              for (const el of document.querySelectorAll('[aria-label],[title],[placeholder]')) {
                for (const a of ['aria-label', 'title', 'placeholder']) {
                  const v = el.getAttribute(a);
                  if (v && /[\\u4e00-\\u9fff]/.test(v)) out.push(`${el.tagName}#${el.id || ''} ${a}="${v}"`);
                }
              }
              return out;
            }""")
            attr_leaks = [x for x in attr_leaks
                          if not any(e in x for e in attr_exempt) and '[收口]' not in x]
            assert not attr_leaks, f'English UI attributes still contain CJK: {attr_leaks}'
            results.append({'case': 'attrs_english_no_cjk', 'result': 'passed'})
        finally:
            browser.close()

        # 窄屏 390px 抽查（语言弹窗 + 切换不溢出）
        browser = pw.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 390, 'height': 800})
            fixture = Fixture()
            page = fixture.start(context)
            open_language_dialog(page)
            pick(page, 'en')
            page.wait_for_function("document.documentElement.lang === 'en'")
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert text_of(page, '#btn-send') == 'Send'
            page.screenshot(path=str(evidence / 'chat-en-390.png'))
            results.append({'case': 'narrow_390_no_overflow', 'result': 'passed'})
        finally:
            browser.close()

    (evidence / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('PASS: bilingual switch, persistence, message originality, page spot checks, 390px')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args().evidence)
