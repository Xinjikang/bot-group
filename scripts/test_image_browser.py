"""Real browser + MCP protocol smoke test. Uses temporary data and no bot workers.

Run with a Python that has playwright and mcp (the configured MCP interpreter).
"""
import ast
import asyncio
import base64
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from playwright.sync_api import sync_playwright
import uvicorn
from test_image_attachments import AttachmentTests
from engine import api, auth


def main():
    fixture = AttachmentTests()
    fixture.setUp()
    app = FastAPI()
    app.include_router(api.router_api)
    app.mount('/app', StaticFiles(directory='frontend', html=True))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    worker = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    worker.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.add_init_script("sessionStorage.removeItem('ge_token')")
            page.on('pageerror', lambda error: errors.append(str(error)))
            image_requests = []
            page.on('request', lambda request: image_requests.append(request.url)
                    if '/attachments/' in request.url else None)
            page.on('dialog', lambda dialog: (errors.append(dialog.message), dialog.dismiss()))
            page.route('**/reasoning*', lambda route: route.fulfill(json={'entries': [], 'runs': []}))
            page.goto(f'http://127.0.0.1:{port}/app/')
            page.wait_for_function("typeof S !== 'undefined' && S.currentProject === 'one'")
            page.evaluate('selectThread(1)')
            page.locator('#composer input[type=file]').set_input_files({'name': 'audit.png', 'mimeType': 'image/png', 'buffer': fixture.png})
            page.locator('#composer .image-drafts img').wait_for()
            page.locator('#btn-send').click()
            page.wait_for_function("document.querySelectorAll('#composer .image-drafts img').length === 0")
            page.evaluate('refreshMessages()')
            page.wait_for_function("document.querySelector('.chat-image')?.naturalWidth === 32")
            requests_before = len(image_requests)
            page.evaluate("""async () => {
                const image = document.querySelector('.chat-image');
                const source = image.src;
                for (let i = 0; i < 20; i++) {
                    renderMessages();
                    await new Promise(requestAnimationFrame);
                    if (document.querySelector('.chat-image') !== image || image.src !== source || image.naturalWidth !== 32)
                        throw new Error('Image changed or became unloaded during message redraw');
                }
            }""")
            assert len(image_requests) == requests_before, 'Redraw must reuse the loaded image without fetching it again'
            page.locator('.chat-image').first.click()
            page.locator('dialog.image-preview').wait_for(state='visible')
            for viewport in [{'width': 1280, 'height': 900}, {'width': 620, 'height': 800}]:
                page.set_viewport_size(viewport)
                for dimensions in [[2400, 1200], [1200, 2400]]:
                    geometry = page.locator('dialog.image-preview img').evaluate("""async (img, size) => {
                        const canvas = document.createElement('canvas');
                        [canvas.width, canvas.height] = size;
                        img.src = canvas.toDataURL();
                        await img.decode();
                        const dialog = img.closest('dialog');
                        const bounds = dialog.getBoundingClientRect();
                        const picture = img.getBoundingClientRect();
                        return {width: bounds.width, fits: dialog.scrollWidth <= dialog.clientWidth
                            && dialog.scrollHeight <= dialog.clientHeight
                            && bounds.left >= 0 && bounds.right <= innerWidth
                            && bounds.top >= 0 && bounds.bottom <= innerHeight
                            && picture.left >= bounds.left && picture.right <= bounds.right};
                    }""", dimensions)
                    assert geometry['fits'], geometry
                    if viewport['width'] == 1280 and dimensions[0] == 2400:
                        assert geometry['width'] > 380, 'Landscape preview must expand past the default dialog width'
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.locator('dialog.image-preview button').click()
            page.reload()
            page.wait_for_function("document.querySelector('.chat-image')?.naturalWidth === 32")
            page.evaluate("selectProject('two')")
            page.wait_for_function("S.currentProject === 'two' && S.messagesReady")
            assert page.locator('.chat-image').count() == 0
            page.evaluate("selectProject('one')")
            page.wait_for_function("document.querySelector('.chat-image')?.naturalWidth === 32")
            # Destination-specific unsent drafts survive switching, without leaking.
            page.locator('#composer input[type=file]').set_input_files({'name': 'draft.png', 'mimeType': 'image/png', 'buffer': fixture.png})
            page.evaluate("selectProject('two')")
            assert page.locator('#composer .image-drafts img').count() == 0
            page.evaluate("selectProject('one')")
            assert page.locator('#composer .image-drafts img').count() == 1
            page.locator('#composer .image-drafts button').click()
            page.locator('.thread-reply .tr-hint').first.click()
            # Paste into the inline topic editor, then send without text.
            page.locator('.tr-text').evaluate("""(el, data) => {
                const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
                const transfer = new DataTransfer();
                transfer.items.add(new File([bytes], 'paste.png', {type:'image/png'}));
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:transfer, bubbles:true, cancelable:true}));
            }""", base64.b64encode(fixture.png).decode())
            page.locator('.thread-reply .image-drafts img').wait_for()
            page.locator('.tr-send').click()
            page.locator('.tr-text').wait_for(state='detached')
            page.evaluate('refreshMessages()')
            page.wait_for_function("document.querySelectorAll('.chat-image').length === 2")
            page.locator('#composer-text').fill('@alice inspect this image')
            page.locator('#composer').evaluate("""(el, data) => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], 'drop.png', {type:'image/png'}));
                el.dispatchEvent(new DragEvent('drop', {dataTransfer:transfer, bubbles:true, cancelable:true}));
            }""", base64.b64encode(fixture.png).decode())
            def switch_during_upload(route):
                page.evaluate("selectProject('two')")
                route.continue_()
            page.route('**/attachments?*', switch_during_upload)
            page.locator('#btn-send').click()
            page.wait_for_function("imageSends.size === 0 && S.currentProject === 'two'")
            assert fixture.client.get('/messages?project=two').json()['count'] == 0
            assert fixture.client.get('/messages?project=one').json()['count'] == 3
            page.unroute('**/attachments?*', switch_during_upload)
            page.evaluate("selectProject('one')")
            page.wait_for_function("S.currentProject === 'one' && S.messagesReady")
            page.evaluate('openDialogAnchored(document.querySelector("#dlg-thread"), null)')
            page.locator('#dlg-title').fill('Local image opening')
            page.locator('#thread-opening-editor input[type=file]').set_input_files(
                {'name': 'opening.png', 'mimeType': 'image/png', 'buffer': fixture.png})
            with page.expect_response(lambda response: response.url.endswith('/thread')
                                      and response.request.method == 'POST') as created:
                page.locator('#thread-form [type=submit]').click()
            assert created.value.status == 200, created.value.text()
            topic_id = created.value.json()['thread_id']
            page.locator('#dlg-thread').wait_for(state='hidden')
            page.evaluate('(id) => selectThread(id)', topic_id)
            page.wait_for_function("document.querySelector('.chat-image')?.naturalWidth === 32")
            page.reload()
            page.wait_for_function("document.querySelector('.chat-image')?.naturalWidth === 32")
            topic = fixture.client.get('/thread', params={'id': topic_id}).json()
            assert topic['messages'][0]['sender'] == 'user'
            assert topic['messages'][0]['attachments'][0]['width'] == 32
            browser.close()
        assert not errors, errors

        # Register the real MCP function in the actual SDK and check its output blocks.
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'group_read_image')
        mcp = FastMCP('image-test')
        scope = {'mcp': mcp, 'Image': Image, 'httpx': Mock(), '_engine_url': lambda: 'http://engine',
                 '_engine_headers': lambda: {'X-Group-Profile': 'alice'}}
        attachment = fixture.client.get('/thread?id=1').json()['messages'][0]['attachments'][0]
        response = fixture.client.get('/attachments/' + attachment['id'], headers={'X-Group-Profile': 'alice', 'X-Group-Key': auth.bot_token('alice')})
        scope['httpx'].get.return_value = response
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'mcp/server.py', 'exec'), scope)
        blocks = asyncio.run(mcp.call_tool('group_read_image', {'attachment_id': attachment['id']}))
        if isinstance(blocks, tuple):
            blocks = blocks[0]
        image = next(block for block in blocks if block.type == 'image')
        assert image.mimeType == 'image/png'
        assert base64.b64decode(image.data) == fixture.png
        print('PASS: browser upload, image-only send/topic opening, preview, reload, project switching, draft isolation; MCP ImageContent bytes')
    except Exception:
        print('Browser errors:', errors)
        raise
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        sock.close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
