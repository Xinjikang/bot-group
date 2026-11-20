"""Live relay-node browser check: real engine API + real WebSocket handshake."""
import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PASSWORD = 'relay-live-admin-pass'
MEMBER = 'remote-live'
MEMBER2 = 'remote-live-2'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='relay-live-') as tmp:
        data_dir = Path(tmp)
        os.environ['GROUP_DATA_DIR'] = str(data_dir)
        os.environ['GROUP_DB'] = str(data_dir / 'group.db')
        os.environ['GROUP_ADMIN_PASSWORD'] = PASSWORD
        os.environ['GROUP_SECRET'] = 'relay-live-secret'

        import uvicorn
        from fastapi import FastAPI, WebSocket
        from fastapi.staticfiles import StaticFiles
        from engine import db, relay_server
        from engine.api import router_api

        conn = db.get_connection()
        try:
            db.init_schema(conn)
            for profile, display_name in ((MEMBER, 'ECS Hermes'), (MEMBER2, '备机 Hermes')):
                cur = conn.execute(
                    "INSERT INTO bots(host_type,profile,config_root,runtime,display_name) "
                    "VALUES('hermes',?,?, 'hermes',?)",
                    (profile, str(data_dir), display_name),
                )
                conn.execute(
                    "INSERT INTO members(profile,display_name,bot_id,source,created_at) "
                    "VALUES(?,?,?,'api',datetime('now'))",
                    (profile, display_name, cur.lastrowid),
                )
            conn.commit()
        finally:
            conn.close()

        app = FastAPI()
        app.include_router(router_api)

        @app.websocket('/relay/ws')
        async def relay_ws_endpoint(websocket: WebSocket):
            await relay_server.relay_ws(websocket)

        app.mount('/app', StaticFiles(directory=str(ROOT / 'frontend'), html=True), name='frontend')

        sock = socket.socket()
        sock.bind(('127.0.0.1', 18042))
        sock.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('隔离引擎启动失败')
            time.sleep(0.01)

        code_version = relay_server._engine_release_version() or ''
        ws = None
        ws2 = None
        browser = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                context = browser.new_context(
                    viewport={'width': 1280, 'height': 900},
                    permissions=['clipboard-read', 'clipboard-write'],
                )
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:18042/app/index.html')
                page.locator('#login-pwd').wait_for(state='visible')
                page.locator('#login-pwd').fill(PASSWORD)
                page.locator('#login-form button[type="submit"]').click()
                page.wait_for_function('document.querySelector("#login").hidden && S.isUserIdentity === true')

                page.locator('#btn-settings').click()
                assert page.locator('#btn-relay-nodes').is_visible()
                page.locator('#btn-relay-nodes').click()
                page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
                page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("暂无已登记节点")')
                page.wait_for_function('document.querySelector("#relay-register-gate").textContent === ""')
                page.locator('#relay-member option[value="remote-live"]').wait_for(state='attached')

                def register(member, ip):
                    page.locator('#relay-member').select_option(member)
                    page.locator('#relay-register-ip').fill(ip)
                    assert page.locator('#relay-register-submit').is_enabled()
                    page.locator('#relay-register-submit').click()
                    page.wait_for_function(
                        f'document.querySelector("#relay-issued-member").value === {json.dumps(member)}')
                    assert page.locator('#relay-issued-member').input_value() == member
                    page.wait_for_function('!document.querySelector("#relay-close").disabled')
                    return page.locator('#relay-issued-credential').input_value()

                credential = register(MEMBER, '100.64.0.10')
                assert credential
                page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("100.64.0.10")')
                page.screenshot(path=str(args.evidence / 'relay-live-issued-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])
                credential2 = register(MEMBER2, '100.64.0.11')
                assert credential2 and credential2 != credential
                page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("100.64.0.11")')
                page.screenshot(path=str(args.evidence / 'relay-live-bindings-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                page.locator('#relay-copy-credential').click()
                page.wait_for_function('document.querySelector("#relay-register-status").textContent.includes("已复制连接凭据")')
                assert page.evaluate('navigator.clipboard.readText()') == credential2

                def hello(link_credential):
                    link = connect('ws://127.0.0.1:18042/relay/ws', open_timeout=5, close_timeout=1, legacy=True)
                    link.send(json.dumps({
                        'relay': {'kind': 'hello'}, 'member': MEMBER, 'credential': link_credential,
                        'code_version': code_version, 'mcp_fingerprint': 'fedcba9876543210',
                        'protocol_version': 1,
                    }))
                    return link, json.loads(link.recv(timeout=5))

                ws, reply = hello(credential)
                assert reply.get('relay', {}).get('kind') == 'hello_ok', reply
                generation = reply['relay']['generation']
                assert int(generation) >= 2

                page.locator('#relay-refresh').click()
                page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("在线")')
                online_text = page.locator('#relay-node-list').inner_text()
                assert '最近心跳' in online_text and '—' not in online_text.split('最近心跳', 1)[1].split('占用', 1)[0]
                assert 'MCP 指纹' in online_text and 'fedcba9876543210' in online_text
                remote_text = page.locator('#relay-node-list article[data-relay-member="remote-live"]').inner_text()
                spare_text = page.locator('#relay-node-list article[data-relay-member="remote-live-2"]').inner_text()
                assert '100.64.0.10' in remote_text and '127.0.0.1' in remote_text
                assert '尚未连接' not in remote_text
                assert '100.64.0.11' in spare_text and '尚未连接' in spare_text
                page.screenshot(path=str(args.evidence / 'relay-live-online-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                page.set_viewport_size({'width': 390, 'height': 844})
                for theme in ('dark', 'light'):
                    page.evaluate('(theme) => applyTheme(theme)', theme)
                    box = page.locator('#relay-nodes-dialog').bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= 390
                    overflow = page.locator('#relay-nodes-dialog').evaluate('(el) => el.scrollWidth - el.clientWidth')
                    assert overflow <= 0, overflow
                    page.screenshot(path=str(args.evidence / f'relay-live-390-{theme}.png'),
                                    mask=[page.locator('#relay-issued-credential')])
                page.set_viewport_size({'width': 1280, 'height': 900})
                page.evaluate("applyTheme('dark')")

                # Reopen the settings dialog: member-to-IP bindings remain and credentials do not.
                page.locator('#relay-close').click()
                page.wait_for_function('!document.querySelector("#relay-nodes-dialog").open')
                page.locator('#btn-settings').click()
                page.locator('#btn-relay-nodes').click()
                page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
                page.wait_for_function('document.querySelector("#relay-register-gate").textContent === ""')
                assert page.locator('#relay-credential').is_hidden()
                reopened_remote = page.locator('#relay-node-list article[data-relay-member="remote-live"]').inner_text()
                reopened_spare = page.locator('#relay-node-list article[data-relay-member="remote-live-2"]').inner_text()
                assert '100.64.0.10' in reopened_remote and '127.0.0.1' in reopened_remote
                assert '100.64.0.11' in reopened_spare and '尚未连接' in reopened_spare
                page.screenshot(path=str(args.evidence / 'relay-live-reopened-bindings-1280-dark.png'))

                # Reissue must send rotate=true, close the old link, reject the old credential and accept the new one.
                page.locator('#relay-node-list article[data-relay-member="remote-live"] button[data-action="reissue"]').click()
                page.wait_for_function('document.querySelector("#app-alert").open')
                page.locator('#app-alert button', has_text='确定').click()
                page.wait_for_function('!document.querySelector("#relay-credential").hidden')
                new_credential = page.locator('#relay-issued-credential').input_value()
                assert new_credential and new_credential != credential
                assert credential not in page.locator('#relay-nodes-dialog').inner_text()
                page.screenshot(path=str(args.evidence / 'relay-live-reissued-issued-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])
                try:
                    ws.recv(timeout=2)
                except Exception:
                    pass  # 旧连接由 rotate 主动关闭。
                try:
                    old_probe, old_reply = hello(credential)
                except Exception:
                    pass  # 旧凭据握手被拒后连接关闭。
                else:
                    assert old_reply.get('relay', {}).get('kind') != 'hello_ok', old_reply
                    old_probe.close()
                ws2, reply2 = hello(new_credential)
                assert reply2.get('relay', {}).get('kind') == 'hello_ok', reply2
                page.locator('#relay-refresh').click()
                page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("在线")')
                page.screenshot(path=str(args.evidence / 'relay-live-reissued-online-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                page.locator('#relay-node-list article[data-relay-member="remote-live"] button[data-action="revoke"]').click()
                page.wait_for_function('document.querySelector("#app-alert").open')
                page.locator('#app-alert button', has_text='确定').click()
                page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("已吊销")')
                page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("凭据已吊销")')
                assert ws is not None and ws2 is not None
                for link in (ws, ws2):
                    try:
                        link.recv(timeout=1)
                    except Exception:
                        pass  # 现连由 revoke 主动关闭，读端以关闭异常结束。

                page.locator('#relay-close').click()
                page.wait_for_function('!document.querySelector("#relay-nodes-dialog").open')
                page.reload()
                page.wait_for_function('document.querySelector("#login").hidden && S.isUserIdentity === true')
                page.locator('#btn-settings').click()
                page.locator('#btn-relay-nodes').click()
                page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
                assert page.locator('#relay-credential').is_hidden()
                assert credential not in page.locator('#relay-nodes-dialog').inner_text()
                assert credential2 not in page.locator('#relay-nodes-dialog').inner_text()
                assert new_credential not in page.locator('#relay-nodes-dialog').inner_text()
                assert not errors, errors
                browser.close()
                browser = None
        finally:
            for link in (ws, ws2):
                if link is not None:
                    try:
                        link.close()
                    except Exception:
                        pass
            if browser is not None:
                browser.close()
            relay_server.close_all()
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            if thread.is_alive():
                raise RuntimeError('隔离引擎未停止')
    print('PASS: live relay registration, real handshake online, rotate reissue with old credential rejected and new credential online, revoke offline, credentials hidden after reload, 390px themes')


if __name__ == '__main__':
    main()
