"""Explicit resources for portable, isolated test entry points."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit


def absolute_path(value: str, name: str) -> Path:
    if not value or not value.strip():
        raise ValueError(f"Set {name} to an absolute path for this test workspace")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve()


def required_directory(name: str) -> Path:
    return absolute_path(os.environ.get(name, ""), name)


def hermes_source() -> str:
    source = required_directory("HERMES_TEST_SOURCE")
    if not (source / "hermes_cli").is_dir():
        raise ValueError("HERMES_TEST_SOURCE must contain the installed Hermes source (hermes_cli)")
    return str(source)


def port_number(value: str | int) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    return port


def loopback_url(value: str) -> str:
    """Require an explicit IPv4 loopback HTTP origin with an explicit port."""
    target = urlsplit(value)
    if (target.scheme != "http" or target.hostname != "127.0.0.1"
            or target.username or target.password or target.query or target.fragment
            or target.path not in ("", "/") or target.port is None):
        raise ValueError("Use an explicit isolated origin: http://127.0.0.1:<port>")
    port_number(target.port)
    return value.rstrip("/")
