"""Real Chrome selection regression; all page requests use local fixtures.

Run: python -B scripts/verify_message_selection.py
No API server, database, workers, or production writes are used.
"""
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def message(ident, thread=None):
    return dict(id=ident, project='alpha', thread_id=thread, sender='bot',
                sender_type='bot', mentions='[]', flags='{}',
                ts=f'2026-09-23T{9 + ident // 60:02}:{ident % 60:02}:00',
                content=f'Message {ident}: select this exact passage and keep it stable.\n'
                        'Second line for a multi-line selection.')


def main():
    messages = [message(i, 42 if i >= 115 else None) for i in range(101, 131)]
    threads = [dict(id=42, project='alpha', title='Selection topic', status='open',
                    members=['bot'], first_message_id=115, created_at=messages[14]['ts'])]
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})

        def route_request(route):
            url = urlparse(route.request.url)
            if url.path == '/app/':
                route.fulfill(path=str(ROOT / 'frontend/index.html'))
            elif url.path == '/app/attachments.js':
                route.fulfill(path=str(ROOT / 'frontend/attachments.js'))
            elif url.path.startswith('/app/assets/'):
                route.fulfill(path=str(ROOT / 'frontend' / url.path.removeprefix('/app/')))
            elif url.path == '/messages':
                query = parse_qs(url.query)
                if 'before_id' in query:
                    route.fulfill(json={'messages': [message(i) for i in range(91, 101)], 'has_more': False})
                else:
                    selected = [m for m in messages if 'thread_id' not in query
                                or str(m['thread_id']) == query['thread_id'][0]]
                    route.fulfill(json={'messages': selected, 'has_more': True})
            elif url.path == '/threads':
                route.fulfill(json={'threads': threads})
            else:
                route.fulfill(json={})

        page.route('**/*', route_request)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://selection.test/app/')
        page.evaluate("""messages => {
            showLogin(false);
            Object.assign(S, {currentProject: 'alpha', projects: [{name: 'alpha'}],
                members: [{profile: 'bot', display_name: 'Bot', role: 'fe-dev'}],
                threads: [{id: 42, project: 'alpha', title: 'Selection topic', status: 'open',
                    members: ['bot'], first_message_id: 115, created_at: messages[14].ts}],
                msgs: messages, messagesReady: true, hasOlderMessages: true, lastMsgId: 130});
            S.thinkBubbles.set('test', {key: 'test', profile: 'bot', project: 'alpha',
                session: 'bot-alpha-work', thread_id: null, status: 'thinking',
                spawned_at: '2026-09-23T11:00:00', collapsed: false,
                entries: [{id: 1, reasoning: 'Thinking text for selection testing.'}]});
            renderMessages();
            window.messageRenderCount = 0;
            const originalRender = renderMessages;
            renderMessages = (...args) => { messageRenderCount++; return originalRender(...args); };
        }""", messages)

        def clear_selection():
            page.evaluate('getSelection().removeAllRanges()')
            page.evaluate('() => new Promise(requestAnimationFrame)')

        def update_thinking():
            result = page.evaluate("""() => {
                const before = messageRenderCount;
                const bubble = S.thinkBubbles.get('test');
                bubble.entries.push({id: bubble.entries.length + 1, reasoning: 'New reasoning'});
                renderThinkBubbles();
                const panel = document.querySelector('.think-bubble[data-bubble-key="test"]');
                return {listRenders: messageRenderCount - before,
                    visible: panel?.querySelectorAll('.think-step').length,
                    expected: bubble.entries.length};
            }""")
            assert result['listRenders'] == 0, 'thinking content must update only its bubble'
            if result.get('visible') is not None:
                assert result['visible'] == result['expected'], 'thinking updates must stay live during selection'

        def selected_text():
            return page.evaluate('getSelection().toString()')

        def drag_coordinates(ident):
            content = page.locator(f'[data-message-id="{ident}"] .msg-content')
            content.scroll_into_view_if_needed()
            return content.evaluate("""el => {
                const node = el.firstChild, range = document.createRange();
                const point = offset => {
                    range.setStart(node, offset); range.setEnd(node, offset + 1);
                    const rect = range.getBoundingClientRect();
                    return {x: rect.left + 1, y: rect.top + rect.height / 2};
                };
                return {start: point(13), middle: point(20), end: point(35)};
            }""")

        for ident, reverse in [(110, False), (120, True)]:
            clear_selection()
            points = drag_coordinates(ident)
            start, end = (points['end'], points['start']) if reverse else (points['start'], points['end'])
            page.mouse.move(**start)
            page.mouse.down()
            page.evaluate(f"window.selectionNode = document.querySelector('[data-message-id=\"{ident}\"] .msg-content').firstChild")
            # A refresh between mouse-down and the first mouse-move must preserve the anchor.
            update_thinking()
            assert page.evaluate('selectionNode.isConnected'), 'mouse-down anchor was removed by refresh'
            page.mouse.move(**points['middle'], steps=5)
            partial = selected_text()
            assert partial, 'mouse must select real text'
            update_thinking()
            assert selected_text() == partial, 'selection changed during drag'
            page.mouse.move(**end, steps=5)
            page.mouse.up()
            selected = selected_text()
            assert selected and len(selected) < 40, selected
            scroll = page.locator('#msg-list').evaluate('el => el.scrollTop')
            for _ in range(3):
                update_thinking()
            assert selected_text() == selected, 'released selection disappeared'
            assert page.evaluate('selectionNode.isConnected'), 'selected text node was replaced'
            assert page.locator('#msg-list').evaluate('el => el.scrollTop') == scroll
            clear_selection()
            assert page.evaluate('selectionNode.isConnected'), 'deselection must also preserve message nodes'

        # Keyboard/backward multi-node selection remains available for copying.
        page.evaluate("""() => {
            const first = document.querySelector('[data-message-id="108"] .msg-content').firstChild;
            const last = document.querySelector('[data-message-id="110"] .msg-content').firstChild;
            getSelection().setBaseAndExtent(last, 25, first, 10);
        }""")
        selected = selected_text()
        messages.append(message(131))
        page.evaluate('refreshMessages()')
        update_thinking()
        assert selected_text() == selected, 'new messages destroyed a cross-message selection'
        assert page.locator('[data-message-id="131"]').count() == 1, 'new messages must appear during selection'
        clear_selection()
        assert page.locator('[data-message-id="131"]').count() == 1

        # Retraction changes only that message; topic metadata keeps other text intact.
        page.evaluate("""() => {
            const node = document.querySelector('[data-message-id="110"] .msg-content').firstChild;
            getSelection().setBaseAndExtent(node, 12, node, 33);
            handleEvent({type: 'message.retracted', data: {message_id: 111,
                retract_by: 'user', retract_at: '2026-09-23T12:00:00'}});
        }""")
        selected = selected_text()
        assert selected
        assert '已撤回' in page.locator('[data-message-id="111"]').inner_text()
        threads[0]['title'] = 'Updated topic title'
        page.evaluate('loadThreads()')
        assert selected_text() == selected
        assert page.locator('.gg-title').inner_text() == 'Updated topic title'
        clear_selection()

        # A history response inserts immediately while keeping selection and viewport.
        page.locator('#msg-list').evaluate('el => el.scrollTop = 0')
        page.evaluate("""() => {
            const node = document.querySelector('[data-message-id="101"] .msg-content').firstChild;
            getSelection().setBaseAndExtent(node, 12, node, 33);
        }""")
        selected = selected_text()
        offset = page.locator('[data-message-id="101"]').bounding_box()['y']
        page.evaluate('loadOlderMessages()')
        assert selected_text() == selected, 'history insertion destroyed selection'
        assert page.locator('[data-message-id="91"]').count() == 1
        clear_selection()
        assert page.locator('[data-message-id="91"]').count() == 1
        restored_offset = page.locator('[data-message-id="101"]').bounding_box()['y']
        assert abs(restored_offset - offset) < 2, (offset, restored_offset)

        # Selection in a thinking bubble gets the same protection.
        page.evaluate("""() => {
            const node = document.querySelector('.ts-reasoning').firstChild;
            getSelection().setBaseAndExtent(node, 0, node, 13);
        }""")
        selected = selected_text()
        update_thinking()
        assert selected_text() == selected

        # Reply binding moves the existing bubble, retaining its selected reasoning.
        page.evaluate("""() => {
            window.replyBubble = document.querySelector('.think-bubble');
            window.reasoningAnchor = getSelection().anchorNode;
            const bubble = S.thinkBubbles.get('test');
            bubble.reply_message_id = 131;
            bubble.status = 'replied';
            renderThinkBubbles();
        }""")
        assert selected_text() == selected, page.evaluate("""() => ({
            selected: getSelection().toString(), anchorConnected: reasoningAnchor.isConnected,
            sameBubble: document.querySelector('.think-bubble') === replyBubble,
            moveBefore: typeof document.body.moveBefore})""")
        assert page.evaluate('replyBubble.isConnected && replyBubble.classList.contains("think-attached")')
        update_thinking()
        assert selected_text() == selected
        clear_selection()
        page.locator('.think-entry[data-bubble-key="test"]').click()
        assert page.locator('.think-attached').evaluate('el => el.classList.contains("collapsed")')
        page.locator('.think-entry[data-bubble-key="test"]').click()
        assert not page.locator('.think-attached').evaluate('el => el.classList.contains("collapsed")')

        # A live reply editor stays focused while new messages are inserted.
        page.locator('.thread-reply .tr-hint').click()
        page.locator('.tr-text').fill('尚未发送的草稿')
        page.locator('.tr-text').evaluate("el => { window.liveEditor = el; el.setSelectionRange(2, 5); }")
        messages.append(message(132))
        page.evaluate('refreshMessages()')
        assert page.evaluate('document.activeElement === liveEditor && liveEditor.isConnected')
        assert page.locator('.tr-text').input_value() == '尚未发送的草稿'
        assert page.evaluate('[liveEditor.selectionStart, liveEditor.selectionEnd]') == [2, 5]

        # A reply can arrive together with its binding, before its DOM row exists.
        page.evaluate("""() => {
            S.thinkBubbles.set('simultaneous', {...S.thinkBubbles.get('test'),
                key: 'simultaneous', status: 'thinking', reply_message_id: null,
                entries: [{id: 501, reasoning: 'Simultaneous reply'}]});
            renderThinkBubbles();
            window.simultaneousBubble = [...document.querySelectorAll('.think-bubble')]
                .find(el => el.dataset.bubbleKey === 'simultaneous');
            const message = {...S.msgs.at(-1), id: 133, thread_id: null, content: 'Simultaneous reply message'};
            S.msgs.push(message);
            Object.assign(S.thinkBubbles.get('simultaneous'), {status: 'replied', reply_message_id: 133});
            renderMessages();
        }""")
        assert page.locator('.think-bubble[data-bubble-key="simultaneous"]').count() == 1
        assert page.evaluate('simultaneousBubble.isConnected && simultaneousBubble.closest("[data-message-id]").dataset.messageId === "133"')

        # Explicit view changes must remain usable while text is selected.
        page.evaluate('selectThread(42)')
        assert page.locator('[data-message-id="101"]').count() == 0
        assert page.locator('[data-message-id="120"]').count() == 1
        points = drag_coordinates(120)
        page.mouse.move(**points['start'])
        page.mouse.down()
        page.mouse.move(**points['end'], steps=5)
        page.mouse.up()
        selected = selected_text()
        update_thinking()
        assert selected_text() == selected
        clear_selection()

        # A cancelled pointer must not leave updates permanently suspended.
        page.evaluate('window.cancelNode = document.querySelector("[data-message-id]")')
        page.locator('#msg-list').dispatch_event('pointerdown', {'button': 0, 'pointerId': 7})
        update_thinking()
        assert page.evaluate('cancelNode.isConnected')
        page.locator('#msg-list').dispatch_event('pointercancel', {'pointerId': 7})
        page.evaluate('() => new Promise(requestAnimationFrame)')
        assert page.evaluate('cancelNode.isConnected')

        page.evaluate('window.blurNode = document.querySelector("[data-message-id]")')
        page.locator('#msg-list').dispatch_event('pointerdown', {'button': 0, 'pointerId': 8})
        update_thinking()
        assert page.evaluate('blurNode.isConnected')
        page.evaluate('window.dispatchEvent(new Event("blur"))')
        page.evaluate('() => new Promise(requestAnimationFrame)')
        assert page.evaluate('blurNode.isConnected')

        # Text selected outside the conversation never holds back its refreshes.
        page.evaluate("""() => {
            const node = document.querySelector('#msg-title').firstChild;
            getSelection().setBaseAndExtent(node, 0, node, 2);
            window.unrelatedNode = document.querySelector('[data-message-id]');
        }""")
        update_thinking()
        assert page.evaluate('unrelatedNode.isConnected')

        page.evaluate("""() => {
            const node = document.querySelector('[data-message-id="120"] .msg-content').firstChild;
            getSelection().setBaseAndExtent(node, 12, node, 33);
        }""")
        page.evaluate("selectProject('beta')")
        page.wait_for_function('S.messagesReady')
        assert page.locator('[data-message-id]').count() == 0, 'project change must replace the old selected view'
        assert not errors, errors
        browser.close()
    print('PASS: forward/backward mouse drags, released/keyboard/multi-node selections, '
          'live scoped thinking updates, new messages, history viewport, retraction, topic title, '
          'reply binding/folding, editor focus/draft, view changes, pointer cancellation')


if __name__ == '__main__':
    main()
