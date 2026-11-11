"""Verify that a successful send reaches the timeline without waiting for polling."""

import argparse
from collections import Counter
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
TOPICS = [dict(id=number, project='p', title=f'隔离话题 {number}', status='open', members=['bot'])
          for number in (7, 8)]
OLD = dict(id=10, project='p', thread_id=7, sender='bot', sender_type='bot',
           content='旧消息', ts='2026-11-11T12:00:00', mentions='[]', attachments=[])
SENT = dict(OLD, id=11, sender='user', sender_type='user', content='合成发送后及时上屏')
OTHER = dict(OLD, id=12, thread_id=8, content='另一话题')


class Fixture:
    def __init__(self):
        self.hold = set()
        self.held = {}
        self.reads = Counter()
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
            self.reads[topic] += 1
            if topic == 7 and self.pause('snapshot', route):
                return
            rows = {7: [OLD, SENT] if self.posts else [OLD], 8: [OTHER]}.get(topic, [])
            data = {'messages': rows}
        elif path == '/msg':
            self.posts += 1
            if self.pause('send', route):
                return
            data = {'message_id': SENT['id']}
        elif path == '/inbox/seen':
            if self.pause('read_ack', route):
                return
            data = {}
        elif path == '/events':
            route.fulfill(body=': test\n\n', content_type='text/event-stream')
            return
        else:
            data = {
                '/auth/status': {'configured': False},
                '/projects': {'projects': [{'name': 'p', 'display_name': '隔离项目'}]},
                '/threads': {'threads': TOPICS},
                '/members': {'members': [{'profile': 'bot', 'display_name': 'Bot', 'active': 1}]},
                '/tasks': {'tasks': [], 'requirements': [], 'pending_approval': []},
            }.get(path, {})
        route.fulfill(json=data)

    def pause(self, stage, route):
        if stage in self.hold:
            self.hold.remove(stage)
            self.held[stage] = route
            return True
        return False

    def start(self, context):
        context.route('**/*', self.route)
        # The production fallback poll cannot satisfy these assertions.
        context.add_init_script('''const realInterval = window.setInterval;
            window.setInterval = (fn, ms, ...args) => ms === 10000 ? 0 : realInterval(fn, ms, ...args);''')
        page = context.new_page()
        page.goto('http://message-visibility.test/app/')
        page.wait_for_function('S.messagesReady')
        page.evaluate("async () => { S.currentProject='p'; await selectThread(7); }")
        page.locator('[data-message-id="10"]').wait_for(timeout=2000)
        return page

    def send(self, page):
        with page.expect_request(lambda req: urlsplit(req.url).path == '/msg'):
            composer = page.locator('#composer-text')
            composer.fill(SENT['content'])
            composer.press('Enter')


def announce(page, count=1):
    page.evaluate('''count => {
        for (let i = 0; i < count; i++) {
            handleEvent({type:'message.new', data:{message_id:11, thread_id:7, project:'p', sender:'user'}});
        }
    }''', count)


def start_held_refresh(page, fixture, stage, endpoint):
    fixture.hold.add(stage)
    with page.expect_request(lambda req: urlsplit(req.url).path == endpoint):
        page.evaluate('() => { window.olderRefresh = refreshMessages(); }')
    for _ in range(50):
        if stage in fixture.held:
            return
        page.wait_for_timeout(20)
    raise AssertionError(f'The fixture did not hold {stage}')


def check_case(page, fixture, case):
    new_message = page.locator('[data-message-id="11"]')
    if case == 'receipt_without_sse':
        fixture.send(page)
        new_message.wait_for(timeout=2000)
    elif case == 'stale_snapshot':
        start_held_refresh(page, fixture, 'snapshot', '/messages')
        reads = fixture.reads[7]
        fixture.send(page)
        page.wait_for_function('!document.querySelector("#composer-text").value')
        announce(page, 3)
        assert fixture.reads[7] == reads and new_message.count() == 0
        fixture.held.pop('snapshot').fulfill(json={'messages': [OLD]})
        new_message.wait_for(timeout=2000)
        assert fixture.reads[7] == reads + 1
    elif case == 'pending_read_ack':
        start_held_refresh(page, fixture, 'read_ack', '/inbox/seen')
        with page.expect_request(lambda req: urlsplit(req.url).path == '/inbox/seen'
                                 and req.post_data_json['through_id'] == SENT['id'], timeout=2000):
            fixture.send(page)
            new_message.wait_for(timeout=2000)
        assert 'read_ack' in fixture.held
        fixture.held.pop('read_ack').fulfill(json={})
    elif case == 'switch_topic':
        start_held_refresh(page, fixture, 'snapshot', '/messages')
        announce(page)
        fixture.hold.add('send')
        fixture.send(page)
        page.wait_for_function('imageSends.has(imageDestinationKey({project:"p", thread_id:7}))')
        page.evaluate('() => selectThread(8)')
        page.locator('#composer-text').fill('另一话题草稿')
        reads = fixture.reads.copy()
        fixture.held.pop('snapshot').fulfill(json={'messages': [OLD]})
        fixture.held.pop('send').fulfill(json={'message_id': SENT['id']})
        page.evaluate('() => window.olderRefresh')
        page.wait_for_function('!imageSends.has(imageDestinationKey({project:"p", thread_id:7}))')
        assert fixture.reads == reads
        assert page.locator('#composer-text').input_value() == '另一话题草稿'
        assert page.locator('[data-message-id="12"]').count() == 1 and new_message.count() == 0


def run(evidence):
    if not evidence.is_absolute():
        raise ValueError('--evidence must be absolute')
    evidence.mkdir(parents=True, exist_ok=False)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        try:
            for case in ('receipt_without_sse', 'stale_snapshot', 'pending_read_ack', 'switch_topic'):
                with browser.new_context() as context:
                    fixture = Fixture()
                    page = fixture.start(context)
                    check_case(page, fixture, case)
                    assert fixture.posts == 1
                    results.append({'case': case, 'result': 'passed'})
        finally:
            browser.close()
    (evidence / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('PASS: receipt refresh, coalesced stale reads, pending read acknowledgement, topic isolation')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args().evidence)
