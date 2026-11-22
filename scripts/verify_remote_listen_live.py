"""Live req#172 check: enable a concrete interface through the settings page,
then connect MCP HTTP and relay WebSocket through the remote listener.

Uses an isolated data directory and the assigned backend port. The loopback
server serves the browser/API; listen_control starts a second uvicorn Server on
the selected local interface. Remote connections come from this host's selected
address, so the member source binding must include that address.
"""
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

PORT = 18042
PASSWORD = 'listen-live-admin-pass'
MEMBER = 'listen-live'


def local_ipv4():
    from engine import listen_control
    for entry in listen_control.enumerate_interfaces():
        if entry.get('family') == 'IPv4':
            return entry['address']
    return None


def http_status(host, path='/health', timeout=3.0):
    try:
        with socket.create_connection((host, PORT), timeout=timeout) as sock:
            sock.sendall(f'GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n'.encode())
            data = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        first = data.split(b'\r\n', 1)[0].decode(errors='replace')
        return int(first.split()[1]) if first.startswith('HTTP/') else None
    except OSError:
        return None


def listening_addresses():
    import psutil
    addresses = set()
    for item in psutil.net_connections(kind='tcp'):
        if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == PORT:
            addresses.add(item.laddr.ip)
    return addresses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)

    lan = local_ipv4()
    if not lan:
        raise RuntimeError('本机没有可用于远程监听的 IPv4 单播地址')

    with tempfile.TemporaryDirectory(prefix='listen-live-') as tmp:
        data_dir = Path(tmp)
        os.environ['GROUP_DATA_DIR'] = str(data_dir)
        os.environ['GROUP_DB'] = str(data_dir / 'group.db')
        os.environ['GROUP_ADMIN_PASSWORD'] = PASSWORD
        os.environ['GROUP_SECRET'] = 'listen-live-secret'
        os.environ['GROUP_ENGINE_PORT'] = str(PORT)

        import uvicorn
        from fastapi import FastAPI, WebSocket
        from fastapi.staticfiles import StaticFiles
        from engine import db, listen_control, relay_server
        from engine.api import router_api

        conn = db.get_connection()
        try:
            db.init_schema(conn)
            cur = conn.execute(
                "INSERT INTO bots(host_type,profile,config_root,runtime,display_name) "
                "VALUES('hermes',?,?, 'hermes','Live Hermes')",
                (MEMBER, str(data_dir)),
            )
            conn.execute(
                "INSERT INTO members(profile,display_name,bot_id,source,created_at) "
                "VALUES(?,?,?,'api',datetime('now'))",
                (MEMBER, 'Live Hermes', cur.lastrowid),
            )
            conn.commit()
        finally:
            conn.close()

        app = FastAPI()
        app.include_router(router_api)

        @app.get('/health')
        def health():
            return {'ok': True}

        @app.websocket('/relay/ws')
        async def relay_ws_endpoint(websocket: WebSocket):
            await relay_server.relay_ws(websocket)

        app.mount('/app', StaticFiles(directory=str(ROOT / 'frontend'), html=True), name='frontend')

        sock = socket.socket()
        sock.bind(('127.0.0.1', PORT))
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
                page.goto(f'http://127.0.0.1:{PORT}/app/index.html')
                page.locator('#login-pwd').wait_for(state='visible')
                page.locator('#login-pwd').fill(PASSWORD)
                page.locator('#login-form button[type="submit"]').click()
                page.wait_for_function('document.querySelector("#login").hidden && S.isUserIdentity === true')

                page.locator('#btn-settings').click()
                page.locator('#btn-relay-nodes').click()
                page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
                page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
                page.wait_for_function('document.querySelector("#relay-register-gate").textContent === ""')
                page.screenshot(path=str(args.evidence / 'listen-disabled-1280-dark.png'))

                page.locator('#relay-member option[value="listen-live"]').wait_for(state='attached')
                page.locator('#relay-member').select_option(MEMBER)
                page.locator('#relay-register-ip').fill(lan)
                page.locator('#relay-register-submit').click()
                page.wait_for_function(
                    f'document.querySelector("#relay-issued-member").value === {json.dumps(MEMBER)}')
                credential = page.locator('#relay-issued-credential').input_value()
                assert credential
                page.wait_for_function('!document.querySelector("#relay-close").disabled')
                page.wait_for_function('!document.querySelector("#relay-listen-address").disabled')

                page.locator('#relay-listen-address').select_option(lan)
                page.wait_for_function('!document.querySelector("#relay-listen-save").disabled')
                page.locator('#relay-listen-save').click()
                page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "已开启"')
                page.wait_for_function('!document.querySelector("#relay-listen-copy").disabled')
                assert lan in page.locator('#relay-listen-summary').inner_text()
                assert f'{lan}:{PORT}' in page.locator('#relay-listen-summary').inner_text()
                assert page.locator('#relay-listen-url').input_value() == f'http://{lan}:{PORT}'
                listeners = listening_addresses()
                assert lan in listeners and '127.0.0.1' in listeners, listeners
                assert '0.0.0.0' not in listeners and '::' not in listeners, listeners
                page.screenshot(path=str(args.evidence / 'listen-enabled-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                # MCP-facing HTTP through the remote listener.
                assert http_status(lan) == 200, f'{lan}:{PORT}/health 未连通'

                # relay WebSocket through the remote listener, not loopback.
                ws = connect(f'ws://{lan}:{PORT}/relay/ws', open_timeout=5, close_timeout=1, legacy=True)
                ws.send(json.dumps({
                    'relay': {'kind': 'hello'}, 'member': MEMBER, 'credential': credential,
                    'code_version': code_version, 'mcp_fingerprint': 'fedcba9876543210',
                    'protocol_version': 1,
                }))
                reply = json.loads(ws.recv(timeout=5))
                assert reply.get('relay', {}).get('kind') == 'hello_ok', reply
                page.locator('#relay-refresh').click()
                page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("在线")')
                online_text = page.locator('#relay-node-list article[data-relay-member="listen-live"]').inner_text()
                assert lan in online_text, online_text
                page.screenshot(path=str(args.evidence / 'listen-online-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                # 390px themes with the listen block visible.
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#relay-nodes-dialog').evaluate('(el) => { el.scrollTop = 0; }')
                for theme in ('dark', 'light'):
                    page.evaluate('(theme) => applyTheme(theme)', theme)
                    box = page.locator('#relay-nodes-dialog').bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= 390
                    overflow = page.locator('#relay-nodes-dialog').evaluate('(el) => el.scrollWidth - el.clientWidth')
                    assert overflow <= 0, overflow
                    page.screenshot(path=str(args.evidence / f'listen-enabled-390-{theme}.png'),
                                    mask=[page.locator('#relay-issued-credential')])
                page.set_viewport_size({'width': 1280, 'height': 900})
                page.evaluate("applyTheme('dark')")

                # Disabling returns to loopback and closes the remote listener.
                page.wait_for_function('!document.querySelector("#relay-listen-disable").disabled')
                page.locator('#relay-listen-disable').click()
                page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
                assert http_status(lan) is None, '停用后远程地址仍可连接'
                assert http_status('127.0.0.1') == 200, '停用后本机回环不可用'
                listeners = listening_addresses()
                assert listeners == {'127.0.0.1'}, listeners
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None

                assert not errors, errors
                browser.close()
                browser = None
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            listen_control.ensure(None)
            relay_server.close_all()
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            if thread.is_alive():
                raise RuntimeError('隔离引擎未停止')
    print(f'PASS: live remote listen on {lan}:{PORT}, HTTP 200, relay WS hello_ok, disable returns to loopback')


if __name__ == '__main__':
    main()
