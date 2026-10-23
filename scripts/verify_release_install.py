"""#215：显式指定分配资源的真实安装/启停/管理/版本升级与恢复验收。

调用方需要提供 Python >=3.11（安装），以及已安装 Playwright 的 Python（浏览器）。
仅创建 test-root 子树，服务目标固定本工作区回环 18042；无模型调用。
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import make_release


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--test-root', type=Path, required=True)
    p.add_argument('--browser-profile', type=Path, required=True)
    p.add_argument('--browser-python', required=True)
    p.add_argument('--install-python', required=True)
    p.add_argument('--browser-only', action='store_true', help='复用已验收的隔离安装，补验刷新与实际资源绑定')
    args = p.parse_args()
    base = args.test_root.resolve()
    if base == ROOT or ROOT in base.parents or 'test-data' not in base.parts:
        p.error('test-root 须为分配 test-data 下的独立目录')
    if base.exists() and not args.browser_only:
        p.error('test-root 已存在，请指定新的独立验收目录')
    if args.browser_only and not (base / '中文 安装目录' / 'VERSION').is_file():
        p.error('browser-only 需要本脚本已建立的独立安装')
    for port in (18042, 18043):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError(f'分配端口 {port} 已占用，请管理员协调资源')
    base.mkdir(parents=True, exist_ok=args.browser_only)
    program = base / '中文 安装目录'
    data = base / '中文 持久数据'
    reports = base / ('browser-resources' if args.browser_only else 'reports')
    reports.mkdir(exist_ok=args.browser_only)
    temporary = base / 'tmp'
    temporary.mkdir(exist_ok=args.browser_only)
    # 清除宿主会话所有引擎/运行时覆盖，子进程只接收本次解析后的绝对路径。
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GROUP_', 'XDG_GROUP_', 'HERMES_', 'CODEX_'))}
    env.update(GROUP_ENGINE_HOST='127.0.0.1', GROUP_ENGINE_PORT='18042',
               GROUP_DATA_DIR=str(data), GROUP_DB=str(data / 'group.db'),
               GROUP_ATTACHMENTS_DIR=str(data / 'attachments'),
               GROUP_ENGINE_URL='http://127.0.0.1:18042', XDG_GROUP_ENGINE_URL='http://127.0.0.1:18042',
               GROUP_ADMIN_PASSWORD='', GROUP_SECRET='isolated-release215',
               HERMES_HOME=str(base / 'hermes'), CODEX_HOME=str(base / 'codex'),
               HERMES_BIN=str(base / 'missing-hermes.exe'),
               TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    url = 'http://127.0.0.1:18042'
    results = []
    running = False
    steps = 0

    def run(command, expected=0, cwd=None):
        nonlocal steps
        steps += 1
        name = f'{steps:02d}-{Path(command[0]).stem}.log'
        print('RUN', steps, ' '.join(str(v) for v in command), flush=True)
        with (reports / name).open('wb') as log:
            result = subprocess.run([str(v) for v in command], cwd=cwd or program,
                                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != expected:
            raise RuntimeError(f'{name}: rc={result.returncode}, expected={expected}\n' +
                               (reports / name).read_text(encoding='utf-8', errors='replace')[-7000:])
        return (reports / name).read_text(encoding='utf-8', errors='replace')

    def api(path, body=None, method=None):
        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers={'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
            return json.load(r)

    def health():
        h = api('/health')
        assert Path(h['program_root']).resolve() == program.resolve(), h
        assert Path(h['db_path']).resolve() == (data / 'group.db').resolve(), h
        return h

    def start():
        nonlocal running
        running = True  # 启动命令部分成功时也须经身份核验清理本次实例。
        run([args.install_python, program / 'tools/service.py', 'start'])
        return health()

    def stop():
        nonlocal running
        run([args.install_python, program / 'tools/service.py', 'stop'])
        running = False

    def assert_data(task_id, migrated=False):
        with closing(sqlite3.connect(data / 'group.db')) as conn:
            assert conn.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == 'done'
            assert conn.execute("SELECT COUNT(*) FROM threads WHERE title='隔离发布验收话题'").fetchone()[0] == 1
            if migrated:
                assert conn.execute("SELECT name FROM sqlite_master WHERE name='release215_migration'").fetchone()
        assert (data / 'attachments' / 'keep.txt').read_text(encoding='utf-8') == 'ATTACHMENT'
        assert (program / 'engine' / 'user-notes.txt').read_text(encoding='utf-8') == 'USER'

    if args.browser_only:
        try:
            start()
            run([args.browser_python, ROOT / 'scripts/verify_release_browser.py', '--url', url,
                 '--profile', args.browser_profile, '--debug-port', '18043', '--report-dir', reports])
        finally:
            if running:
                stop()
        for port in (18042, 18043):
            with socket.socket() as s:
                assert s.connect_ex(('127.0.0.1', port)) != 0
        print('PASS browser refresh/reopen, CDP profile and actual loopback listeners; processes released')
        return

    try:
        # 完整运行时包，初装经 bootstrap 的真实 file URL 下载与 SHA256 校验。
        v1out = base / 'v1'
        make_release.make_release(ROOT, v1out, 'bot-group', 'v0.1.0')
        archive = v1out / 'bot-group-v0.1.0.zip'
        env.update(BOOTSTRAP_URL=archive.as_uri(), BOOTSTRAP_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                   INSTALL_DIR=str(program))
        run([args.install_python, ROOT / 'tools/bootstrap.py'], cwd=ROOT)
        run([args.install_python, program / 'tools/install.py', 'install'])
        first = start()
        assert api('/auth/status') == {'configured': False}
        project_root = base / '验收 项目'
        project_root.mkdir()
        (project_root / 'AGENTS.md').write_text('# Isolated release acceptance\n', encoding='utf-8')
        api('/projects', {'name': 'release215', 'display_name': '发布验收', 'root': str(project_root)})
        for role in ('manager', 'architect', 'dev', 'reviewer'):
            profile = 'release215-' + role
            api('/members', {'profile': profile, 'display_name': role, 'project': 'release215', 'active': True})
            api('/projects/release215/roles', {'name': role})
            api(f'/projects/release215/members/{profile}/role', {'role': role}, 'PATCH')
        permissions = api('/projects/release215/permissions')
        for name in permissions['grants']:
            permissions['grants'][name] = ['release215-manager', 'release215-architect']
        api('/projects/release215/permissions', {'revision': permissions['revision'], 'grants': permissions['grants']}, 'PUT')
        run([args.browser_python, ROOT / 'scripts/verify_release_browser.py', '--url', url,
             '--profile', args.browser_profile, '--debug-port', '18043', '--report-dir', reports])
        # 真实任务池入口，真实角色与授权；无模型调用、无替换 taskpool 函数。
        flow = '''from engine import db,taskpool
import json
project='release215'
conn=db.get_connection()
source=conn.execute("SELECT id,thread_id FROM messages WHERE project=? AND sender_type='user' ORDER BY id LIMIT 1",(project,)).fetchone()
roles={r['profile']:r['role_id'] for r in conn.execute('SELECT profile,role_id FROM project_members WHERE project=?',(project,))}
conn.close()
def check(r):
    assert 'error' not in r,r
    return r
rid=check(taskpool.req_create(project,'release215-manager','隔离安装任务',background='Release验收',scope='独立数据',approach='真实任务池',acceptance='任务完成并跨重启保留',undecided='无',source_ref=f'thread_id={source["thread_id"]}; message_id={source["id"]}'))['requirement_id']
t=check(taskpool.task_create(project,'release215-architect',rid,[dict(title='隔离任务',next_bot='release215-dev',description='验证完整任务流转',design_basis='隔离发布验收',acceptance_criteria='四角色交接闭环',prerequisites=[])],credential='isolated-release215',complete=True))
assert t['decompose_state']=='done',t
tid=t['task_ids'][0]
for actor,stage,next_actor in [('dev','dev','reviewer'),('reviewer','review','architect'),('architect','accept','manager')]:
    claim=check(taskpool.task_claim(project,'release215-'+actor))
    assert claim['claimed'] and claim['task']['id']==tid,claim
    check(taskpool.task_log(project,'release215-'+actor,tid,stage,result='隔离验收实际流转通过',next_bot='release215-'+next_actor,expected_role_id=roles['release215-'+next_actor]))
assert check(taskpool.task_complete(project,'release215-manager',tid))['completed']
assert check(taskpool.req_complete(project,'release215-manager',rid))['completed']
print(json.dumps({'task_id':tid,'requirement_id':rid}))
'''
        service_python = program / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        task_id = json.loads(run([service_python, '-c', flow]).strip())['task_id']
        (data / 'attachments').mkdir(exist_ok=True)
        (data / 'attachments' / 'keep.txt').write_text('ATTACHMENT', encoding='utf-8')
        (program / 'engine' / 'user-notes.txt').write_text('USER', encoding='utf-8')
        # fake成员完成任务后停用，后续重启扫描保持静默。
        for role in ('manager', 'architect', 'dev', 'reviewer'):
            api('/members/release215-' + role + '/active', {'project': 'release215', 'active': False})
        stop()
        second = start()
        assert second['instance_id'] != first['instance_id']
        assert_data(task_id)
        results.append('bootstrap、重复install、免密建项目成员与话题、四角色任务闭环、刷新重开、停启恢复')
        # 新版增加真实迁移表，并修改锁定文件摘要，验证依赖同步与实际启动迁移。
        stage = base / 'v2-source'
        with zipfile.ZipFile(archive) as z:
            z.extractall(stage)
        with (stage / 'engine/schema.sql').open('a', encoding='utf-8') as f:
            f.write('\nCREATE TABLE IF NOT EXISTS release215_migration (value TEXT);\n')
        with (stage / 'requirements.lock').open('a', encoding='utf-8') as f:
            f.write('\n# release215 v0.2.0 dependency synchronization\n')
        out = base / 'v2'
        make_release.make_release(stage, out, 'bot-group', 'v0.2.0')
        index = base / 'releases.json'
        entry = json.loads((out / 'releases.json').read_text(encoding='utf-8'))['releases'][0]
        for k in ('source', 'manifest'):
            entry[k] = (out / entry[k]).as_uri()
        env['RELEASE_INDEX_URL'] = index.as_uri()
        def publish(e):
            index.write_text(json.dumps({'releases': [e]}), encoding='utf-8')
        # 查询/下载/校验失败期间实际旧服务仍提供同一 instance_id。
        for change in ({'source': (base / 'missing.zip').as_uri()}, {'sha256': '0' * 64}, {'manifest': ''}):
            publish({**entry, **change})
            run([args.install_python, program / 'tools/upgrade.py', 'upgrade'], expected=1)
            assert health()['instance_id'] == second['instance_id']
        publish(entry)
        run([args.install_python, program / 'tools/upgrade.py', 'upgrade'])
        running = False
        assert (program / 'VERSION').read_text().strip() == 'v0.2.0'
        start()
        assert_data(task_id, migrated=True)
        state = json.loads((data / 'install.json').read_text())
        assert state['lock_sha256'] == hashlib.sha256((program / 'requirements.lock').read_bytes()).hexdigest()
        before = health()['instance_id']
        text = run([args.install_python, program / 'tools/upgrade.py', 'upgrade'])
        assert '无需更新' in text and health()['instance_id'] == before
        results.append('最新正式Release发现、资产失败旧实例继续服务、锁定依赖同步、真实迁移、无更新')
        # 实际安装子进程故障，触发真实旧依赖重建及回滚校验。
        (stage / 'tools/install.py').write_text('import sys\nsys.exit(7)\n', encoding='utf-8')
        out3 = base / 'v3'
        make_release.make_release(stage, out3, 'bot-group', 'v0.3.0')
        failed = json.loads((out3 / 'releases.json').read_text())['releases'][0]
        for k in ('source', 'manifest'):
            failed[k] = (out3 / failed[k]).as_uri()
        publish(failed)
        run([args.install_python, program / 'tools/upgrade.py', 'upgrade'], expected=1)
        running = False
        assert (program / 'VERSION').read_text().strip() == 'v0.2.0'
        start()
        assert_data(task_id, migrated=True)
        results.append('实际新版install子进程失败、旧版源码/解释器/依赖/数据库/附件/配置恢复并重新启动')
        stop()
    finally:
        if running:
            stop()
        evidence = {'results': results, 'program_root': str(program), 'data_dir': str(data),
                    'host': '127.0.0.1', 'backend_port': 18042,
                    'frontend': 'backend /app/ static mount; no separate frontend process',
                    'browser_port': 18043, 'browser_profile': str(args.browser_profile),
                    'models_called': False}
        (reports / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    for port in (18042, 18043):
        with socket.socket() as s:
            assert s.connect_ex(('127.0.0.1', port)) != 0, f'端口 {port} 未释放'
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
