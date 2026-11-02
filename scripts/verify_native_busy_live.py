"""req#142 成果 1 task#276 验收 7 真机层：真实异步子任务回调驱动 Hermes 自发续跑。

场景（全部隔离：随机回环端口 + 隔离数据目录 + 隔离 Hermes home，复用 gw_probe 安装与模型配置）：
1. 隔离引擎 + 真实网关启动；A（default，hermes bot）经引擎真实 spawn 一次（work 会话）
2. A 第一轮真实模型调用 delegate_task 提交异步子任务后结束（引擎 admission 终态）
3. 子任务完成回调自动续跑 A 的原生会话（新 admission，引擎无 hermes_executions 行），
   续跑第一动作调用 busy_wait 阻塞（受控窗口，证明原生 running 无引擎记录）
4. 窗口内：A 的 work job + B 的 work job 入队，由引擎真实队列 worker 轮转——
   预检查命中原生忙 → B 投递（记录层）、A 保留原 ID 排队
5. 释放 busy_wait → 续跑结束 → 原生空闲 → 引擎队列恢复投递 A（原 ID，真实二次 spawn）
6. 证据：原生 admission/会话状态、引擎执行记录、wake_jobs 时间线、清理三方核验

分层说明（验收 7 要求模拟层与真实运行层分别记录）：
- 真实层：异步委托与续跑、原生 running/pending、引擎队列 worker 的跳过/投递决策、A 恢复投递
- 记录层：B 的 deliver 由 serve 内记录器承接（真实 spawn 链路由既有网关真机探针覆盖），
  避免为 B 再拉起一个真实模型回合
- /thinking 展示证据属 req#142 成果 2（task#277），本脚本不采集

退出码：0=全部门禁通过；3=轮询超时；4=原生状态/引擎记录不一致；5=隔离失守；6=preflight 拒绝；
9=清理失败；10=实例归属不一致。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def mcp_busy_wait():
    from mcp.server.fastmcp import FastMCP
    server = FastMCP('native_busy_probe')

    @server.tool()
    def busy_wait() -> str:
        """Block until the isolated probe controller releases this execution."""
        root = Path(os.environ['NATIVE_BUSY_PROBE_WORK'])
        (root / 'ready_busy_wait').touch()
        deadline = time.monotonic() + 600
        while not (root / 'continue').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Probe controller did not release busy_wait')
            time.sleep(.2)
        return 'Released. Reply exactly once with [BUSY_DONE].'

    server.run()


def serve(url):
    from fastapi import Depends
    from engine import main, hermes_gateway as gateway, db, wake
    from engine.hermes_gateway_client import GatewayClient
    endpoint = json.loads(os.environ['NATIVE_BUSY_PROBE_ENDPOINT'])
    command = json.loads(os.environ['NATIVE_BUSY_PROBE_COMMAND'])
    home = Path(os.environ['HERMES_HOME'])

    class DirectLease:
        """租约契约包装：探针直连客户端 + close 释放（预检查与 spawn 共用）。"""

        def __init__(self, client):
            self.client = client
            self.closed = False

        @property
        def endpoint(self):
            return self.client.endpoint

        def rpc(self, method, **params):
            return self.client.rpc(method, **params)

        def close(self):
            self.closed = True

    gateway.connection = lambda *a, **k: (DirectLease(GatewayClient(endpoint)), home, command)

    # 注入 busy_wait 工具（会话级 editor MCP），供续跑第一动作受控阻塞
    original_configure = gateway.sessions.configure_tracked

    def configure(conn, project, client, snapshot, **kwargs):
        snapshot = original_configure(conn, project, client, snapshot, **kwargs)
        # 追加 busy_wait 工具（保留原有 editor MCP 如 task_queue），供续跑第一动作受控阻塞
        current = snapshot.get('editor_mcp') or {}
        specs = list(current.get('mcp_servers') or [])
        specs.append({'name': 'native_busy_probe', 'command': sys.executable,
                      'args': [__file__, '--mcp'],
                      'env': [{'name': 'NATIVE_BUSY_PROBE_WORK',
                               'value': os.environ['NATIVE_BUSY_PROBE_WORK']}]})
        gateway.sessions.mutate(client, snapshot, 'editor_mcp', {'mcp_servers': specs})
        return gateway.sessions.attach(client, snapshot['session_id'])

    gateway.sessions.configure_tracked = configure

    # B 的投递记录层：真实引擎队列 worker 走到 deliver 即证明「其他独立会话继续推进」；
    # B 不拉真实模型回合（真实 spawn 链路由既有网关真机探针覆盖）
    delivered_b = []
    original_deliver = wake.deliver

    def deliver(profile, kind, **kwargs):
        if profile == 'probe-b':
            delivered_b.append({'profile': profile, 'kind': kind, 'thread_id': kwargs.get('thread_id'),
                               'job_id': kwargs.get('job_id'), 'at': time.time()})
            return {'ok': True, 'detail': 'recorded-b-delivery'}
        return original_deliver(profile, kind, **kwargs)

    wake.deliver = deliver

    @main.app.post('/probe/spawn-a')
    def spawn_a(payload: dict, identity=Depends(main._resolve_identity)):
        main._admin_only(identity, 'Probe administrator required')
        conn = db.get_connection()
        try:
            bot_id = db._ensure_hermes_bot(conn, 'default')
            bot = conn.execute('SELECT * FROM bots WHERE id=?', (bot_id,)).fetchone()
        finally:
            conn.close()
        result = gateway.spawn('default', 'work', payload['signal'], None, 'alpha',
                               bot=bot, workdir=os.environ['NATIVE_BUSY_PROBE_WORK'], model_args=[])
        return result

    @main.app.get('/probe/b-deliveries')
    def b_deliveries(identity=Depends(main._resolve_identity)):
        main._admin_only(identity, 'Probe administrator required')
        return {'deliveries': delivered_b}

    target = urlsplit(url)
    main.main(['--host', target.hostname, '--port', str(target.port), '--data-dir', os.environ['GROUP_DATA_DIR']])


def verify(args):
    import httpx
    import yaml
    import gw_probe as probe
    from engine import db, auth, wake as wake_mod
    from engine.hermes_gateway_client import GatewayClient, ensure
    from scripts.probe_config import absolute_path, loopback_url
    from scripts.project_test_support import enable_members

    url = loopback_url(args.backend)
    target = urlsplit(url)
    with socket.socket() as check:
        check.bind((target.hostname, target.port))
    evidence = absolute_path(args.evidence, '--evidence')
    evidence.mkdir(parents=True, exist_ok=False)
    record, detail = probe.install_record()
    if not record:
        raise RuntimeError(f'preflight 拒绝：{detail}')
    if args.run_dir:
        run = Path(args.run_dir)
        if not run.is_dir():
            raise ValueError('--run-dir 必须指向已存在的预热 run 目录')
    else:
        # run 目录必须短：home/installs/<id>/environments/<hash>/venv/.../psutil/_psutil_windows.*.pyd
        # 是 DLL 加载路径，Windows 260 字符限制内才可导入（task-276/<uuid32> 实测 269 字符超限失败）
        run = probe.PROBE_DATA / ('t' + uuid.uuid4().hex[:12])
    home, workspace, data = run / 'home', run / 'workspace', run / 'engine'
    for directory in (home, workspace, data):
        directory.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load((probe.HERMES_HOME / 'config.yaml').read_text(encoding='utf-8'))
    config.update(mcp_servers={}, approvals={'mode': 'smart'},
                  terminal={'backend': 'local', 'cwd': str(workspace), 'timeout': 150})
    # acp 会话工具集：todo + delegation（生产 config.yaml cli 平台同款；只给 todo 会缺
    # delegate_task，异步回调场景无法触发——上一轮真机证据已确认）
    config['platform_toolsets'] = {'acp': ['todo', 'delegation']}
    config.setdefault('agent', {})['max_turns'] = 24
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    shutil.copy2(probe.HERMES_HOME / '.env', home / '.env')
    # 解释器/源码经命令行注入（默认 gw_probe 安装与 worktree；真机异步回调场景需带
    # delegate_task 的生产 Hermes 时用 --hermes-python/--hermes-source 注入，禁止写死路径）
    hermes_python = args.hermes_python or str(record['python'])
    hermes_source = args.hermes_source or str(probe.WORKTREE_SRC)
    command = [hermes_python, '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    # 网关环境与引擎环境分离：网关只拿白名单 + 隔离 home/source（不带 GROUP_* 管理域配置，
    # 防止生产版网关心跳引擎/连接托管配置）；引擎环境另加隔离引擎配置
    gateway_env = probe.probe_env()
    gateway_env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / 'runtime'),
                       PYTHONPATH=hermes_source)
    engine_env = dict(gateway_env)
    engine_env.update(PYTHONPATH=str(ROOT),
                      GROUP_DATA_DIR=str(data), GROUP_DB=str(data / 'group.db'),
                      GROUP_ENGINE_HOST=target.hostname, GROUP_ENGINE_PORT=str(target.port),
                      GROUP_ENGINE_URL=url, GROUP_ATTACHMENTS_DIR=str(data / 'attachments'),
                      GROUP_ADMIN_PASSWORD='isolated-native-busy', GROUP_SECRET=secrets.token_hex(32),
                      GROUP_ENGINE_KEY=secrets.token_hex(32), NATIVE_BUSY_PROBE_WORK=str(workspace),
                      NATIVE_BUSY_PROBE_COMMAND=json.dumps(command), GROUP_MCP_PYTHON=sys.executable)
    os.environ.update(engine_env)
    subprocess.run([sys.executable, str(ROOT / 'tools' / 'install.py'), 'repair'],
                   cwd=ROOT, env=engine_env, check=True, stdout=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    conn = db.get_connection()
    db.init_schema(conn)
    conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) VALUES('default','probe-a','dev')")
    conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) VALUES('probe-b','probe-b','dev')")
    conn.execute("INSERT OR IGNORE INTO projects(name,root,created_at) VALUES('alpha',?,?)",
                 (str(workspace), db.now_iso()))
    enable_members(conn, ('alpha',), ('default', 'probe-b', 'user'))
    conn.commit()
    headers = {'Authorization': 'Bearer ' + auth.login('isolated-native-busy')['token']}
    client = httpx.Client(base_url=url, headers=headers, timeout=10, trust_env=False)
    result = {'url': url, 'data_dir': str(data), 'checks': {}, 'engine_rows': [],
              'jobs': [], 'resources_released': False}
    endpoint = None
    initial = None
    viewer = None

    def health():
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            try:
                value = client.get('/health').json()
                if value.get('instance_id'):
                    assert Path(value['db_path']) == data / 'group.db'
                    return value
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.2)
        raise TimeoutError('isolated engine readiness deadline exceeded')

    def engine_rows():
        return [dict(r) for r in conn.execute('SELECT * FROM hermes_executions ORDER BY started_at')]

    try:
        endpoint = ensure(command, home, env=gateway_env, timeout=600)
        engine_env['NATIVE_BUSY_PROBE_ENDPOINT'] = json.dumps(endpoint)
        result['endpoint'] = endpoint
        # 隔离归属门禁：网关必须绑定隔离 home（生产版启动器可能改写 HERMES_HOME）
        profile_home = Path(endpoint.get('profile_id') or '')
        assert profile_home == home, f'gateway bound unexpected home: {profile_home}'
        # preflight：新 home 环境下 psutil 必须可导入（Windows 260 路径限制，超长即失败快报）
        home_pythons = list((home / 'installs').rglob('python.exe'))
        if home_pythons:
            check = subprocess.run([str(home_pythons[0]), '-c', 'import psutil; print(psutil.__version__)'],
                                   capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', timeout=120)
            if check.returncode != 0:
                raise RuntimeError(f'isolated home psutil import failed: {check.stderr.strip()}')
        with (evidence / 'engine.log').open('wb') as log:
            initial = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', __file__, '--serve', '--backend', url],
                cwd=ROOT, env=engine_env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        health()
        viewer = GatewayClient(endpoint)
        signal = ('Isolated native-busy probe. First round only: use tool_search to find delegate_task, '
                  'then call delegate_task exactly once with one sub-task whose goal is to reply with a single '
                  'sentence "delegation done". Do not wait for its result; finish this round immediately. '
                  'When the system later resumes you after the delegation completes, your first action must be '
                  'calling busy_wait (find it with tool_search) and waiting for its result; then reply exactly '
                  'once with [BUSY_DONE] and finish.')
        response = client.post('/probe/spawn-a', json={'signal': signal}, timeout=120)
        response.raise_for_status()
        assert response.json().get('ok'), response.json()
        result['checks']['a_first_admission_spawned'] = True
        # 等续跑（异步回调自动恢复的原生执行）进入 busy_wait：受控窗口
        deadline = time.monotonic() + 300
        while not (workspace / 'ready_busy_wait').exists():
            if time.monotonic() > deadline:
                # 超时落盘会话证据（模型未按信号调用 delegate_task/busy_wait 时可定位实际行为）
                binding = conn.execute("SELECT session_id FROM session_bindings WHERE profile='default' "
                                       "AND kind='work'").fetchone()
                if binding:
                    try:
                        snapshot = viewer.rpc('session.resume', session_id=binding['session_id'], timeout=10)
                        snapshot = {'revision': snapshot.get('revision'), 'running': snapshot.get('running'),
                                    'pending': snapshot.get('pending'),
                                    'messages': [{'role': m.get('role'), 'content': str(m.get('content'))[:300]}
                                                 for m in (snapshot.get('messages') or [])[-8:]]}
                    except Exception as exc:  # noqa: BLE001
                        snapshot = {'resume_error': str(exc)}
                else:
                    snapshot = {'binding': None}
                (evidence / 'timeout-session.json').write_text(
                    json.dumps({'snapshot': snapshot,
                                'workspace': [p.name for p in workspace.iterdir()]},
                               ensure_ascii=False, indent=2, default=str), encoding='utf-8')
                raise TimeoutError('native continuation did not enter busy_wait')
            time.sleep(1)
        rows_at_window = engine_rows()
        # 验收门禁：续跑（异步回调恢复的原生执行）期间引擎记录必须全部终态
        assert all(r['state'] == 'terminal' for r in rows_at_window), rows_at_window
        result['checks']['continuation_running_without_engine_row'] = True
        assert any(r['outcome'] == 'completed' for r in rows_at_window), rows_at_window
        result['checks']['first_admission_terminal'] = True
        binding = conn.execute("SELECT session_id,generation FROM session_bindings WHERE profile='default' "
                               "AND kind='work'").fetchone()
        assert binding, 'missing work binding for default'
        snapshot = viewer.rpc('session.resume', session_id=binding['session_id'], timeout=10)
        assert snapshot['running'] or snapshot['pending'], snapshot
        result['checks']['native_running_while_engine_idle'] = True
        # 窗口内入队 A + B，交给引擎真实队列 worker
        wake_mod.enqueue(conn, 'default', 'work', 'probe', project='alpha')
        wake_mod.enqueue(conn, 'probe-b', 'work', 'probe', project='alpha')
        job_a = conn.execute("SELECT * FROM wake_jobs WHERE profile='default' AND kind='work'").fetchone()
        job_b = conn.execute("SELECT * FROM wake_jobs WHERE profile='probe-b' AND kind='work'").fetchone()
        deadline = time.monotonic() + 30
        deliveries = []
        while time.monotonic() < deadline:
            deliveries = client.get('/probe/b-deliveries').json()['deliveries']
            if deliveries:
                break
            time.sleep(.5)
        assert deliveries, 'B was not delivered while A native-busy'
        row_a = conn.execute('SELECT id,status FROM wake_jobs WHERE id=?', (job_a['id'],)).fetchone()
        assert row_a['status'] == 'queued' and row_a['id'] == job_a['id'], row_a
        result['checks']['b_delivered_a_retained_original_id'] = True
        result['jobs'].append({'window': 'native-busy', 'a': dict(job_a), 'b': dict(job_b),
                               'b_deliveries': deliveries})
        # 释放 busy_wait → 续跑结束 → 原生空闲 → A 恢复投递（原 ID）
        (workspace / 'continue').touch()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            row_a = conn.execute('SELECT status FROM wake_jobs WHERE id=?', (job_a['id'],)).fetchone()
            if row_a['status'] == 'done':
                break
            time.sleep(1)
        assert row_a['status'] == 'done', row_a
        result['checks']['a_recovered_same_id'] = True
        result['jobs'].append({'window': 'after-release', 'a': dict(conn.execute(
            'SELECT * FROM wake_jobs WHERE id=?', (job_a['id'],)).fetchone())})
        result['engine_rows'] = engine_rows()
        # 引擎行=首次 admission + 恢复投递 admission，共 2 条 completed；续跑无引擎行
        completed = sum(1 for r in result['engine_rows'] if r['outcome'] == 'completed')
        assert completed == 2, result['engine_rows']
        result['checks']['continuation_left_no_engine_row'] = True
    finally:
        try:
            from engine.maintenance.stop_engine import stop
            stop(url, timeout=25)
        finally:
            if initial is not None:
                initial.wait(timeout=10)
            if viewer is not None:
                viewer.close()
            # ensure 超时/失败（endpoint 为空）同样按隔离 home 回收可能残留的网关
            ended = subprocess.run(command + ['gateway', 'stop', '--yes'], env=gateway_env,
                capture_output=True, text=True, encoding='utf-8', timeout=120,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result['gateway_stop'] = ended.returncode
            residue = subprocess.run(
                ['wmic', 'process', 'where', 'name="python.exe"', 'get', 'ProcessId,CommandLine'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            # 残留判据用 run 目录（网关进程命令行含 runtime 路径而非 home 路径，gateway stop 失败时
            # 仍可能有进程持有 home 锁），并核对 gateway.pid 文件
            result['gateway_home_clean'] = (str(run) not in (residue.stdout or '')
                                            and not (home / 'gateway.pid').exists())
            with socket.socket() as check:
                result['resources_released'] = check.connect_ex((target.hostname, target.port)) != 0
            client.close()
            conn.close()
            (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    required = ['a_first_admission_spawned', 'continuation_running_without_engine_row',
                'first_admission_terminal', 'native_running_while_engine_idle',
                'b_delivered_a_retained_original_id', 'a_recovered_same_id',
                'continuation_left_no_engine_row']
    missing = [name for name in required if result['checks'].get(name) is not True]
    assert not missing, f'required checks not all true: {missing}'
    assert result['gateway_home_clean'], 'isolated home still holds gateway processes'
    assert result['resources_released'], f'engine port not released: {url}'
    print(json.dumps(result['checks'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend')
    parser.add_argument('--evidence')
    parser.add_argument('--run-dir')
    parser.add_argument('--hermes-python')
    parser.add_argument('--hermes-source')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--mcp', action='store_true')
    args = parser.parse_args()
    if args.mcp:
        mcp_busy_wait()
    elif args.serve and args.backend:
        serve(args.backend)
    elif args.evidence and args.backend:
        verify(args)
    else:
        parser.error('--evidence is required')
