"""Read provider metadata as data without importing or executing Hermes modules."""
import ast
from dataclasses import dataclass, field
from io import StringIO
import json
from pathlib import Path
import re

from dotenv import dotenv_values

_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass
class Catalog:
    variables: set[str] = field(default_factory=set)
    credentials: set[str] = field(default_factory=set)
    providers: dict[str, set[str]] = field(default_factory=dict)

    def add(self, provider, keys=(), urls=()):
        keys = {k for k in keys if isinstance(k, str) and _NAME.fullmatch(k)}
        urls = {k for k in urls if isinstance(k, str) and _NAME.fullmatch(k)}
        self.variables.update(keys | urls)
        self.credentials.update(keys)
        self.providers.setdefault(provider, set()).update(keys)


def _value(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        raise ValueError('Unsupported Hermes provider metadata; update the compatibility adapter') from None


def _assignment(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return node.value
    raise ValueError('Unsupported Hermes provider metadata; update the compatibility adapter')


def scan(root: Path, home: Path) -> Catalog:
    """Adapter for Hermes afc3b7c6f3: registry rows, overlays and categorized env metadata."""
    cli = root / 'hermes-agent' / 'hermes_cli'
    result = Catalog()
    rows = _assignment(cli / 'auth.py', '_REGISTRY_ROWS')
    if not isinstance(rows, (ast.Tuple, ast.List)):
        raise ValueError('Unsupported provider registry')
    for row in rows.elts:
        if isinstance(row, ast.Tuple) and len(row.elts) >= 4:
            result.add(_value(row.elts[0]), _value(row.elts[3]),
                       [_value(row.elts[4])] if len(row.elts) > 4 else [])
        elif isinstance(row, ast.Call) and isinstance(row.func, ast.Name) and row.func.id == 'ProviderConfig':
            fields = {k.arg: k.value for k in row.keywords}
            provider = _value(row.args[0]) if row.args else _value(fields['id'])
            result.add(provider, _value(fields['api_key_env_vars']) if 'api_key_env_vars' in fields else (),
                       [_value(fields['base_url_env_var'])] if 'base_url_env_var' in fields else ())
        else:
            raise ValueError('Unsupported provider registry row')
    overlays = _assignment(cli / 'providers.py', 'HERMES_OVERLAYS')
    metadata = _assignment(cli / 'config_defaults.py', 'OPTIONAL_ENV_VARS')
    if not isinstance(overlays, ast.Dict) or not isinstance(metadata, ast.Dict):
        raise ValueError('Unsupported provider metadata')
    for key, entry in zip(overlays.keys, overlays.values):
        if not isinstance(entry, ast.Call) or not isinstance(entry.func, ast.Name) or entry.func.id != 'HermesOverlay':
            raise ValueError('Unsupported provider overlay')
        fields = {k.arg: k.value for k in entry.keywords}
        result.add(_value(key), _value(fields['extra_env_vars']) if 'extra_env_vars' in fields else (),
                   [_value(fields['base_url_env_var'])] if 'base_url_env_var' in fields else ())
    for key, entry in zip(metadata.keys, metadata.values):
        if not isinstance(entry, ast.Call) or not isinstance(entry.func, ast.Name):
            raise ValueError('Unsupported environment metadata')
        category = next((_value(k.value) for k in entry.keywords if k.arg == 'category'), None)
        if entry.func.id in ('_prov', '_base_url') or category == 'provider':
            name = _value(key)
            # Categorized metadata also includes non-secret provider configuration.
            result.variables.add(name)
    # The optional local catalog extends built-in providers without network I/O.
    for path in dict.fromkeys((root / 'models_dev_cache.json', home / 'models_dev_cache.json')):
        if path.is_file():
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Invalid local provider catalog')
            for provider, entry in data.items():
                if isinstance(entry, dict) and isinstance(entry.get('env', []), list):
                    result.add(provider, entry.get('env', []))
    return result


def custom_blocks(config):
    """Explicit custom-provider and model bindings; no recursive search of unrelated settings."""
    model = config.get('model')
    if isinstance(model, dict):
        yield 'model', model
    providers = config.get('providers', {})
    if isinstance(providers, dict):
        yield from ((str(k), v) for k, v in providers.items() if isinstance(v, dict))
    legacy = config.get('custom_providers', [])
    if isinstance(legacy, list):
        yield from ((str(v.get('name', 'custom')), v) for v in legacy if isinstance(v, dict))
    aliases = config.get('model_aliases', {})
    if isinstance(aliases, dict):
        yield from ((str(k), v) for k, v in aliases.items() if isinstance(v, dict))


def copy_environment(root, home, config, raw):
    catalog = scan(Path(root), Path(home))
    blocks = list(custom_blocks(config))
    for name, block in blocks:
        key = block.get('key_env') or block.get('api_key_env')
        if key:
            if not isinstance(key, str) or not _NAME.fullmatch(key):
                raise ValueError('Invalid provider credential variable')
            catalog.add(name, [key])
    values = dotenv_values(stream=StringIO(raw), interpolate=False)
    copied = {key: value for key, value in values.items() if key in catalog.variables and value is not None}
    # Quoted dotenv values retain whitespace, hashes, equals signs and multiline secrets.
    env = ''.join(key + "='" + value.replace('\\', '\\\\').replace("'", "\\'") + "'\n"
                  for key, value in copied.items())
    model = config.get('model', {})
    provider = model.get('provider', '') if isinstance(model, dict) else ''
    selected = [(name, block) for name, block in blocks if name != 'model' and
                provider.removeprefix('custom:') in (name, str(block.get('provider_key', '')), str(block.get('name', '')).lower().replace(' ', '-'))] if provider else []
    selected_blocks = [model] if isinstance(model, dict) else []
    selected_blocks += [block for _, block in selected]
    expected = catalog.providers.get(provider, catalog.credentials if not provider else set())
    for name, block in selected:
        expected = expected | catalog.providers.get(name, set())
    if isinstance(model, dict) and (model.get('key_env') or model.get('api_key_env')):
        expected = {model.get('key_env') or model.get('api_key_env')}
    provisioned = any(copied.get(k, '').strip() for k in expected)
    provisioned = provisioned or any(block.get('api_key') for block in selected_blocks)
    return env.encode('utf-8'), bool(provisioned)
