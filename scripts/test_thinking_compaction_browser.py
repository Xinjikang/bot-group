"""Real Chrome checks reply attachment and recovery of an old compaction cache.

All routes are synthetic; no production service, database or Bot is accessed.
"""
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main():
    state = {'repaired': False}
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request):
            url = urlparse(request.request.url)
            if url.path.startswith('/app/'):
                file = ROOT / 'frontend' / (url.path[5:] or 'index.html')
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            elif url.path == '/reasoning':
                after = int(parse_qs(url.query).get('after_id', ['0'])[0])
                entries = [{'id': 1, 'timestamp': 101, 'reasoning': '压缩前：核对测试报告。'}]
                if state['repaired']:
                    entries += [{'id': 3, 'timestamp': 103, 'reasoning': '压缩后：验收完成，通知负责人。'},
                                {'id': 4, 'timestamp': 105, 'finished': True}]
                else:
                    entries += [{'id': 2, 'timestamp': 102, 'finished': True}]
                request.fulfill(json={'entries': [e for e in entries if e['id'] > after],
                    'latest_id': 4 if state['repaired'] else 2,
                    'replies': [{'message_id': 6871, 'sent_at': 104}] if state['repaired'] else [],
                    'process_ended_at': 106 if state['repaired'] else None})
            else:
                request.fulfill(json={'runs': [], 'thinking': [], 'messages': [], 'threads': []})

        page.route('**/*', route)
        page.goto('https://workspace.test/app/index.html')
        setup = """() => {
            showLogin(false);
            S.currentProject = 'isolated'; S.currentThread = null; S.messagesReady = true;
            S.hasOlderMessages = false;
            S.members = [{profile: 'architect', display_name: '苏瑾', role: '架构师'}];
            S.msgs = [{id: 6871, project: 'isolated', sender: 'architect', sender_type: 'bot',
                thread_id: null, ts: '2026-10-15T19:28:32', content: '测试任务已验收关闭。', mentions: '[]'}];
            renderMessages();
        }"""
        page.evaluate(setup)
        page.evaluate("""startThinkBubble({profile:'architect', session:'architect-isolated-work',
            project:'isolated', thread_id:null, wake_started_at:100, spawned_at:'2026-10-15T19:22:38'})""")
        page.wait_for_function("[...S.thinkRuns.values()][0]?.finished === true")
        message = page.locator('[data-message-id="6871"]')
        assert message.locator('.think-entry').count() == 0
        legacy_cache = page.evaluate("""() => {
            const saved = JSON.parse(localStorage.getItem('group-engine.thinking.v2'));
            for (const run of saved.runs) delete run.replyReconciled;
            return JSON.stringify(saved);
        }""")
        state['repaired'] = True
        page.evaluate("""refreshThinkRunsForMessage({sender:'architect', project:'isolated', thread_id:null,
            message_id:6871, execution:{session:'architect-isolated-work', wake_started_at:100}})""")
        message.locator('.think-entry').wait_for(state='visible')
        assert page.locator('.think-bubble').count() == 1
        message.locator('.think-entry').click()
        assert message.locator('.ts-reasoning').all_text_contents() == [
            '压缩前：核对测试报告。', '压缩后：验收完成，通知负责人。']
        output = ROOT / 'cache' / 'thinking-compaction.png'
        output.parent.mkdir(parents=True, exist_ok=True)
        message.screenshot(path=str(output))
        # The user's already-ended cache must also recover after an ordinary reload.
        page.reload()
        page.evaluate(setup)
        page.evaluate("""saved => {
            localStorage.setItem('group-engine.thinking.v2', saved);
            restoreThinkingState(); loadThinking();
        }""", legacy_cache)
        message.locator('.think-entry').wait_for(state='visible')
        assert page.locator('.think-bubble').count() == 1
        assert page.evaluate('[...S.thinkBubbles.values()][0].reply_message_id') == 6871
        assert not errors, errors
        browser.close()
    print('PASS: formal reply contains pre/post-compaction reasoning; old orphan cache recovers on reload')


if __name__ == '__main__':
    main()
