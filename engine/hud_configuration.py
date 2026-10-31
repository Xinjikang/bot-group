"""Project-scoped settings operations using the existing project business APIs."""
import copy
import secrets
import hashlib
from pathlib import Path

from fastapi import HTTPException
from . import hud_tools, project_hud as hud

SECTIONS = ('basic', 'roles', 'members', 'permissions', 'profiles')
OPERATIONS = {'basic': ['update'], 'roles': ['create', 'rename', 'remove'],
              'members': ['set_active', 'assign_role'], 'permissions': ['set_role_permissions'], 'profiles': ['create']}


def read(item, section):
    from . import api
    if section not in SECTIONS:
        raise HTTPException(400, '请选择 basic、roles、members、permissions 或 profiles')
    if section == 'profiles':
        values = _profile_options(item)
    elif section == 'basic':
        with hud.connection() as conn:
            values = hud_tools.configuration(conn, item)
    elif item['project']:
        project = item['project']
        if section == 'permissions':
            values = api.get_project_permissions(project, identity=('project-assistant', 'user'))
        else:
            roles = api.get_project_roles(project)['roles']
            members = api.get_members(project=project, enrolled_only=True)['members']
            values = {'roles': roles, 'members': members}
    else:
        wizard = item['state'].get('draft', {}).get('wizard', {})
        values = {'roles': wizard.get('roles', []), 'members': wizard.get('members', [])}
        if section == 'permissions':
            from .project_permissions import PERMISSIONS
            values['permissions'] = [{'key': key, 'label': label} for key, label in PERMISSIONS.items()]
    operations = ['assign_role'] if not item['project'] and section == 'members' else OPERATIONS[section]
    return {'configuration': values, 'config_version': hud_tools.version(values),
            'operations': operations}


def _validate(changes, keys):
    if not isinstance(changes, dict) or set(changes) != set(keys):
        raise HTTPException(400, '本操作需要字段：' + ', '.join(keys))


def _role(roles, role_id):
    if type(role_id) is not int:
        raise HTTPException(400, 'role_id 必须为项目角色 ID')
    role = next((role for role in roles if role['id'] == role_id), None)
    if role is None:
        raise HTTPException(404, '当前项目中未找到该角色')
    return role


def execute(sid, token, action='read', section='all', operation='update', changes=None, expected_version=''):
    from . import api
    with hud._LOCK:
        with hud.connection() as conn:
            item = hud_tools.authorize(token, sid, conn)
        if action == 'read':
            selected = SECTIONS if section == 'all' else (section,)
            return {'project': item['project'], 'sections': {key: read(item, key) for key in selected},
                    'file_editing': 'AGENTS.md and SOUL use the native file tools'}
        if action != 'update' or section not in SECTIONS or operation not in OPERATIONS[section]:
            raise HTTPException(400, '请选择当前配置区域支持的操作')
        if not item['state'].get('user_request'):
            raise HTTPException(409, '配置修改需要当前用户请求')
        current = read(item, section)
        if not expected_version or expected_version != current['config_version']:
            raise HTTPException(409, {'message': '配置已变化，请重新读取当前配置', **current})
        if section == 'basic':
            return hud_tools.update(sid, token, changes, expected_version)
        if section == 'profiles':
            result = _create_profile(item, changes)
            _record(item, section, operation, {'profile': result['profile'], 'member_profile': result.get('member_profile')})
            return {'saved': True, 'section': section, **result, **read(item, section)}
        if not item['project']:
            return _draft(item, section, operation, changes, current)
        project, values = item['project'], current['configuration']
        identity = ('project-assistant', 'user')
        if section == 'roles':
            if operation == 'create':
                _validate(changes, ['name'])
                api.post_project_role(project, changes, identity=identity)
            else:
                _validate(changes, ['role_id', 'name'] if operation == 'rename' else ['role_id'])
                role = _role(values['roles'], changes['role_id'])
                if operation == 'rename':
                    api.patch_project_role(project, role['id'], {'name': changes['name']}, identity=identity)
                else:
                    api.delete_project_role(project, role['id'], identity=identity)
        elif section == 'members':
            _validate(changes, ['profile', 'active'] if operation == 'set_active' else ['profile', 'role_id'])
            profile = changes['profile']
            if not isinstance(profile, str) or not any(m['profile'] == profile for m in values['members']):
                raise HTTPException(404, '当前项目中未找到该成员')
            if operation == 'set_active':
                if type(changes['active']) is not bool:
                    raise HTTPException(400, 'active 必须为布尔值')
                api.set_member_active(profile, {'project': project, 'active': changes['active']}, identity=identity)
            else:
                role = _role(values['roles'], changes['role_id']) if changes['role_id'] is not None else None
                api.set_project_member_role(project, profile, {'role': role['name'] if role else ''}, identity=identity)
        else:
            _validate(changes, ['role_id', 'permissions'])
            role = _role(values['roles'], changes['role_id'])
            allowed = {p['key'] for p in values['permissions']}
            permissions = changes['permissions']
            if not isinstance(permissions, list) or any(not isinstance(p, str) or p not in allowed for p in permissions):
                raise HTTPException(400, '请使用当前配置中列出的权限 key')
            target = f'role:{role["id"]}'
            grants = {key: [p for p in targets if p != target] for key, targets in values['grants'].items()}
            for permission in permissions:
                grants[permission].append(target)
            api.put_project_permissions(project, {'revision': values['revision'], 'grants': grants}, identity=identity)
        result = read(item, section)
        _record(item, section, operation, changes)
        return {'saved': True, 'section': section, **result}


