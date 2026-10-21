"""Persistent engine-owned workspace resources, shared by directory identity."""
import hashlib
import errno
import ipaddress
import json
import socket
import uuid
from pathlib import Path

from fastapi import HTTPException
from . import db, project_workspaces as ws

RULE = ('使用“当前项目”JSON 中引擎分配的工作区资源。开发或修改项目运行入口时，根据项目实际运行方式，'
        '通过启动参数、环境变量或配置文件接入分配的端口、监听地址及数据目录，保持各工作区独立配置。'
        '已有项目首次使用这些资源时，先检查并适配现有运行配置。运行入口按项目需要实现；'
        '脚本、exe 或其他运行方式均按其实际支持的接口接入。'
        '启动服务和浏览器时显式使用对应配置，启动后核验实际端口、监听地址及数据目录与分配结果一致，'
        '并将验证结果纳入开发交付记录。无法接入时报告具体原因；'
        '资源冲突时停止相关启动并请管理员通过工作区配置协调调整；'
        '执行结束释放本次启动且无需继续运行的进程和资源。')


def identity(project, path):
    return 'ws_' + hashlib.sha256((project + '\0' + ws.key(path)).encode()).hexdigest()[:24]


def ports(config):
    return [s['port'] for s in config['services'].values()] + ([config['browser']['port']] if config['browser'] else [])


def directories(config):
    return [config['test_data_directory']] + ([config['browser']['data_directory']] if config['browser'] else [])


def records(conn):
    return conn.execute('SELECT r.* FROM workspace_resources r JOIN projects p ON p.name=r.project WHERE p.removed=0').fetchall()


