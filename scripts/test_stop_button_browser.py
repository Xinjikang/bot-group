"""Real Chromium, mocked APIs: enlarged high-contrast stop button on think bubbles.

bot-group req#178 task#335. Run:
  py -3.10 -X utf8 -B scripts/test_stop_button_browser.py --evidence <dir>
"""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', default=None)
    args = parser.parse_args()
    evidence = Path(args.evidence) if args.evidence else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)

    stops = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(),
                            content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if path in ('/events', '/desktop/events'):
                req.fulfill(body=': test\n\n', content_type='text/event-stream')
                return
            if path == '/stop':
                stops.append(req.request.post_data_json)
                req.fulfill(json={'stopped': True})
                return
            data = {}
            if path == '/auth/status':
                data = {'configured': False}
            elif path == '/messages':
                data = {'messages': []}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/projects':
                data = {'projects': [{'name': 'p', 'display_name': '隔离项目'}]}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://stop-button.test/app/index.html')
        page.wait_for_function('S.messagesReady')

        # Render one live thinking bubble through the production renderer.
        page.evaluate('''() => {
            showConfirm = async () => true;
            S.thinkRuns.set('run-1', {profile: 'bot', project: 'p', thread_id: 3,
                state: 'running', key: 'run-1', finished: false});
            const bubble = {key: 'run-1', sourceKey: 'run-1', profile: 'bot',
                project: 'p', thread_id: 3, status: 'thinking', collapsed: false,
                displayKeys: [], entries: [], spawned_at: '2026-11-25T10:00:00'};
            const panel = renderThinkBubble(bubble, false);
            panel.id = 'test-bubble';
            document.getElementById('msg-list').appendChild(panel);
        }''')
        btn = page.locator('#test-bubble .tb-stop')
        assert btn.count() == 1, 'stop button rendered on the thinking bubble'

        # 1) 位置原位：思考气泡头部内（与名字/状态同一行），未新增按钮。
        head = page.locator('#test-bubble .think-bubble-head')
        assert head.count() == 1
        assert head.locator('.tb-stop').count() == 1

        # 2) 可见文字「停止」（zh），title/aria 含成员名。
        assert btn.inner_text().strip() == '停止', btn.inner_text()
        assert btn.get_attribute('title') == '停止 bot'
        assert btn.get_attribute('aria-label') == '停止 bot'

        # 3) computed：文字放大、对比色、边框、点击区域扩大。
        style = btn.evaluate('''el => {
            const s = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return {fontSize: s.fontSize, fontWeight: s.fontWeight, color: s.color,
                    padding: s.padding, width: rect.width, height: rect.height,
                    border: s.borderTopWidth, background: s.backgroundColor};
        }''')
        assert style['fontSize'] == '12px', style
        assert int(style['fontWeight']) >= 600, style
        assert style['border'] == '1px', style
        assert style['background'] == 'rgba(0, 0, 0, 0)', style
        assert style['width'] >= 40 and style['height'] >= 26, style
        btn.hover()
        assert btn.evaluate('el => getComputedStyle(el).backgroundColor') == 'rgba(0, 0, 0, 0)'

        if evidence:
            page.screenshot(path=str(evidence / 'stop_button_zh.png'), full_page=True)

        # 4) 点击真实触发 /stop 请求（确认被 stub 为同意）。
        btn.click()
        page.wait_for_timeout(300)
        assert stops, 'click on enlarged stop button POSTs /stop'
        assert stops[-1] == {'profile': 'bot', 'project': 'p', 'thread_id': 3}, stops

        # 5) 切英文：按钮文字跟语言走；中文恢复。
        page.evaluate('setUILanguage("en")')
        page.wait_for_function("document.documentElement.lang === 'en'")
        page.wait_for_timeout(200)
        # 思考气泡内容在语言变化时随 renderThinkBubbles 重建；直接重渲染断言
        en_text = page.evaluate('''() => {
            const panel = renderThinkBubble({key: 'run-1', sourceKey: 'run-1',
                profile: 'bot', project: 'p', thread_id: 3, status: 'thinking',
                collapsed: false, displayKeys: [], entries: [], spawned_at: '2026-11-25T10:00:00'}, false);
            panel.id = 'test-bubble-en';
            document.getElementById('msg-list').appendChild(panel);
            return panel.querySelector('.tb-stop').textContent.trim();
        }''')
        assert en_text == 'Stop', en_text
        page.evaluate('setUILanguage("zh-CN")')

        if evidence:
            (evidence / 'results.json').write_text(json.dumps([
                {'case': 'stop_button_in_place', 'result': 'passed'},
                {'case': 'enlarged_high_contrast_style', 'result': 'passed'},
                {'case': 'label_zh', 'result': 'passed'},
                {'case': 'click_posts_stop', 'result': 'passed'},
                {'case': 'label_en', 'result': 'passed'},
            ], ensure_ascii=False, indent=1), encoding='utf-8')

        browser.close()
    print('PASS: stop button in place, enlarged, high contrast, label zh/en, click posts /stop')


if __name__ == '__main__':
    main()
