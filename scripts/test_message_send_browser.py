"""Send feedback and draft ownership with synthetic HTTP and isolated Chrome."""
import argparse
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    if not evidence.is_absolute():
        raise ValueError('--evidence must be absolute')
    evidence.mkdir(parents=True, exist_ok=False)
    pending, posts, errors, results, traces = [], [], [], [], []

    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            if file.is_file():
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                req.fulfill(status=404)
            return
        data = {}
        if path == '/msg':
            posts.append(req.request.post_data_json)
            pending.append(req)
            return
        if path == '/auth/status':
            data = {'configured': False}
        elif path == '/projects':
            data = {'projects': [{'name': name, 'display_name': name} for name in ('p', 'q')]}
        elif path == '/members':
            data = {'members': [{'profile': 'bot', 'display_name': 'bot', 'active': 1}]}
        elif path == '/messages':
            data = {'messages': [], 'count': 0}
        elif path == '/threads':
            data = {'threads': []}
        elif path == '/tasks':
            data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        elif path == '/appearance/background':
            req.fulfill(status=204)
            return
        req.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        try:
            for theme in ('dark', 'light'):
                for width in (1280, 390):
                    context = browser.new_context(viewport={'width': width, 'height': 800})
                    try:
                        context.route('**/*', route)
                        page = context.new_page()
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        prefix = '[TEST TEMP_SEND_TRACE] finish '
                        page.goto('http://message-send.test/app/')
                        page.wait_for_function('S.messagesReady')
                        page.evaluate("""({theme, narrow}) => {
                          S.currentProject='p'; document.documentElement.dataset.theme=theme;
                          if(narrow) {
                            document.querySelector('.col-threads').style.display='none';
                            document.querySelector('.col-tasks').style.display='none';
                          }
                          updateComposerState();
                        }""", {'theme': theme, 'narrow': width == 390})
                        field, button = page.locator('#composer-text'), page.locator('#btn-send')
                        before = len(posts)
                        field.fill('@bot send probe')
                        field.press('Enter')
                        page.wait_for_function('document.querySelector("#btn-send").disabled')
                        assert button.inner_text() == '发送中…'
                        assert field.input_value() == '@bot send probe'
                        assert page.locator('#composer').get_attribute('aria-busy') == 'true'
                        page.evaluate('document.querySelector("#composer").requestSubmit()')
                        page.wait_for_timeout(50)
                        assert len(posts) == before + 1, 'one in-flight send per destination'
                        page.screenshot(path=str(evidence / f'pending-{theme}-{width}.png'))
                        request = pending.pop(0)
                        query = urlparse(request.request.url).query
                        assert re.fullmatch(r'send_trace=[a-f0-9]{32}', query), query
                        trace_id = query.split('=', 1)[1]
                        with page.expect_console_message(predicate=lambda m: m.text.startswith(prefix)
                                                         and trace_id in m.text) as printed:
                            request.fulfill(json={'message_id': 10})
                        page.wait_for_function('document.querySelector("#composer-text").value === ""')
                        assert button.is_enabled()
                        assert button.inner_text() == '发送'
                        timing = json.loads(printed.value.text[len(prefix):])
                        assert timing['status'] == 200 and timing['outcome'] == 'ok'
                        assert timing['total_ms'] >= timing['receipt_to_button_ms'] >= 0
                        assert 'send probe' not in json.dumps(timing)
                        traces.append(timing)

                        field.fill('@bot retained on failure')
                        field.press('Enter')
                        page.wait_for_function('document.querySelector("#btn-send").disabled')
                        page.wait_for_timeout(30)
                        with page.expect_console_message(predicate=lambda m: m.text.startswith(prefix)) as printed:
                            pending.pop(0).fulfill(status=503, json={'detail': 'Isolated send failure'})
                        timing = json.loads(printed.value.text[len(prefix):])
                        assert timing['status'] == 503 and timing['outcome'] == 'error'
                        traces.append(timing)
                        page.wait_for_function('!document.querySelector("#btn-send").disabled')
                        assert field.input_value() == '@bot retained on failure'
                        page.locator('#app-alert .primary').click()

                        field.press('Enter')
                        page.wait_for_function('document.querySelector("#btn-send").disabled')
                        page.wait_for_timeout(30)
                        page.evaluate('selectProject("q")')
                        assert button.is_enabled()
                        field.fill('@bot next project draft')
                        pending.pop(0).fulfill(json={'message_id': 11})
                        page.wait_for_function('imageSends.size === 0')
                        assert field.input_value() == '@bot next project draft'
                        assert page.locator('#composer').get_attribute('aria-busy') == 'false'
                        page.evaluate('selectProject("p")')
                        assert button.is_enabled()

                        field.fill('@bot pending original')
                        field.press('Enter')
                        page.wait_for_function('document.querySelector("#btn-send").disabled')
                        page.wait_for_timeout(30)
                        field.fill('@bot newer draft')
                        pending.pop(0).fulfill(json={'message_id': 12})
                        page.wait_for_function('imageSends.size === 0')
                        assert field.input_value() == '@bot newer draft'
                        page.evaluate("""() => {
                          S.currentThread=7; S.threads=[{id:7,status:'closed'}]; updateComposerState();
                        }""")
                        assert button.is_disabled()
                        assert field.is_disabled()
                        assert button.inner_text() == '发送'
                        results.append({'theme': theme, 'width': width, 'result': 'passed'})
                    finally:
                        context.close()
        finally:
            browser.close()
    assert not errors, errors
    (evidence / 'results.json').write_text(json.dumps({'cases': results, 'posts': len(posts), 'errors': errors,
                                                      'send_traces': traces}, indent=2), encoding='utf-8')
    print('PASS: pending feedback, duplicate gate, success, failure, project switch, newer draft; dark/light x 1280/390')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args().evidence)