def available(port):
    for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
        try:
            with socket.socket(family) as sock:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind((address, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL):
                continue
            return False
    return True


def defaults(root, slot, used):
    def allocate():
        for port in range(18000, 48000):
            if port not in used and available(port):
                used.add(port)
                return port
        raise HTTPException(409, '可分配端口不足，请释放资源后重试')
    base = Path(root).parent / '.bot-group-resources' / hashlib.sha256(ws.key(root).encode()).hexdigest()[:16] / slot
    return {'services': {name: {'port': allocate(), 'host': '127.0.0.1'} for name in ('frontend', 'backend')},
            'browser': {'port': allocate(), 'host': '127.0.0.1', 'data_directory': str(base / 'browser')},
            'test_data_directory': str(base / 'test-data')}


def normalize(config):
    if not isinstance(config, dict) or set(config) != {'services', 'browser', 'test_data_directory'}:
        raise HTTPException(400, '工作区资源需要 services、browser 和 test_data_directory')
    def endpoint(value, browser=False):
        fields = {'port', 'host', 'data_directory'} if browser else {'port', 'host'}
        if not isinstance(value, dict) or set(value) != fields:
            raise HTTPException(400, '端口配置字段无效')
        if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
            raise HTTPException(400, '端口须为 1024–65535 的整数')
        if not isinstance(value['host'], str):
            raise HTTPException(400, '监听地址须为 IP 地址文本')
        try:
            host = str(ipaddress.ip_address(value['host']))
        except (ValueError, TypeError):
            raise HTTPException(400, '监听地址须为有效 IP 地址')
        result = {'port': value['port'], 'host': host}
        if browser:
            result['data_directory'] = ws.canonical(value['data_directory'])
        return result
    services = config['services']
    if not isinstance(services, dict) or len(services) > 32:
        raise HTTPException(400, '服务配置最多 32 项')
    if any(not isinstance(k, str) or not k.strip() or len(k) > 80 or any(ord(c) < 32 for c in k) for k in services):
        raise HTTPException(400, '服务名称须为有效单行文本')
    return {'services': {k: endpoint(services[k]) for k in sorted(services)},
            'browser': endpoint(config['browser'], True) if config['browser'] is not None else None,
            'test_data_directory': ws.canonical(config['test_data_directory'])}


def validate(config, others, old=None):
    used = [p for c in others for p in ports(c)]
    own = ports(config)
    if len(set(own)) != len(own) or set(own).intersection(used):
        raise HTTPException(409, '端口与其他工作区或本工作区服务重复，请修改端口')
    for port in own:
        if port not in ports(old or {'services': {}, 'browser': None}) and not available(port):
            raise HTTPException(409, f'端口 {port} 已被本机进程占用，请修改端口')
    paths = [Path(ws.key(p)) for p in directories(config)]
    other_paths = [Path(ws.key(p)) for c in others for p in directories(c)]
    for index, path in enumerate(paths):
        for other in other_paths + paths[:index]:
            if path == other or path in other.parents or other in path.parents:
                raise HTTPException(409, '浏览器及测试数据目录必须相互隔离，请修改目录')


def plan(conn, body):
    supplied = body.get('workspace_resources', [])
    if not isinstance(supplied, list) or any(not isinstance(r, dict) or set(r) != {'key', 'config'} for r in supplied):
        raise HTTPException(400, '工作区资源方案格式无效')
    drafts = {r['key']: r['config'] for r in supplied if isinstance(r['key'], str)}
    if len(drafts) != len(supplied):
        raise HTTPException(400, '工作区资源标识重复或无效')
    slots = ['shared'] if body.get('directory_mode', 'shared') == 'shared' else [f"bot-{m['bot_id']}" for m in body.get('members', [])]
    if set(drafts) - set(slots):
        raise HTTPException(409, '成员或协作模式已变化，请重新确认工作区资源')
    others = [json.loads(r['config']) for r in records(conn)]
    used = {p for c in others for p in ports(c)}
    for draft in drafts.values():
        used.update(ports(normalize(draft)))
    result = []
    for slot in slots:
        config = normalize(drafts[slot] if slot in drafts else defaults(body['root'], slot + '-' + uuid.uuid4().hex[:12], used))
        validate(config, others)
        others.append(config)
        result.append({'key': slot, 'config': config})
    return result


def save(conn, project, path, config, revision=None):
    path = ws.key(path)
    row = conn.execute('SELECT * FROM workspace_resources WHERE project=? AND path=?', (project, path)).fetchone()
    if revision is not None and revision != (row['revision'] if row else 0):
        raise HTTPException(409, '工作区配置已被更新，请重新打开后修改')
    config = normalize(config)
    roots = [Path(ws.key(r['root'])) for r in conn.execute('SELECT root FROM projects WHERE removed=0')]
    roots += [Path(ws.key(r['path'])) for r in conn.execute('SELECT path FROM project_workspaces')]
    for directory in directories(config):
        target = Path(ws.key(directory))
        if target.parent == target or any(target == root or target in root.parents for root in roots):
            raise HTTPException(400, '数据目录须为专用子目录，不能使用磁盘根目录或项目目录本身')
    validate(config, [json.loads(r['config']) for r in records(conn) if (r['project'], r['path']) != (project, path)],
             json.loads(row['config']) if row else None)
    conn.execute('INSERT INTO workspace_resources(project,path,workspace_id,config,revision) VALUES(?,?,?,?,1) '
                 'ON CONFLICT(project,path) DO UPDATE SET config=excluded.config,revision=workspace_resources.revision+1',
                 (project, path, identity(project, path), json.dumps(config, ensure_ascii=False)))
    return config


def get(conn, project, path):
    row = conn.execute('SELECT * FROM workspace_resources WHERE project=? AND path=?', (project, ws.key(path))).fetchone()
    if row:
        return {'workspace_id': row['workspace_id'], 'revision': row['revision'], 'config': json.loads(row['config'])}
    used = {p for r in records(conn) for p in ports(json.loads(r['config']))}
    return {'workspace_id': identity(project, path), 'revision': 0,
            'config': normalize(defaults(ws.project(conn, project)['root'], identity(project, path), used))}


def member(project, profile, body=None, actor=''):
    with ws.LOCK:
        conn = db.get_connection()
        try:
            with conn:
                if body is not None:
                    conn.execute('BEGIN IMMEDIATE')
                p = ws.project(conn, project)
                if p.get('removed') or not conn.execute('SELECT 1 FROM project_members WHERE project=? AND profile=?', (project, profile)).fetchone():
                    raise HTTPException(404, '项目成员不存在')
                path = ws.selected_path(conn, p, profile)
                sharing = [r['profile'] for r in conn.execute('SELECT profile FROM project_members WHERE project=?', (project,))
                           if ws.key(ws.selected_path(conn, p, r['profile'])) == ws.key(path)]
                if body is not None:
                    for affected in sharing:
                        ws.ensure_idle(project, affected)
                    ws.launch_directory(conn, project, profile)
                    if type(body.get('revision')) is not int:
                        raise HTTPException(400, '请提供配置版本 revision')
                    save(conn, project, path, body.get('config'), body['revision'])
                    ws.audit(conn, project, actor, 'workspace.resources', {'path': path, 'profile': profile})
                return {**get(conn, project, path), 'path': ws.canonical(path), 'members': sharing}
        finally:
            conn.close()


def prompt_config(conn, project, path):
    result = get(conn, project, path)
    if not result['revision']:
        conn.execute('BEGIN IMMEDIATE')
        result = get(conn, project, path)
        if not result['revision']:
            save(conn, project, path, result['config'], 0)
        conn.commit()
    c = result['config']
    return {'工作区 ID': result['workspace_id'], '服务': {k: {'端口': v['port'], '监听地址': v['host']} for k, v in c['services'].items()},
            '浏览器': {'调试端口': c['browser']['port'], '监听地址': c['browser']['host'], '用户数据目录': c['browser']['data_directory']} if c['browser'] else None,
            '测试数据目录': c['test_data_directory']}
