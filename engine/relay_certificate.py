"""Persistent TLS identity of the engine's remote listener."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import config


def identity(folder=None):
    folder = config.data_dir() / 'secrets' if folder is None else folder
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / 'relay-listener.pem'
    if not path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'bot-group relay')])
        now = datetime.now(timezone.utc)
        import psutil
        addresses = {ipaddress.ip_address(addr.address.split('%')[0])
                     for entries in psutil.net_if_addrs().values() for addr in entries
                     if addr.family in (socket.AF_INET, socket.AF_INET6)}
        names = [x509.DNSName('localhost'), *(x509.IPAddress(addr) for addr in addresses)]
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=825))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName(names), critical=False)
                .sign(key, hashes.SHA256()))
        staged = tempfile.NamedTemporaryFile(dir=folder, delete=False)
        try:
            with staged:
                staged.write(cert.public_bytes(serialization.Encoding.PEM))
                staged.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
                staged.flush()
                os.fsync(staged.fileno())
            try:
                os.link(staged.name, path)
            except FileExistsError:
                pass
        finally:
            Path(staged.name).unlink(missing_ok=True)
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    return path, cert


def listener_config(app, pem, **options):
    import uvicorn
    config = uvicorn.Config(app, ssl_certfile=str(pem), ssl_keyfile=str(pem), **options)
    config.load()
    if ssl.HAS_TLSv1_3:
        # Relay clients open a fresh TLS session on every dial.
        config.ssl.num_tickets = 0
    return config


def fingerprint():
    return identity()[1].fingerprint(hashes.SHA256()).hex()


def public_pem():
    return identity()[1].public_bytes(serialization.Encoding.PEM).decode('ascii')