def _record(item, section, operation, changes):
    with hud.connection() as conn:
        current = hud.row(conn, item['id'])
        current['state']['settings_update'] = {'id': secrets.token_hex(12), 'section': section,
                                                'operation': operation, 'changes': changes}
        hud.add_entry(conn, current, 'system', {'text': '已更新项目配置', 'section': section, 'operation': operation})
        conn.execute('UPDATE hud_sessions SET state=?,updated=? WHERE id=?',
                     (hud.encode(current['state']), hud.time.time(), item['id']))
    hud.events.bus.publish('hud.changed', {'session': item['id'], 'project': item['project']})
    hud.events.bus.publish('project.updated', {'project': item['project']})


def _draft(item, section, operation, changes, current):
    """Update wizard lists while retaining final creation validation in the wizard."""
    values = copy.deepcopy(current['configuration'])
    roles, members = values['roles'], values['members']
    if section == 'members' and operation == 'set_active':
        raise HTTPException(409, '成员启停在项目创建后生效；创建草稿通过成员选择管理入组范围')
    if section == 'roles' and operation == 'create':
        _validate(changes, ['name'])
        name = changes['name']
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise HTTPException(400, '角色名必须为非空单行文本')
        if any(r['name'] == name.strip() for r in roles):
            raise HTTPException(409, '已存在同名角色')
        roles.append({'name': name.strip(), 'permissions': []})
    else:
        # Wizard roles have names before their stable database IDs are assigned.
        _validate(changes, ['role', 'name'] if section == 'roles' and operation == 'rename' else
                  ['role'] if section == 'roles' else ['bot_id', 'role'] if operation == 'assign_role' else ['role', 'permissions'])
        role = next((r for r in roles if r['name'] == changes.get('role')), None)
        if section == 'roles':
            if role is None:
                raise HTTPException(404, '草稿中未找到该角色')
            if operation == 'rename':
                name = changes['name']
                if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
                    raise HTTPException(400, '角色名必须为非空单行文本')
                if any(r is not role and r['name'] == name.strip() for r in roles):
                    raise HTTPException(409, '已存在同名角色')
                for member in members:
                    if member.get('role') == role['name']:
                        member['role'] = name.strip()
                role['name'] = name.strip()
            else:
                roles.remove(role)
                for member in members:
                    if member.get('role') == role['name']:
                        member['role'] = ''
        elif section == 'members':
            member = next((m for m in members if m.get('bot_id') == changes['bot_id']), None)
            if member is None:
                with hud.connection() as conn:
                    registered = conn.execute('SELECT 1 FROM bots WHERE id=?', (changes['bot_id'],)).fetchone()
                if type(changes['bot_id']) is not int or not registered:
                    raise HTTPException(404, '未找到可选择的 profile')
                member = {'bot_id': changes['bot_id'], 'role': '', 'permissions': []}
                members.append(member)
            if changes['role'] and role is None:
                raise HTTPException(404, '草稿中未找到该角色')
            member['role'] = changes['role']
        else:
            from .project_permissions import PERMISSIONS
            if role is None or not isinstance(changes['permissions'], list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in changes['permissions']):
                raise HTTPException(400, '请提供有效角色与权限 key')
            role['permissions'] = changes['permissions']
    before = copy.deepcopy(item['state']['draft'].get('wizard', {}))
    wizard = item['state']['draft'].setdefault('wizard', {})
    wizard.update(roles=roles, members=members)
    item['state']['config_update'] = {'id': secrets.token_hex(12), 'changes': {'wizard': wizard},
                                     'before': {'wizard': before}, 'draft': True}
    with hud.connection() as conn:
        conn.execute('UPDATE hud_sessions SET state=? WHERE id=?', (hud.encode(item['state']), item['id']))
    _record(item, section, operation, changes)
    return {'saved': True, 'section': section, **read(item, section)}


def _profile_options(item):
    from . import project_templates, discovery
    template_id = (project_templates.reference(item['project']).get('template') or {}).get('id', 'blank') if item['project'] else item['state'].get('draft', {}).get('template', 'blank')
    template = project_templates.load(template_id) if template_id != 'blank' else {'roles': []}
    roles = [{key: role.get(key) for key in ('id', 'name', 'soul_reference', 'permissions')} for role in template['roles']]
    with hud.connection() as conn:
        bots = [dict(r) for r in conn.execute("SELECT id,host_type,profile,config_root,display_name FROM bots WHERE profile<>'user' ORDER BY id")]
    return {'template': template_id, 'template_roles': roles, 'registered_profiles': bots,
            'template_root': str(project_templates.TEMPLATE_ROOT / template_id),
            'default_homes': {'hermes': str(discovery._hermes_root()), 'codex': str(discovery._codex_root())}}


def _create_profile(item, changes):
    from . import api
    from pydantic import ValidationError
    allowed = {'host_type', 'profile', 'display_name', 'model', 'source', 'instructions'}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise HTTPException(400, '创建 profile 参数无效')
    try:
        body = api.ProfileCreateBody(project=item['project'], **changes,
            request_id=hud_tools.version({'session': item['id'], 'changes': changes}))
    except ValidationError:
        raise HTTPException(400, '请提供有效的 profile、宿主类型和初始角色说明') from None
    result = api.post_create_profile(body, identity=('project-assistant', 'user'))
    from . import profile_config
    filename = 'SOUL.md' if body.host_type == 'hermes' else 'AGENTS.md'
    raw = profile_config._snapshot(Path(result['home']), [filename])[filename]
    verified = raw is not None and raw.decode('utf-8-sig').rstrip('\r\n') == body.instructions.rstrip('\r\n')
    if body.instructions and not verified:
        raise HTTPException(409, {'message': 'profile 已创建，初始说明文件核验失败，请检查实际文件', 'created': result})
    return {**result, 'instructions_file': str(Path(result['home']) / filename),
            'instructions_verified': verified, 'instructions_sha256': hashlib.sha256(raw or b'').hexdigest()}
