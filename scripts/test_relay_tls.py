"""Relay transport boundaries and certificate persistence on isolated resources."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.hazmat.primitives import hashes
from engine import relay_certificate, relay_server, relay_switch
from scripts.test_relay_plugin import _plugin, make_cfg


class RelayTLSTests(unittest.TestCase):
    def test_listener_identity_survives_concurrent_initialization_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'data' / 'secrets'
            with ThreadPoolExecutor(4) as pool:
                identities = list(pool.map(lambda _: relay_certificate.identity(folder), range(8)))
            path, cert = relay_certificate.identity(folder)
            self.assertEqual({c.fingerprint(hashes.SHA256()) for _, c in identities}, {cert.fingerprint(hashes.SHA256())})
            self.assertEqual(list(folder.iterdir()), [path])
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(path))

    def test_plaintext_endpoints_reject_before_handshake(self):
        ws = AsyncMock()
        ws.scope = {'scheme': 'ws'}
        asyncio.run(relay_server.relay_ws(ws))
        ws.accept.assert_not_awaited()
        ws.receive_text.assert_not_awaited()
        ws.close.assert_awaited_once()
        client = _plugin.relay.RelayClient(make_cfg('http://example.invalid'))
        with patch('websockets.sync.client.connect') as connect, self.assertRaises(ValueError):
            with client._connect():
                self.fail('legacy transport opened')
        connect.assert_not_called()

    def test_old_plugin_never_receives_return_dial_instruction(self):
        conn, send = MagicMock(), MagicMock()
        self.assertFalse(relay_switch.send_return(conn, 'remote', 1, send, 3))
        send.assert_not_called()
        conn.execute.assert_called_once()

    def test_pin_checked_before_invite_or_credential(self):
        client = _plugin.relay.RelayClient(make_cfg('https://example.invalid', fingerprint='a' * 64))
        socket = MagicMock()
        socket.socket.getpeercert.return_value = b'different certificate'
        with patch('websockets.sync.client.connect') as connect:
            connect.return_value.__enter__.return_value = socket
            with self.assertRaises(ssl.SSLCertVerificationError):
                with client._connect() as ws:
                    client._handshake(ws)
            socket.send.assert_not_called()
