"""Hermes executable/source identity and session capability checks."""
from __future__ import annotations

import ast
import importlib
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import threading


class HermesCompatibilityError(RuntimeError):
    """Installed Hermes cannot provide the required session operations."""

    def __str__(self):
        return f'{type(self).__name__}: {super().__str__()}'


class HermesConfigurationError(HermesCompatibilityError):
    """The selected entry, source or loaded modules refer to conflicting installs."""


SESSION_METHODS = ('create_session', 'get_session', 'set_session_title',
                   'resolve_session_by_title', 'delete_session_if_empty',
                   'get_compression_tip', 'close')
_IMPORT_LOCK = threading.RLock()


def native_entry(entry: str) -> str:
    """Resolve pure Windows forwarding wrappers without invoking a command shell."""
    if sys.platform != 'win32':
        return entry
    path = Path(entry)
    if path.suffix.lower() not in ('.cmd', '.bat'):
        if path.suffix.lower() != '.exe':
            raise HermesConfigurationError('Hermes Windows 入口须为原生 .exe 或可解析的纯转发 .cmd/.bat')
        return entry
    raw = path.read_bytes()
    try:
        script = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        script = raw.decode('mbcs')
    commands = [line.strip() for line in script.splitlines()
                if line.strip() and line.strip().lower() not in ('@echo off', 'echo off')
                and not line.lstrip().lower().startswith(('rem ', '::'))]
    match = re.fullmatch(r'@?(?:"([^"\r\n]+\.exe)"|([^\s"]+\.exe))\s+%\*',
                         commands[0], re.IGNORECASE) if len(commands) == 1 else None
    if match is None:
        raise HermesConfigurationError(f'Hermes 包装入口无法安全解析：{path}；请将 HERMES_BIN 指向真实 hermes.exe')
    target = match.group(1) or match.group(2)
    target = re.sub(r'%~dp0', lambda _: str(path.resolve().parent) + os.sep, target, flags=re.IGNORECASE)
    if '%' in target:
        raise HermesConfigurationError(f'Hermes 包装入口含未解析变量：{path}；请设置原生 HERMES_BIN')
    native = Path(target)
    if not native.is_absolute():
        raise HermesConfigurationError(f'Hermes 包装目标须为绝对路径或 %~dp0 路径：{path}')
    if not native.is_file():
        raise FileNotFoundError(f'Hermes 包装目标不存在：{native}')
    logging.getLogger(__name__).info('Hermes native entry resolved from wrapper: %s -> %s', path, native)
    return str(native.resolve())


def executable() -> str:
    """Select explicit entry, configured home, PATH, then the native fallback."""
    explicit = os.environ.get('HERMES_BIN', '').strip()
    if explicit:
        return native_entry(shutil.which(explicit) or explicit)
    home = os.environ.get('HERMES_HOME', '').strip()
    if home:
        for name in (('hermes.exe', 'hermes.cmd', 'hermes.bat') if sys.platform == 'win32' else ('hermes',)):
            candidate = Path(home) / 'bin' / name
            if candidate.is_file():
                return native_entry(str(candidate))
    if sys.platform == 'win32':
        native = shutil.which('hermes.exe')
        if native:
            return native_entry(native)
    found = shutil.which('hermes')
    if found:
        return native_entry(found)
    for candidate in (
        Path.home() / 'hermes/hermes-agent/venv/Scripts/hermes.exe',
        Path.home() / 'hermes/hermes-agent/venv/Scripts/hermes',
    ):
        if candidate.is_file():
            return native_entry(str(candidate))
    raise FileNotFoundError('未找到原生 Hermes 入口；请设置 HERMES_BIN')


def source_directory(home: Path, entry: str | None = None) -> Path:
    """Resolve supported layouts near the actual executable and reject ambiguity."""
    entry_path = Path(entry or executable()).resolve()
    if not entry_path.is_file():
        raise HermesConfigurationError(f'Hermes 入口不存在：{entry_path}；请修正 HERMES_BIN')
    found = None
    # Bound the search to the entry's layout; filesystem roots are never candidates.
    for parent in list(entry_path.parents)[:4]:
        if parent == parent.parent:
            break
        candidates = [p.resolve() for p in (parent, parent / 'hermes-agent')
                      if (p / 'hermes_state.py').is_file() and (p / 'hermes_state_registry.py').is_file()]
        if len(set(candidates)) > 1:
            raise HermesConfigurationError(f'Hermes 入口附近存在多个源码目录：{entry_path}；请修正 HERMES_BIN')
        if candidates:
            found = candidates[0]
            break
    expected = (home / 'hermes-agent').resolve()
    if found is None:
        raise HermesConfigurationError(f'无法从 Hermes 入口定位会话源码：{entry_path}；请检查 HERMES_BIN 与 HERMES_HOME')
    if expected.is_dir() and found != expected:
        raise HermesConfigurationError(f'Hermes 入口源码 {found} 与 HERMES_HOME 源码 {expected} 冲突；请统一配置')
    return found


