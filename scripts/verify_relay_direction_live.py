"""Live req#173 browser check: A -> B -> A through the settings page.

Requires an isolated data directory and the assigned engine port. Direction A
uses a real WebSocket client against the loopback /relay/ws endpoint; direction
B uses the actual plugin ListenServer (self-signed TLS + one-shot pairing) and
the engine's outbound dialer. The plugin is loaded from the repository package,
not copied or reimplemented.
"""
import argparse
import importlib.util
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
PASSWORD = 'direction-live-admin-pass'
MEMBER = 'direction-live'


def local_ipv4():
    from engine import listen_control
    for entry in listen_control.enumerate_interfaces():
        if entry.get('family') == 'IPv4':
            return entry['address']
    return None


def load_plugin():
    package_dir = ROOT / 'plugins' / 'hermes' / 'bot-group-relay'
    spec = importlib.util.spec_from_file_location(
        'bot_group_relay', package_dir / '__init__.py',
        submodule_search_locations=[str(package_dir)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['bot_group_relay'] = package
    spec.loader.exec_module(package)
    from bot_group_relay import config, listen, pairing
    return config, listen, pairing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)

    lan = local_ipv4()
    if not lan:
        raise RuntimeError('本机没有可用于测试的 IPv4 单播地址')
    plugin_config, plugin_listen, plugin_pairing = load_plugin()

    with tempfile.TemporaryDirectory(prefix='direction-live-') as tmp:
        tmp = Path(tmp)
        os.environ['GROUP_DATA_DIR'] = str(tmp / 'data')
        os.environ['GROUP_DB'] = str(tmp / 'data' / 'group.db')
        os.environ['GROUP_ADMIN_PASSWORD'] = PASSWORD
        os.environ['GROUP_SECRET'] = 'direction-live-secret'
        os.environ['GROUP_ENGINE_PORT'] = str(PORT)

        import uvicorn
        from fastapi import FastAPI, WebSocket
        from fastapi.staticfiles import StaticFiles
        from engine import db, listen_control, relay_dialer, relay_server
        from engine.api import router_api

        conn = db.get_connection()
        try:
            db.init_schema(conn)
            cur = conn.execute(
                "INSERT INTO bots(host_type,profile,config_root,runtime,display_name) "
                "VALUES('hermes',?,?, 'hermes','Direction Hermes')",
                (MEMBER, str(tmp)))
            conn.execute(
                "INSERT INTO members(profile,display_name,bot_id,source,created_at) "
                "VALUES(?,?,?,'api',datetime('now'))",
                (MEMBER, 'Direction Hermes', cur.lastrowid))
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

        engine_version = relay_server._engine_release_version() or ''
        fake_a = None
        plugin_server = None
        browser = None

        def direction_connection(credential):
            ws = connect(f'ws://127.0.0.1:{PORT}/relay/ws', open_timeout=5,
                         close_timeout=1, legacy=True)
            ws.send(json.dumps({
                'relay': {'kind': 'hello'}, 'member': MEMBER, 'credential': credential,
                'code_version': engine_version, 'plugin_version': engine_version,
                'mcp_fingerprint': 'abcdef0123456789', 'protocol_version': 1,
            }))
            reply = json.loads(ws.recv(timeout=5))
            assert reply.get('relay', {}).get('kind') == 'hello_ok', reply
            return ws

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
                page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("暂无")')
                page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')

                page.locator('#relay-member').select_option(MEMBER)
                page.locator('#relay-register-ip').fill('192.0.2.10')
                page.locator('#relay-register-submit').click()
                page.wait_for_function('!document.querySelector("#relay-issued-credential").value')
                page.wait_for_function('document.querySelector("#relay-issued-credential").value.length > 0')
                credential = page.locator('#relay-issued-credential').input_value()
                assert credential

                # Direction A requires the engine listener before switching there.
                page.locator('#relay-listen-address').select_option(lan)
                page.locator('#relay-listen-save').click()
                page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "已开启"')
                fake_a = direction_connection(credential)
                page.locator('#relay-refresh').click()
                page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("已连通")')
                assert page.locator('#relay-direction-listen').get_attribute('aria-pressed') == 'true'
                assert page.locator('#relay-stage-listen').get_attribute('class') == 'relay-stage-done'
                assert page.locator('#relay-stage-auth').get_attribute('class') == 'relay-stage-done'
                assert lan in page.locator('#relay-direction-local').inner_text()
                page.screenshot(path=str(args.evidence / 'direction-a-online-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                # Start the real plugin listener with a temporary certificate and pairing state.
                cert, key = str(tmp / 'cert.pem'), str(tmp / 'key.pem')
                plugin_listen._generate_cert_with_cryptography(Path(cert), Path(key))
                fingerprint = plugin_listen.certificate_fingerprint(cert)
                plugin_pairing._state_dir = lambda: tmp / 'pair-state'
                plugin_server = plugin_listen.ListenServer(
                    address=lan, port=0, cert_path=cert, key_path=key,
                    advertise=lan, member=MEMBER, home=str(tmp / 'plugin-home'),
                    mcp_python=sys.executable, scheme='wss')
                plugin_server.start()
                pairing = plugin_pairing.issue_pairing(
                    member=MEMBER, advertise=lan, port=plugin_server.bound_port(),
                    fingerprint=fingerprint, scheme='wss')
                pairing_url = pairing['pairing_url']
                assert 'pairing_token=' in pairing_url and fingerprint in pairing_url

                page.locator('#relay-direction-dial').click()
                page.wait_for_function('!document.querySelector("#relay-pairing").hidden')
                page.locator('#relay-pairing-url').fill(pairing_url)
                page.locator('#relay-pairing-parse').click()
                page.wait_for_function('!document.querySelector("#relay-pairing-preview").hidden')
                assert 'Direction Hermes' in page.locator('#relay-pairing-identity').inner_text()
                assert fingerprint[:8] in page.locator('#relay-pairing-fingerprint').inner_text()
                page.locator('#relay-pairing-confirm').click()
                page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("已连通")')
                page.wait_for_function('document.querySelector("#relay-direction-dial").getAttribute("aria-pressed") === "true"')
                assert page.locator('#relay-stage-auth').get_attribute('class') == 'relay-stage-done'
                assert page.locator('#relay-stage-mcp').get_attribute('class') == 'relay-stage-done'
                assert lan in page.locator('#relay-direction-remote').inner_text()
                assert str(plugin_server.bound_port()) in page.locator('#relay-direction-remote').inner_text()
                assert page.locator('#relay-pairing-preview').is_hidden()
                assert plugin_server._active_ws is not None, '引擎未拨通插件监听'
                page.screenshot(path=str(args.evidence / 'direction-b-online-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                # Switch back to A; the engine closes the dial channel and the plugin
                # reconnects the direction-A WS with the same member credential.
                page.locator('#relay-direction-listen').click()
                page.wait_for_function('document.querySelector("#relay-direction-badge").textContent === "切换中"')
                if fake_a is not None:
                    try:
                        fake_a.close()
                    except Exception:
                        pass
                fake_a = direction_connection(credential)
                page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("已连通")')
                page.wait_for_function('document.querySelector("#relay-direction-listen").getAttribute("aria-pressed") === "true"')
                assert page.locator('#relay-stage-auth').get_attribute('class') == 'relay-stage-done'
                page.screenshot(path=str(args.evidence / 'direction-a-again-1280-dark.png'),
                                mask=[page.locator('#relay-issued-credential')])

                page.set_viewport_size({'width': 390, 'height': 844})
                for theme in ('dark', 'light'):
                    page.evaluate('(theme) => applyTheme(theme)', theme)
                    page.locator('#relay-direction').scroll_into_view_if_needed()
                    box = page.locator('#relay-nodes-dialog').bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= 390
                    overflow = page.locator('#relay-nodes-dialog').evaluate('(el) => el.scrollWidth - el.clientWidth')
                    assert overflow <= 0, (theme, overflow)
                    assert page.locator('#relay-direction').is_visible()
                    page.screenshot(path=str(args.evidence / f'direction-390-{theme}.png'),
                                    mask=[page.locator('#relay-issued-credential')])
                assert not errors, errors
                browser.close()
                browser = None
        finally:
            if fake_a is not None:
                try:
                    fake_a.close()
                except Exception:
                    pass
            if plugin_server is not None:
                try:
                    plugin_server.stop()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            relay_dialer.stop_dial(MEMBER)
            listen_control.ensure(None)
            relay_server.close_all()
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            if thread.is_alive():
                raise RuntimeError('隔离引擎未停止')
    print(f'PASS: live A->B->A on {lan}, real plugin listen + engine dial and loopback register')


if __name__ == '__main__':
    main()
