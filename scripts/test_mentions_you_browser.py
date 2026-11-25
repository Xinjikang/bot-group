"""Real Chromium, mocked APIs: bold inline mentions and the mentions-you badge.

bot-group req#178 task#334. Run:
  py -3.10 -X utf8 -B scripts/test_mentions_you_browser.py --evidence <dir>
"""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

MESSAGES = [
    {"id": 1, "ts": "2026-11-25T10:00:00", "sender": "architect", "sender_type": "bot",
     "content": "@user 请看一下这个方案", "mentions": '["user"]', "thread_id": 7, "project": "p"},
    {"id": 2, "ts": "2026-11-25T10:01:00", "sender": "architect", "sender_type": "bot",
     "content": "@reviewer 你来复审", "mentions": '["reviewer"]', "thread_id": 7, "project": "p"},
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', default=None)
    args = parser.parse_args()
    evidence = Path(args.evidence) if args.evidence else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)

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
            data = {}
            if path == '/messages':
                data = {'messages': MESSAGES}
            elif path == '/auth/status':
                data = {'configured': False}
            elif path == '/members':
                data = {'members': [
                    {'profile': 'architect', 'display_name': '架构师', 'active': 1},
                    {'profile': 'reviewer', 'display_name': '审查员', 'active': 1},
                ]}
            elif path == '/threads':
                data = {'threads': [{'id': 7, 'title': '示例话题', 'status': 'open'}]}
            elif path == '/projects':
                data = {'projects': [{'name': 'p', 'display_name': '隔离项目'}]}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://mention-you.test/app/index.html')
        page.wait_for_function('S.messagesReady')

        # 1) zh：提到我标记与透明背景的加粗点名
        badge = page.locator('[data-message-id="1"] .tag-mentions-you')
        assert badge.count() == 1, 'mentions-you badge rendered on the mentioned message'
        assert badge.inner_text() == '提到我', badge.inner_text()
        mention = page.locator('[data-message-id="1"] .msg-content .mention')
        assert mention.inner_text() == '@用户', mention.inner_text()
        styles = mention.evaluate("el => { const s = getComputedStyle(el); return {bg: s.backgroundColor, weight: s.fontWeight, color: s.color}; }")
        assert styles['weight'] in ('600', '700'), styles
        assert styles['bg'] == 'rgba(0, 0, 0, 0)', styles

        # 2) 点别人的消息无标记
        assert page.locator('[data-message-id="2"] .tag-mentions-you').count() == 0

        # 3) 切英文：标记文字跟语言走；消息正文保留原文
        page.evaluate('setUILanguage("en")')
        page.wait_for_function("document.documentElement.lang === 'en'")
        page.wait_for_function("document.querySelector('[data-message-id=\"1\"] .tag-mentions-you')?.innerText === 'Mentions you'")
        assert page.locator('[data-message-id="1"] .msg-content').inner_text() == '@用户 请看一下这个方案'

        # 4) 切回中文恢复
        page.evaluate('setUILanguage("zh-CN")')
        page.wait_for_function("document.querySelector('[data-message-id=\"1\"] .tag-mentions-you')?.innerText === '提到我'")

        if evidence:
            page.screenshot(path=str(evidence / 'mentions_you_zh.png'), full_page=True)
            page.evaluate('setUILanguage("en")')
            page.wait_for_function("document.querySelector('[data-message-id=\"1\"] .tag-mentions-you')?.innerText === 'Mentions you'")
            page.screenshot(path=str(evidence / 'mentions_you_en.png'), full_page=True)
            (evidence / 'results.json').write_text(json.dumps([
                {'case': 'bold_transparent_mention_and_badge_zh', 'result': 'passed'},
                {'case': 'no_badge_for_others', 'result': 'passed'},
                {'case': 'badge_follows_language', 'result': 'passed'},
                {'case': 'message_body_verbatim', 'result': 'passed'},
            ], ensure_ascii=False, indent=1), encoding='utf-8')

        browser.close()
    print('PASS: bold mention with transparent background, mentions-you badge zh/en, no badge for others, body verbatim')


if __name__ == '__main__':
    main()
