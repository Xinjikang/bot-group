"""Topic delegation and work process waits with synthetic HTTP and isolated Chrome."""
import argparse
import itertools
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
LABEL = '子代理正在执行后台任务'


def run(evidence):
    execution = dict(profile='bot', project='p', session='bot-p-chat-10', kind='chat',
                     thread_id=10, wake_started_at=100, spawned_at='2026-11-05T12:00:00',
                     state='waiting_delegation', channel='hermes-gateway')
    state = {'ended': False}

    def route(request):
        path = urlparse(request.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            if file.is_file():
                request.fulfill(body=file.read_bytes(),
                                content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                request.fulfill(status=404)
        elif path == '/auth/status':
            request.fulfill(json={'configured': False})
        elif path == '/thinking':
            request.fulfill(json={'runs': [] if state['ended'] else [execution], 'thinking': []})
        elif path == '/threads':
            request.fulfill(json={'threads': [{'id': 10, 'project': 'p', 'title': '隔离话题', 'status': 'open', 'members': []}]})
        elif path == '/reasoning':
            request.fulfill(json={'entries': [], 'latest_id': 0, 'replies': [],
                                 'execution_state': execution['state'],
                                 'background_kind': execution.get('background_kind', 'delegation'),
                                 'process_ended_at': 110 if state['ended'] else None})
        elif path == '/appearance/background':
            request.fulfill(status=404)
        elif path == '/events':
            request.fulfill(body=': ok\n\n', content_type='text/event-stream')
        else:
            request.fulfill(json={'projects': [], 'members': [], 'threads': [], 'messages': [],
                                 'tasks': [], 'requirements': [], 'pending_approval': [], 'runs': []})

    def show_topic(page, theme, width):
        page.evaluate("""({theme, narrow, process}) => {
            document.documentElement.dataset.theme = theme;
            S.currentProject = 'p'; S.currentThread = process ? null : 10;
            S.threads = [{id:10, project:'p', title:'隔离话题', status:'open', members:[]}];
            if (narrow) {
                document.querySelector('.col-threads').style.display = 'none';
                document.querySelector('.col-tasks').style.display = 'none';
            }
            renderMessages();
        }""", {'theme': theme, 'narrow': width == 390, 'process': execution['kind'] == 'work'})

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        for kind, theme, width in itertools.product(('delegation', 'process'), ('dark', 'light'), (1280, 390)):
            label = '后台进程正在执行' if kind == 'process' else LABEL
            execution.update(kind='work' if kind == 'process' else 'chat',
                             thread_id=None if kind == 'process' else 10,
                             session='bot-p-work' if kind == 'process' else 'bot-p-chat-10',
                             background_kind=kind)
            state['ended'] = False
            execution['state'] = 'waiting_delegation'
            context = browser.new_context(viewport={'width': width, 'height': 800})
            page = context.new_page()
            page.route('**/*', route)
            page.goto('http://delegation-bubble.test/app/')
            page.wait_for_function('S.thinkRuns.size === 1')
            show_topic(page, theme, width)
            head = page.locator('.think-bubble-head')
            head.filter(has_text=label).wait_for()
            assert page.locator('.tb-stop').count() == 1
            assert page.locator('.tb-dots-still').count() == 1
            assert '思考中' not in head.inner_text()
            assert page.locator('.think-bubble-body').is_visible()
            assert page.evaluate("""() => {
                const head = document.querySelector('.think-bubble-head');
                return head.clientWidth > 240 && head.scrollWidth <= head.clientWidth + 1;
            }"""), 'background status must fit the message column'
            page.screenshot(path=str(evidence / f'waiting-{kind}-{theme}-{width}.png'))
            page.reload()
            page.wait_for_function('S.thinkRuns.size === 1')
            show_topic(page, theme, width)
            head.filter(has_text=label).wait_for()
            execution['state'] = 'running'
            page.evaluate('loadThinking()')
            head.filter(has_text='思考中').wait_for()
            assert page.locator('.tb-dots-still').count() == 0
            assert page.evaluate('S.thinkRuns.size') == 1
            execution['state'] = 'waiting_delegation'
            page.evaluate('loadThinking()')
            head.filter(has_text=label).wait_for()
            execution['state'] = 'terminal'
            state['ended'] = True
            page.evaluate('loadThinking()')
            page.wait_for_function('[...S.thinkRuns.values()][0].finished')
            assert page.locator('.tb-stop').count() == 0
            assert label not in head.inner_text()
            results.append({'kind': kind, 'theme': theme, 'width': width, 'result': 'passed'})
            context.close()
        browser.close()
    (evidence / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('PASS: topic delegation/work process waiting, refresh, running, completion; dark/light × 1280/390 (8 combinations)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not args.evidence.is_absolute():
        parser.error('--evidence must be absolute')
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
