"""Real Chrome approval UI against isolated HTTP fixtures."""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    item = dict(execution_id='execution', prompt_id='prompt', generation=3, profile='default',
                project='alpha', thread_id=7, command='<script>unsafe()</script> echo 合成请求',
                description='需要确认本次操作', choices=['once', 'deny', 'session', 'always'], error='')
    state = dict(items=[item], posts=[], fail=False, held=None)
    errors = []
    def route(req):
        target = urlparse(req.request.url); path = target.path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain'); return
        if path == '/hermes-approvals':
            params = parse_qs(target.query)
            items = [x for x in state['items'] if params.get('project', [x['project']])[0] == x['project']
                     and params.get('thread_id', [str(x['thread_id'])])[0] == str(x['thread_id'])]
            req.fulfill(json={'approvals': items}); return
        if path.startswith('/hermes-approvals/'):
            state['posts'].append(req.request.post_data_json)
            if state['fail']:
                req.fulfill(status=503, json={'detail': '授权结果暂未确认'}); return
            state['held'] = req; return
        if path == '/appearance/background':
            req.fulfill(status=204); return
        data = {'configured': False} if path == '/auth/status' else {}
        if path in ('/projects', '/members', '/messages', '/threads'):
            data = {path[1:]: []}
        if path == '/tasks': data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        req.fulfill(json=data)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route); page.goto('http://approvals.test/app/')
        try:
            page.locator('.approval-card').wait_for(timeout=10000)
        except Exception:
            page.screenshot(path=str(evidence / 'initial-failure.png'))
            print({'errors': errors, 'state': page.evaluate('({user:S.isUserIdentity,project:S.currentProject,thread:S.currentThread,login:document.querySelector("#login").hidden,panel:document.querySelector("#hermes-approvals").outerHTML})')})
            raise
        assert page.locator('.approval-command').inner_text() == item['command']
        page.reload(); page.locator('.approval-card').wait_for()
        for width, theme in ((1440, 'dark'), (900, 'dark'), (601, 'dark'), (600, 'light'), (390, 'light'), (390, 'dark')):
            page.set_viewport_size({'width': width, 'height': 900}); page.evaluate('applyTheme', theme)
            page.wait_for_timeout(100)
            for rect in page.locator('.approval-actions button').evaluate_all('(els)=>els.map(e=>({left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right}))'):
                assert rect['left'] >= 0 and rect['right'] <= width
            page.screenshot(path=str(evidence / f'approval-{width}-{theme}.png'))
        page.evaluate("S.currentThread=8; document.dispatchEvent(new Event('approval-context-changed'))")
        page.locator('.approval-card').wait_for(state='hidden')
        page.evaluate("S.currentThread=7; document.dispatchEvent(new Event('approval-context-changed'))")
        try:
            page.locator('.approval-card').wait_for(timeout=10000)
        except Exception:
            (evidence / 'scope-failure.json').write_text(json.dumps(page.evaluate('({user:S.isUserIdentity,project:S.currentProject,thread:S.currentThread,panel:document.querySelector("#hermes-approvals").outerHTML,rect:document.querySelector("#hermes-approvals").getBoundingClientRect().toJSON()})'), ensure_ascii=False), encoding='utf-8')
            raise
        page.get_by_role('button', name='长期允许', exact=True).click()
        page.get_by_role('button', name='取消', exact=True).click()
        assert not state['posts']
        page.get_by_role('button', name='仅允许这一次', exact=True).click()
        page.wait_for_timeout(100)
        assert all(page.locator('.approval-actions button').nth(i).is_disabled() for i in range(4))
        assert state['posts'] == [{'prompt_id': 'prompt', 'generation': 3, 'choice': 'once'}]
        state['items'] = []; state['held'].fulfill(json={'status': 'resolved'})
        page.locator('.approval-card').wait_for(state='hidden')
        state['items'] = [item]
        page.locator('.approval-card').wait_for()
        state['fail'] = True
        page.get_by_role('button', name='拒绝', exact=True).click()
        page.locator('#app-alert').wait_for()
        assert '结果暂未确认' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='确定', exact=True).click()
        state['items'] = []
        page.locator('.approval-card').wait_for(state='hidden')
        assert len(state['posts']) == 2
        assert not errors, errors
        browser.close()
    (evidence / 'result.json').write_text(json.dumps({'passed': True, 'posts': state['posts']}, ensure_ascii=False), encoding='utf-8')
    print('PASS approval scope, refresh, themes, escaping, confirmation, duplicate guard, external resolution and error feedback')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args(); args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
