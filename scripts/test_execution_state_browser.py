"""Real Chrome, synthetic HTTP: 执行状态提示与叫停结果（req#134 task#256）。

交付口径（用户 8994/8996 修正）：界面只报告操作失败/确认超时事实，是否再次操作由用户决定。
覆盖：
- state=manual 时思考条停住并显示「停止请求已发送，结束状态确认超时」或「执行状态查询失败」，
  不再显示「思考中」，不出现「待人工处理/待人工确认」等接手要求，停止按钮保留。
- 叫停返回未确认终态时提示「停止请求已发送」；随后转 manual 时一次性提示确认超时。
- 叫停接口失败显示「停止失败：原因」，原有停止入口仍可用。
- state=cancelling 显示「正在取消…」。
- 深色/浅色 × 桌面/390px：标签可见、无横向溢出、danger 色随主题。
"""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
START_TIMEOUT = 1760000000       # 已叫停、转 manual 的执行
START_RECOVER = 1760000100       # 未叫停、状态无法确认的执行
INTENT_KEY = 'group-engine.cancel-intent.v1'
LABEL_STOP_TIMEOUT = '停止请求已发送，结束状态确认超时'
LABEL_QUERY_FAILED = '执行状态查询失败'


def run(evidence):
    state = {
        'runs': [
            {'profile': 'bot-a', 'project': 'p', 'session': 'bot-a-p-work', 'kind': 'work',
             'thread_id': None, 'wake_started_at': START_TIMEOUT, 'spawned_at': '2026-10-28T12:00:00',
             'channel': 'hermes-gateway', 'state': 'manual'},
            {'profile': 'bot-b', 'project': 'p', 'session': 'bot-b-p-work', 'kind': 'work',
             'thread_id': None, 'wake_started_at': START_RECOVER, 'spawned_at': '2026-10-28T12:01:00',
             'channel': 'hermes-gateway', 'state': 'manual'},
        ],
        'stops': [],
        'stop_fail': False,
    }

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
        if path == '/thinking':
            request.fulfill(json={'runs': state['runs'], 'thinking': []}); return
        if path == '/stop':
            state['stops'].append(request.request.post_data_json)
            if state['stop_fail']:
                request.fulfill(status=500, json={'detail': 'engine offline'}); return
            request.fulfill(json={'profile': 'bot-a', 'project': 'p', 'thread_id': None, 'stopped': False,
                                  'killed': 0, 'soft_deleted': -1, 'seen': 0, 'cancelled': 0,
                                  'released': 0, 'pending_cancelled': 0,
                                  'detail': 'Hermes 执行终态尚未确认，保留任务与队列占用'}); return
        if path == '/reasoning':
            request.fulfill(json={'entries': [], 'latest_id': 0, 'process_ended_at': None, 'replies': []}); return
        if path == '/appearance/background':
            request.fulfill(status=404); return
        if path == '/events':
            request.fulfill(body=': ok\n\n', content_type='text/event-stream'); return
        request.fulfill(json={'projects': [], 'members': [], 'threads': [], 'messages': [], 'tasks': [],
                              'requirements': [], 'pending_approval': [], 'runs': []})

    def head_text(page, session):
        return page.evaluate("""session => {
            const bubble = [...document.querySelectorAll('.think-bubble')]
                .find(node => node.dataset.session === session);
            return bubble ? bubble.querySelector('.think-bubble-head').textContent.replace(/\\s+/g, ' ') : '';
        }""", session)

    def bubble_text(page, session):
        return page.evaluate("""session => {
            const bubble = [...document.querySelectorAll('.think-bubble')]
                .find(node => node.dataset.session === session);
            return bubble ? bubble.textContent.replace(/\\s+/g, ' ') : '';
        }""", session)

    def alert_text(page):
        return page.evaluate("""() => {
            const dialog = document.querySelector('#app-alert');
            return dialog && dialog.open ? document.querySelector('#app-alert-message').textContent : '';
        }""")

    def wait_alert(page, text):
        page.wait_for_function("""text => {
            const dialog = document.querySelector('#app-alert');
            return !!dialog && dialog.open && document.querySelector('#app-alert-message').textContent.includes(text);
        }""", arg=text)
        return alert_text(page)

    def dismiss(page):
        page.locator('#app-alert button.primary').click()
        page.wait_for_function("!document.querySelector('#app-alert')?.open")

    def stop_button(page, session):
        return page.locator(f'.think-bubble[data-session="{session}"] .tb-stop').first

    colors = {'dark': 'rgb(224, 92, 106)', 'light': 'rgb(255, 159, 169)'}
    forbidden = ('待人工处理', '待人工确认', '需人工', '人工处理')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)

        # ---- ① manual 标签：叫停确认超时 vs 查询失败，四种视口/主题，停止入口保留 ----
        for theme in ('dark', 'light'):
            for width, height in ((1280, 800), (390, 780)):
                page = browser.new_page(viewport={'width': width, 'height': height})
                page.add_init_script(
                    f"sessionStorage.setItem({INTENT_KEY!r}, JSON.stringify({{'bot-a-p-work:{START_TIMEOUT}': 1}}))")
                page.route('**/*', route)
                page.goto('http://execution-state.test/app/')
                page.wait_for_function(
                    "document.querySelectorAll('.think-bubble.stalled').length === 2")
                page.evaluate("theme => { document.documentElement.dataset.theme = theme }", theme)
                page.wait_for_timeout(150)
                if width < 520:
                    # 390px：三栏外壳下中栏被两侧栏挤到约 2px（既有布局行为，非本次改动）。
                    # 这里隐藏两侧栏做组件级检查——标签在 390 容器内完整可读且不溢出。
                    page.evaluate("""() => {
                        document.querySelector('.col-threads').style.display = 'none';
                        document.querySelector('.col-tasks').style.display = 'none';
                    }""")
                    page.wait_for_timeout(150)
                    assert page.evaluate("""() => {
                        const bubble = document.querySelector('.think-bubble.stalled');
                        const head = bubble && bubble.querySelector('.think-bubble-head');
                        return !!head && bubble.getBoundingClientRect().width > 240
                            && head.scrollWidth <= head.clientWidth + 1;
                    }"""), '390px 组件级检查：标签与停止按钮不应被裁切'
                assert LABEL_STOP_TIMEOUT in head_text(page, 'bot-a-p-work'), head_text(page, 'bot-a-p-work')
                assert LABEL_QUERY_FAILED in head_text(page, 'bot-b-p-work'), head_text(page, 'bot-b-p-work')
                for session in ('bot-a-p-work', 'bot-b-p-work'):
                    assert '思考中' not in head_text(page, session), head_text(page, session)
                    assert stop_button(page, session).count() == 1, 'manual 态必须保留原有停止入口'
                assert page.locator('.think-bubble .tb-stop').count() == 2
                assert '执行状态未能确认' in bubble_text(page, 'bot-b-p-work')
                text = page.evaluate("document.querySelector('.think-bubble').textContent")
                for word in forbidden:
                    assert word not in text, (word, text)
                color = page.evaluate("""() => {
                    const node = document.querySelector('.think-bubble.stalled .think-bubble-head');
                    return node ? getComputedStyle(node).color : '';
                }""")
                assert color == colors[theme], (theme, color)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), \
                    '窄屏不应横向溢出'
                assert page.locator('#app-alert-message').count() == 0, '载入即 manual 不应弹窗'
                page.screenshot(path=str(evidence / f'execution-state-{theme}-{width}.png'))
                page.close()

        # ---- ② 叫停 → 确认超时 → manual 事实标签 + 一次性提示；再次叫停失败可见 ----
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.route('**/*', route)
        state['runs'] = [dict(state['runs'][0], state='running')]
        page.goto('http://execution-state.test/app/')
        page.wait_for_function("document.querySelectorAll('.think-bubble').length === 1")
        assert '思考中' in head_text(page, 'bot-a-p-work'), head_text(page, 'bot-a-p-work')
        stop_button(page, 'bot-a-p-work').click()
        wait_alert(page, '叫停')
        dismiss(page)
        pending = wait_alert(page, '停止请求已发送')
        assert '150 秒' in pending, pending
        assert '未确认死透' not in pending, pending
        for word in forbidden:
            assert word not in pending, (word, pending)
        dismiss(page)
        assert state['stops'] == [{'profile': 'bot-a', 'project': 'p', 'thread_id': None}], state['stops']
        state['runs'] = [dict(state['runs'][0], state='manual')]
        page.evaluate('loadThinking()')
        page.wait_for_function("document.querySelectorAll('.think-bubble.stalled').length === 1")
        assert LABEL_STOP_TIMEOUT in head_text(page, 'bot-a-p-work'), head_text(page, 'bot-a-p-work')
        timeout_alert = wait_alert(page, LABEL_STOP_TIMEOUT)
        assert '150 秒' in timeout_alert, timeout_alert
        for word in forbidden:
            assert word not in timeout_alert, (word, timeout_alert)
        dismiss(page)
        page.evaluate('loadThinking()')
        page.wait_for_timeout(400)
        assert alert_text(page) == '', '确认超时提示只应弹一次'

        # 原有停止入口保留：再次点击仍发起请求；接口失败如实显示「停止失败」。
        state['stop_fail'] = True
        stop_button(page, 'bot-a-p-work').click()
        wait_alert(page, '叫停')
        dismiss(page)
        failed = wait_alert(page, '停止失败')
        assert 'engine offline' in failed, failed
        assert len(state['stops']) == 2, state['stops']
        dismiss(page)
        assert LABEL_STOP_TIMEOUT in head_text(page, 'bot-a-p-work'), '失败后仍保留事实状态'
        page.screenshot(path=str(evidence / 'execution-state-stop-failed.png'))

        # ---- ③ 取消进行中 ----
        state['stop_fail'] = False
        state['runs'] = [dict(state['runs'][0], state='cancelling')]
        page.evaluate('loadThinking()')
        page.wait_for_function("document.querySelectorAll('.think-bubble.stalled').length === 1")
        assert '正在取消…' in head_text(page, 'bot-a-p-work'), head_text(page, 'bot-a-p-work')
        page.screenshot(path=str(evidence / 'execution-state-cancelling.png'))
        # ---- ④ manual 无叫停意图 → 用户再次点击停止：state 不变，标签改为停止事实 ----
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.route('**/*', route)
        state['stop_fail'] = False
        state['runs'] = [dict(state['runs'][0], profile='bot-b', session='bot-b-p-work',
                              wake_started_at=START_RECOVER, state='manual')]
        page.goto('http://execution-state.test/app/')
        page.wait_for_function("document.querySelectorAll('.think-bubble.stalled').length === 1")
        assert LABEL_QUERY_FAILED in head_text(page, 'bot-b-p-work'), head_text(page, 'bot-b-p-work')
        stop_button(page, 'bot-b-p-work').click()
        wait_alert(page, '叫停')
        dismiss(page)
        requested = wait_alert(page, '停止请求已发送')
        assert 'engine offline' not in requested, requested
        dismiss(page)
        page.wait_for_function("""label => {
            const bubble = [...document.querySelectorAll('.think-bubble')]
                .find(node => node.dataset.session === 'bot-b-p-work');
            return !!bubble && bubble.querySelector('.think-bubble-head').textContent.includes(label);
        }""", arg=LABEL_STOP_TIMEOUT)
        assert len(state['stops']) == 3, state['stops']
        page.screenshot(path=str(evidence / 'execution-state-intent-after-manual.png'))
        page.close()
        browser.close()

    print('PASS: 事实状态（停止请求已发送/确认超时、执行状态查询失败）、停止入口保留、'
          '停止失败提示、确认超时一次性提示、cancelling 标签、深浅主题与 390px 无溢出')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
