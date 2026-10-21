"""Edit Hermes profile files; running agents and the spawn path are untouched."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid

from fastapi import HTTPException
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, ScalarNode

_FILES = ("profile.yaml", "SOUL.md", ".env", "config.yaml")
_LOCK = threading.RLock()
_MAX_BYTES = 2 * 1024 * 1024


def _root() -> Path:
    # Match hermes_constants.get_default_hermes_root (including named-profile homes).
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        native = base / "hermes"
    else:
        native = Path.home() / ".hermes"
    configured = os.environ.get("HERMES_HOME", "").strip()
    if not configured:
        return native.resolve()
    home = Path(configured).resolve()
    if home.is_relative_to(native.resolve()):
        return native.resolve()
    return home.parent.parent if home.parent.name == "profiles" else home


def _directory(profile: str) -> Path:
    from .db import is_builtin_user
    if is_builtin_user(profile) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile):
        raise HTTPException(400, "无效的 profile")
    root = _root()
    from . import bot_identity
    try:
        folder = bot_identity.hermes_home(profile, root).resolve()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if folder.resolve() != folder or not folder.is_dir():
        raise HTTPException(404, "Profile 目录不存在或路径无效")
    return folder


def _snapshot(folder: Path, names=_FILES) -> dict[str, bytes | None]:
    result = {}
    for name in names:
        path = folder / name
        if path.is_symlink() or path.resolve() != path:
            raise HTTPException(400, f"{name} 必须是 profile 内的普通文件")
        if path.exists() and (not path.is_file() or path.stat().st_size > _MAX_BYTES):
            raise HTTPException(400, f"{name} 不是普通文件或超过 2 MB")
        result[name] = path.read_bytes() if path.exists() else None
    return result


def _revision(files: dict[str, bytes | None]) -> str:
    digest = hashlib.sha256()
    for name, content in files.items():
        digest.update(name.encode())
        digest.update(b"missing" if content is None else hashlib.sha256(content).digest())
    return digest.hexdigest()


def _text(content: bytes | None) -> str:
    return content.decode("utf-8-sig") if content is not None else ""


def _yaml(content: bytes | None) -> tuple[YAML, CommentedMap]:
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    parsed = parser.load(_text(content))
    if parsed is None:
        parsed = CommentedMap()
    if not isinstance(parsed, CommentedMap):
        raise HTTPException(400, "YAML 顶层必须是键值映射")
    return parser, parsed


def _dump(parser: YAML, value: CommentedMap, original: bytes | None) -> bytes:
    stream = io.StringIO()
    parser.dump(value, stream)
    text = stream.getvalue()
    if original and b"\r\n" in original:
        text = text.replace("\n", "\r\n")
    return (b"\xef\xbb\xbf" if original and original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")


def _view(files: dict[str, bytes | None]) -> dict:
    _, meta = _yaml(files["profile.yaml"])
    _, config = _yaml(files["config.yaml"])
    model = config.get("model") or {}
    if not isinstance(model, (str, dict)):
        raise HTTPException(400, "config.yaml 的 model 格式无效")
    if not isinstance(config.get('agent') or {}, dict):
        raise HTTPException(400, 'config.yaml 的 agent 必须是键值映射')
    return {
        "description": str(meta.get("description") or ""),
        "soul": _text(files["SOUL.md"]),
        "env": _text(files[".env"]),
        "model": model if isinstance(model, str) else str(model.get("default") or model.get("model") or ""),
        "provider": "" if isinstance(model, str) else str(model.get("provider") or ""),
        "reasoning_effort": ('none' if (config.get('agent') or {}).get('reasoning_effort') is False
                             else str((config.get('agent') or {}).get('reasoning_effort') or '')),
        "revision": _revision(files),
    }


def _stage(folder: Path, content: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".group-edit-", dir=folder)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _update_model(original: bytes | None, values: dict) -> bytes:
    """Update scalar spans or insert absent fields, preserving surrounding bytes."""
    text = _text(original)
    parser = YAML()
    root = parser.compose(text)
    error = "模型配置需要普通 YAML 块映射和独立单行值；原文件保持完整"
    if root is not None and (not isinstance(root, MappingNode) or root.flow_style):
        raise HTTPException(400, error)
    fields = (("default", "model"), ("provider", "provider"))
    replacements = {}
    for key_name, field in fields:
        value = values[field].strip()
        if any(char in value for char in ("\n", "\r", "\0")):
            raise HTTPException(400, "模型和供应商必须是单行文本")
        replacements[key_name] = json.dumps(value, ensure_ascii=False)
    eol = '\r\n' if '\r\n' in text else '\n'
    model_pair = next(((key, value) for key, value in root.value if key.value == "model"), None) if root else None
    edits = []
    if model_pair is None:
        # Insert before an explicit YAML document end, or append at EOF.
        pos = root.end_mark.index if root else len(text)
        prefix = eol if pos and text[pos - 1] != '\n' else ''
        block = 'model:' + eol + ''.join(f'  {key}: {replacements[key]}' + eol for key, _ in fields)
        edits.append((pos, pos, prefix + block))
    else:
        model_key, model = model_pair
        if (not isinstance(model, MappingNode) or model.flow_style
                or model.start_mark.index < model_key.end_mark.index
                or text[model.start_mark.index:model.start_mark.index + 1] in ("&", "*", "!")):
            raise HTTPException(400, error)
        missing = []
        for key_name, field in fields:
            pair = next(((key, value) for key, value in model.value if key.value == key_name), None)
            if pair is None:
                missing.append(key_name)
                continue
            key, node = pair
            if (not isinstance(node, ScalarNode) or node.style in ("|", ">")
                    or key.start_mark.line != node.start_mark.line
                    or node.start_mark.line != node.end_mark.line):
                raise HTTPException(400, error)
            start, end = node.start_mark.index, node.end_mark.index
            # Shared aliases, anchors and tags must retain their original meaning.
            if text[start:end].startswith(("&", "*", "!")) or start < key.end_mark.index:
                raise HTTPException(400, error)
            if str(node.value or "") != values[field].strip():
                edits.append((start, end, replacements[key_name]))
        if missing:
            first = model.value[0][0].start_mark
            pos = text.rfind('\n', 0, first.index) + 1
            block = ''.join(' ' * first.column + key + ': ' + replacements[key] + eol for key in missing)
            edits.append((pos, pos, block))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    # Validate the resulting YAML before staging any writes.
    parsed = YAML().load(text)
    if any(parsed["model"][key] != values[field].strip()
           for key, field in (("default", "model"), ("provider", "provider"))):
        raise HTTPException(400, "模型配置回读校验失败")
    bom = b"\xef\xbb\xbf" if original and original.startswith(b"\xef\xbb\xbf") else b""
    return bom + text.encode("utf-8")


def _update_reasoning(original: bytes | None, effort: str) -> bytes:
    if effort not in ('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise HTTPException(400, '不支持的推理强度')
    text = _text(original)
    root = YAML().compose(text)
    if root is not None and (not isinstance(root, MappingNode) or root.flow_style):
        raise HTTPException(400, '推理配置需要 YAML 块映射')
    eol = '\r\n' if '\r\n' in text else '\n'
    value = json.dumps(effort)
    pair = next(((k, v) for k, v in root.value if k.value == 'agent'), None) if root else None
    if pair is None:
        text += (eol if text and not text.endswith('\n') else '') + 'agent:' + eol + '  reasoning_effort: ' + value + eol
    else:
        key, node = pair
        if (not isinstance(node, MappingNode) or node.flow_style
                or text[node.start_mark.index:node.start_mark.index+1] in ('&', '*', '!')):
            raise HTTPException(400, 'agent 配置需要普通 YAML 块映射')
        field = next(((k, v) for k, v in node.value if k.value == 'reasoning_effort'), None)
        if field:
            k, v = field
            if (not isinstance(v, ScalarNode) or v.style in ('|', '>')
                    or k.start_mark.line != v.end_mark.line or v.start_mark.index < k.end_mark.index
                    or text[v.start_mark.index:v.start_mark.index+1] in ('&', '*', '!')):
                raise HTTPException(400, 'reasoning_effort 需要独立单行值')
            text = text[:v.start_mark.index] + value + text[v.end_mark.index:]
        else:
            pos = text.find('\n', key.end_mark.index)
            if pos < 0:
                raise HTTPException(400, 'agent 配置格式无效')
            indent = node.value[0][0].start_mark.column if node.value else 2
            text = text[:pos+1] + ' ' * indent + 'reasoning_effort: ' + value + eol + text[pos+1:]
    if YAML().load(text)['agent']['reasoning_effort'] != effort:
        raise HTTPException(400, '推理配置回读校验失败')
    return (b'\xef\xbb\xbf' if original and original.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')


def _backup(folder: Path, files: dict[str, bytes | None]) -> Path:
    """Persist changed files' originals before replacement; retain past backups."""
    parent = folder / ".group-engine-backups"
    if parent.is_symlink() or parent.resolve() != parent:
        raise HTTPException(400, "备份目录路径无效，原配置保持完整")
    parent.mkdir(mode=0o700, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    backup = parent / name
    backup.mkdir(mode=0o700)
    for name, content in files.items():
        if content is not None:
            staged = _stage(backup, content)
            try:
                os.replace(staged, backup / name)
            finally:
                staged.unlink(missing_ok=True)
    (backup / "manifest.json").write_text(json.dumps({
        "revision": _revision(files),
        "changed_files": list(files),
        "missing_files": [name for name, content in files.items() if content is None],
    }, indent=2), encoding="utf-8")
    # Verify durable backup bytes before granting the caller permission to replace originals.
    for name, content in files.items():
        if content is not None and (backup / name).read_bytes() != content:
            raise OSError("backup verification failed")
    return backup


def _commit(folder: Path, before: dict, after: dict) -> str | None:
    staged, replaced = {}, []
    backup = None
    recovery_failed = False
    try:
        for name, content in after.items():
            if content == before[name]:
                continue
            staged[name] = _stage(folder, content)
        if _snapshot(folder, before) != before:
            raise HTTPException(409, "配置已被其他操作修改，请重新打开编辑窗口")
        if staged:
            backup = _backup(folder, {name: before[name] for name in staged})
        if _snapshot(folder, before) != before:
            raise HTTPException(409, "配置已被其他操作修改，请重新打开编辑窗口")
        for name, path in staged.items():
            os.replace(path, folder / name)
            replaced.append(name)
    except Exception:
        for name in reversed(replaced):
            try:
                if before[name] is not None:
                    restored = _stage(folder, before[name])
                    try:
                        os.replace(restored, folder / name)
                    finally:
                        restored.unlink(missing_ok=True)
                else:
                    (folder / name).unlink(missing_ok=True)
            except OSError:
                recovery_failed = True
        if recovery_failed:
            raise HTTPException(500, "保存及恢复失败，原文件备份保留在 profile 目录的 .group-engine-backups 中") from None
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
    return str(backup.relative_to(folder)) if backup else None


def read(profile: str) -> dict:
    from . import codex_profile_config
    bot = codex_profile_config.resolve(profile)
    if bot:
        return codex_profile_config.read(bot)
    try:
        with _LOCK:
            return _view(_snapshot(_directory(profile)))
    except (UnicodeError, YAMLError):
        raise HTTPException(400, "Profile 文件必须是有效 UTF-8，且 YAML 格式正确") from None
    except OSError:
        raise HTTPException(500, "读取 profile 文件失败，请检查目录权限") from None


def save(profile: str, values: dict) -> dict:
    from . import codex_profile_config
    bot = codex_profile_config.resolve(profile)
    if bot:
        return codex_profile_config.save(bot, values)
    try:
        with _LOCK:
            folder = _directory(profile)
            before = _snapshot(folder)
            current = _view(before)
            if values["revision"] != current["revision"]:
                raise HTTPException(409, "配置已被其他操作修改，请重新打开编辑窗口")
            after = dict(before)
            if values["description"] != current["description"]:
                parser, meta = _yaml(before["profile.yaml"])
                meta["description"] = values["description"]
                meta["description_auto"] = False
                after["profile.yaml"] = _dump(parser, meta, before["profile.yaml"])
            for field, name in (("soul", "SOUL.md"), ("env", ".env")):
                # Browsers normalize textarea CRLF. Preserve untouched bytes and the file's EOL style.
                value = values[field].replace("\r\n", "\n")
                if value != current[field].replace("\r\n", "\n"):
                    if before[name] and b"\r\n" in before[name]:
                        value = value.replace("\n", "\r\n")
                    after[name] = value.encode("utf-8")
            if any(values[k] != current[k] for k in ("model", "provider")):
                after["config.yaml"] = _update_model(before["config.yaml"], values)
            effort = values.get('reasoning_effort')
            if effort is not None and effort != current['reasoning_effort']:
                after['config.yaml'] = _update_reasoning(after['config.yaml'], effort)
            if any(content is not None and len(content) > _MAX_BYTES for content in after.values()):
                raise HTTPException(400, "单个配置文件不能超过 2 MB")
            backup = _commit(folder, before, after)
            return {"saved": True, "backup": backup}
    except (UnicodeError, YAMLError):
        raise HTTPException(400, "Profile 文件必须是有效 UTF-8，且 YAML 格式正确") from None
    except OSError:
        raise HTTPException(500, "保存失败，原配置已保留，请检查目录权限") from None