def inspect_source(source: Path) -> dict:
    """Read declared capabilities without importing source or opening a database."""
    trees = {}
    def tree(module):
        if module not in trees:
            trees[module] = ast.parse((source / (module + '.py')).read_text(encoding='utf-8-sig'))
        return trees[module]

    def methods(module, name, visited):
        key = (module, name)
        if key in visited:
            return set()
        visited.add(key)
        nodes = tree(module).body
        cls = next((n for n in nodes if isinstance(n, ast.ClassDef) and n.name == name), None)
        if cls is None:
            return set()
        result = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        imports = {alias.asname or alias.name: (n.module, alias.name)
                   for n in nodes if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('hermes_state')
                   for alias in n.names}
        for base in cls.bases:
            if isinstance(base, ast.Name):
                target = imports.get(base.id, (module, base.id))
                result.update(methods(*target, visited))
        return result

    try:
        available = methods('hermes_state', 'SessionDB', set())
        missing = [name for name in SESSION_METHODS if name not in available]
        if not any(isinstance(n, ast.FunctionDef) and n.name == 'acquire' for n in tree('hermes_state_registry').body):
            missing.append('hermes_state_registry.acquire')
        mode = 'native' if (source / 'hermes_state_ids.py').is_file() else 'legacy'
        if mode == 'native' and not any(isinstance(n, ast.FunctionDef) and n.name == 'new_session_id' for n in tree('hermes_state_ids').body):
            missing.append('hermes_state_ids.new_session_id')
        if missing:
            raise HermesCompatibilityError(f'Hermes 缺少会话接口 {", ".join(missing)}；请更新 Hermes；源码：{source}')
        version = ''
        version_file = source / 'hermes_cli/__init__.py'
        if version_file.is_file():
            for node in ast.parse(version_file.read_text(encoding='utf-8-sig')).body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        version = node.value.value
        return {'source': str(source), 'version': version, 'session_ids': mode, 'status': 'compatible', 'check': 'static'}
    except (OSError, SyntaxError) as exc:
        raise HermesCompatibilityError(f'Hermes 会话源码无法检查：{source}；请修复或更新 Hermes；{exc}') from exc


def prepare_source(source: Path) -> None:
    """Validate actual imported capabilities and prevent cross-install module reuse."""
    source = source.resolve()
    with _IMPORT_LOCK:
        for name, module in list(sys.modules.items()):
            if name == 'hermes_state' or name.startswith('hermes_state_'):
                origin = getattr(module, '__file__', None)
                if origin and Path(origin).resolve().parent != source:
                    raise HermesConfigurationError(f'Hermes 已加载另一安装的模块 {name}：{origin}；目标源码：{source}；请统一配置并重启引擎')
        if str(source) in sys.path:
            sys.path.remove(str(source))
        sys.path.insert(0, str(source))
        try:
            registry = importlib.import_module('hermes_state_registry')
            state = importlib.import_module('hermes_state')
            for module in (registry, state):
                if Path(module.__file__).resolve().parent != source:
                    raise HermesConfigurationError(f'Hermes 模块来源与目标源码 {source} 冲突；请统一配置并重启引擎')
            missing = [name for name in SESSION_METHODS if not callable(getattr(state.SessionDB, name, None))]
            if not callable(getattr(registry, 'acquire', None)):
                missing.append('hermes_state_registry.acquire')
            if missing:
                raise HermesCompatibilityError(f'Hermes 缺少会话接口 {", ".join(missing)}；请更新 Hermes；源码：{source}')
        except (ImportError, AttributeError, SyntaxError) as exc:
            raise HermesCompatibilityError(f'Hermes 会话接口加载失败：{exc}；请修复或更新 Hermes；源码：{source}') from exc


def new_session_id(source: Path) -> str:
    """Use native minting, or the legacy CLI's timestamp and six UUID hex digits."""
    if not (source / 'hermes_state_ids.py').is_file():
        from datetime import datetime
        import uuid
        return f'{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
    try:
        ids = importlib.import_module('hermes_state_ids')
        if Path(ids.__file__).resolve().parent != source.resolve():
            raise HermesConfigurationError(f'Hermes ID 模块与目标源码 {source} 冲突；请统一配置并重启引擎')
        mint = getattr(ids, 'new_session_id', None)
        if not callable(mint):
            raise HermesCompatibilityError(f'Hermes 缺少可调用的 new_session_id；请更新 Hermes；源码：{source}')
        return mint()
    except (ImportError, AttributeError, SyntaxError) as exc:
        raise HermesCompatibilityError(f'Hermes ID 接口加载失败：{exc}；请修复或更新 Hermes；源码：{source}') from exc


def diagnostic(home: Path) -> dict:
    """Installation report; failure affects Hermes readiness only."""
    entry = ''
    try:
        entry = executable()
        return {'home': str(home), 'bin': entry, **inspect_source(source_directory(home, entry))}
    except (HermesCompatibilityError, OSError) as exc:
        return {'home': str(home), 'bin': entry, 'status': 'unavailable', 'detail': str(exc)}
