"""Build independent headless profiles while reusing HOME account configuration."""
from __future__ import annotations

import json

from fastapi import HTTPException

from . import discovery, dsh_profile_config, profile_config as files


_INHERITED = {'agent-default-model', 'llm-deepseek', 'llm-pi-ai',
              'deepseek-account', 'credentials', 'authorization'}


def content(home, body):
    entry = discovery._dsh_entry()
    if not discovery._dsh_entry_available(entry):
        raise HTTPException(400, 'DSH 执行入口不可用，请检查宿主安装和 DSH_BIN 配置')
    raw = files._snapshot(home, ['cordis.patch.yml'])['cordis.patch.yml'] if home else None
    parser, source = dsh_profile_config._yaml(raw)
    # Keep runtime routing; UI services and source identity belong to the source profile.
    patch = [item for item in source if isinstance(item, dict) and item.get('id') in _INHERITED]
    if body['model']:
        model = next((item for item in patch if item['id'] == 'agent-default-model'), None)
        if model is None:
            model = {'id': 'agent-default-model', 'config': {}}
            patch.append(model)
        model.setdefault('config', {})['model'] = body['model']
    instructions = body['instructions'] or f"# {body['display_name'] or body['profile']}\n\n按当前项目的职责、权限和协作约定完成工作。\n"
    patch.append({'id': 'system-prompt', 'config': {'personaPrefix': instructions}})
    package = {'name': 'dsh-profile-' + body['profile'], 'private': True,
               'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless']}}}
    return {
        'package.json': json.dumps(package, ensure_ascii=False, indent=2).encode('utf-8'),
        'cordis.yml': b'[]\n',
        'cordis.patch.yml': files._dump(parser, patch, None),
    }, False
