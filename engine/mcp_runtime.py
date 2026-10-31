"""Per-execution Hermes MCP overlay, stored under the engine's data directory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import tempfile

import yaml

from . import config


def cleanup_overlay(temporary):
    """Cleanup failure must not prevent execution state from being finalized."""
    if temporary is not None:
        try:
            temporary.cleanup()
        except OSError:
            logging.getLogger(__name__).exception("Cannot remove MCP runtime directory %s", temporary.name)


def hermes_overlay(server: dict, env: dict, *, profile_home: Path | None = None,
                   name: str = 'task_queue') -> tempfile.TemporaryDirectory:
    """Create an isolated managed overlay while preserving inherited managed settings."""
    parent = config.data_dir() / "runtime" / "mcp"
    parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="hermes-", dir=parent)
    directory = Path(temporary.name)
    inherited = env.get("HERMES_MANAGED_DIR", "").strip()
    source = Path(inherited) if inherited else Path("/etc/hermes")
    try:
        # Hermes merges mappings by leaf. A saved HTTP entry cannot become stdio
        # while its URL key remains present; reject that conflict before launching.
        if profile_home is not None and (profile_home / "config.yaml").is_file():
            saved = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8")) or {}
            existing = (saved.get("mcp_servers") or {}).get(name) if isinstance(saved, dict) else None
            if isinstance(existing, dict) and "url" in existing:
                raise ValueError(f"Remove the saved HTTP {name} MCP entry before using the engine's stdio connection")
        settings = {}
        source_config = source / "config.yaml"
        if source_config.is_file():
            settings = yaml.safe_load(source_config.read_text(encoding="utf-8"))
            if settings is None:
                settings = {}
            if not isinstance(settings, dict):
                raise ValueError("Hermes managed config must contain a mapping")
        servers = settings.setdefault("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("Hermes managed mcp_servers must contain a mapping")
        servers[name] = {**server, "enabled": True, "lazy": False,
                                 "cwd": str(Path(server["args"][0]).resolve().parent.parent),
                                 "connect_timeout": 60}
        # JSON is valid YAML and preserves Windows paths and multiline environment values.
        (directory / "config.yaml").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        if (source / ".env").is_file():
            shutil.copy2(source / ".env", directory / ".env")
        env["HERMES_MANAGED_DIR"] = str(directory.resolve())
        return temporary
    except BaseException:
        temporary.cleanup()
        raise
