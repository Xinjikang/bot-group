"""Live selected-agent acceptance using isolated project artifacts and configuration."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--runtime', choices=['hermes', 'codex'], required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--scenario', choices=['testing', 'software', 'research', 'writing', 'algorithm',
        'product-prototype', 'usage', 'draft', 'settings', 'remaining', 'all'], default='all')
    parser.add_argument('--browser-python', type=Path)
    args = parser.parse_args()
    if not args.home.is_absolute() or not args.evidence.is_absolute() or not args.home.is_dir():
        parser.error('existing absolute home and new absolute evidence directory required')
    args.evidence.mkdir(parents=True, exist_ok=False)
    base = args.evidence
    os.environ.update(GROUP_DB=str(base / 'group.db'), GROUP_DATA_DIR=str(base / 'data'))
    from engine import config, db, hud_runtime, hermes_gateway, project_hud as hud
    from fastapi import FastAPI
    from engine import api
    import uvicorn
    import yaml
    import tomlkit
    home = (args.home / 'profiles' / args.profile if args.runtime == 'hermes' and args.profile != 'default' else args.home)
    if args.runtime == 'codex':
        from engine import bot_identity
        codex_home, profile = bot_identity.codex_settings({'config_root': str(args.home), 'profile': args.profile})
        home = Path(codex_home)
    files = [home / name for name in ('config.yaml', 'config.toml', 'auth.json', '.env', 'SOUL.md', 'AGENTS.md')]
    if args.runtime == 'codex' and profile:
        files.append(home / f'{profile}.config.toml')
    def fingerprints():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    before = fingerprints()
    if args.runtime == 'hermes':
        source = yaml.safe_load((home / 'config.yaml').read_text(encoding='utf-8')) or {}
        route = source.get('model')
    else:
        config_path = home / (f'{profile}.config.toml' if profile and (home / f'{profile}.config.toml').exists() else 'config.toml')
        source = tomlkit.parse(config_path.read_text(encoding='utf-8'))
        selected = {**source, **source.get('profiles', {}).get(profile, {})} if profile else source
        route = {key: selected.get(key) for key in ('model', 'model_provider', 'model_reasoning_effort')}
    print(json.dumps({'runtime': args.runtime, 'route': route, 'scope': 'synthetic isolated project materials'}, ensure_ascii=False), flush=True)
    with closing(db.get_connection()) as conn:
        db.init_schema(conn)
        cursor = conn.execute('INSERT INTO bots(host_type,runtime,profile,config_root,display_name) VALUES(?,?,?,?,?)',
                     (args.runtime, args.runtime, args.profile, str(args.home), '原生助手产物验收'))
        bot_id = cursor.lastrowid
        conn.commit()
    app = FastAPI()
    app.include_router(api.router_api)
    from fastapi.staticfiles import StaticFiles
    app.mount('/app', StaticFiles(directory=Path(__file__).resolve().parents[1] / 'frontend', html=True))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    config.apply_cli(host='127.0.0.1', port=port, data_dir=str(base / 'data'))
    os.environ['GROUP_ENGINE_URL'] = f'http://127.0.0.1:{port}'
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    worker.start()
    limit = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > limit:
            raise RuntimeError('isolated API did not start')
        time.sleep(.05)
    records, calls, progress_updates = [], [], []
    original_connection = hermes_gateway.connection
    original_run = hud_runtime._run
    original_sink = hud_runtime.progress_sink
    browser = None
    @contextmanager
    def observed_sink(callback):
        def observed_progress(kind, text):
            if kind == 'reasoning' and progress_updates and progress_updates[-1]['kind'] == 'reasoning':
                progress_updates[-1] = {'kind': kind, 'text': text}
            else:
                progress_updates.append({'kind': kind, 'text': text})
            callback(kind, text)
        with original_sink(observed_progress):
            yield
    def observed_connection(*values, **options):
        client, root, command = original_connection(*values, **options)
        rpc = client.rpc
        def observed(method, **params):
            result = rpc(method, **params)
            record = {'method': method}
            if method == 'tools.show':
                record['tools'] = [t['name'] for s in result.get('sections', []) for t in s.get('tools', [])]
            if method in ('session.create', 'session.resume', 'session.mutate'):
                info = result.get('info', {})
                record['selection'] = {key: info[key] for key in ('toolsets','enabled_toolsets','edit_approval_policy') if key in info}
                if 'tools_changed' in result:
                    record['tools_changed'] = result['tools_changed']
            calls.append(record)
            return result
        client.rpc = observed
        return client, root, command
    def observed_run(*values, **options):
        command, prompt, env, cwd = values
        if args.runtime == 'codex':
            clean = dict(source)
            if profile:
                clean.update(source.get('profiles', {}).get(profile, {}))
            (base / f'native-launch-{len(records)}.json').write_text(json.dumps({
                'command': [arg if not arg.startswith(('mcp_servers.project_config=', 'developer_instructions=')) else arg.split('=',1)[0]+'=<session override>' for arg in command],
                'cwd': cwd, 'sandbox_mode': clean.get('sandbox_mode'),
                'default_permissions': clean.get('default_permissions'), 'windows': dict(clean.get('windows', {})),
                'features': dict(clean.get('features', {})), 'memories': dict(clean.get('memories', {})),
                'mcp_servers': list(clean.get('mcp_servers', {})), 'configuration_source': str(config_path),
                'native_settings_inherited': '--sandbox' not in command and '--ephemeral' not in command},
                ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            result = original_run(*values, **options)
        except hud_runtime.NativeProcessError as exc:
            (base / f'native-diagnostic-{len(records)}.log').write_text(exc.diagnostic, encoding='utf-8')
            raise
        (base / f'native-events-{len(records)}.jsonl').write_text(result, encoding='utf-8')
        return result
    hermes_gateway.connection = observed_connection
    hud_runtime._run = observed_run
    hud_runtime.progress_sink = observed_sink
    def save():
        (base / 'result.json').write_text(json.dumps({'runtime': args.runtime, 'route': route,
            'records': records, 'calls': calls, 'configuration_unchanged': before == fingerprints(),
            'gateway_boundary': 'selected native installation'},
            ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        scenarios = (['testing', 'software', 'research', 'writing', 'algorithm', 'product-prototype', 'usage', 'draft']
                     if args.scenario == 'all' else ['research', 'writing', 'algorithm', 'product-prototype', 'usage']
                     if args.scenario == 'remaining' else [args.scenario])
        for scenario in scenarios:
            root = base / 'projects' / scenario
            root.mkdir(parents=True)
            if scenario != 'draft':
                (root / 'materials').mkdir()
                (root / 'materials/SOURCE.md').write_text(
                    '# 图书借阅产品资料\n来源版本：SYNTHETIC-2026-10-A。\n'
                    'R-001：每人最多同时借阅 5 本图书。R-002：借期 30 天。R-003：逾期后暂停新增借阅。\n'
                    'P-01 登录页：登录成功进入书目页，失败保留输入并显示原因。\n'
                    'P-02 书目页：搜索、查看可借数量、进入图书详情。P-03 详情页：提交借阅，成功提示到期日。\n'
                    '品牌主色 #246BCE，桌面和 390px 窄屏。界面使用既有组件。\n', encoding='utf-8')
                if scenario == 'testing':
                    (root / 'docs/product').mkdir(parents=True)
                    (root / 'docs/product/PRD.md').write_text('# 原始需求\n\n## 既有保护约定\n保留标识：KEEP-EXISTING-SECTION。\n', encoding='utf-8')
                    targets = ['docs/product/PRD.md', 'docs/product/PROTOTYPE.md']
                    instruction = ('读取 materials/SOURCE.md 和测试模板中的两份产品文档骨架，直接完善 docs/product/PRD.md、'
                        'docs/product/PROTOTYPE.md。保留 PRD 既有保护约定，准确保留来源版本、R-001/R-002/R-003、'
                        '数值5和30、页面P-01/P-02/P-03及状态，原型关联对应需求；缺失信息标为待确认。'
                        '完成后读取核验，并把项目简介改为“图书借阅独立测试资料已准备”。在 HUD 简短汇报实际结果。')
                elif scenario == 'software':
                    targets = ['docs/engineering/ENGINEERING.md', 'docs/ui-spec/UI-SPEC.md']
                    instruction = ('读取 materials/SOURCE.md 和软件模板对应工程/UI规范骨架，直接编写 '
                        'docs/engineering/ENGINEERING.md 与 docs/ui-spec/UI-SPEC.md，登记已知项目条件、品牌色和窄屏要求，'
                        '工程技术栈及运行命令保持待确认，保留资料来源版本。完成后读取核验；在 HUD 汇报实际文件。')
                elif scenario == 'usage':
                    targets = []
                    instruction = ('依据当前安装 bot-group 的工程与使用文档，说明项目成员如何登记工作区资源、'
                        '测试报告应写到哪里，以及更新引擎代码后如何使其生效。读取依据后准确回答，引用文档路径和章节，'
                        '这是使用咨询，在 HUD 简短解答。')
                elif scenario == 'settings':
                    targets = []
                    isolated_home = base / 'created-profiles'
                    (isolated_home / 'profiles').mkdir(parents=True)
                    os.environ['CODEX_HOME'] = str(isolated_home)
                    instruction = ('请实际完成本项目配置：把角色“旧审查\u200c”改名为“审查员”，将现有的合成测试成员先停用再启用，'
                        '并给它分配“审查员”角色；审查员只启用 task.update 角色权限，保留其他角色及成员授权。'
                        '再创建 Codex profile，标识 hud-reviewer-proof、显示名“验收审查员”，使用默认配置来源，'
                        '自动引用当前软件模板的 reviewer SOUL 全文，并分配审查员角色，读取实际说明文件核验。'
                        '最后帮我打开角色页面，我看一眼。在 HUD 简短汇报实际结果。')
                else:
                    cases = {
                        'research': (['docs/BRIEF.md', 'materials/SOURCES.md'],
                            '研究目标是评估图书借阅流程的需求完整性，交付可追溯的缺项与决策报告；只使用已给资料，研究范围和验收标准写入 brief。'),
                        'writing': (['docs/BRIEF.md', 'materials/SOURCES.md'],
                            '为图书管理员准备借阅规则操作手册，读者为新入职管理员，发布渠道为内部知识库，目标篇幅800字；整理写作要求和事实来源。'),
                        'algorithm': (['docs/requirements/BRIEF.md', 'docs/engineering/ENGINEERING.md'],
                            '算法目标是根据读者借阅数与逾期状态判断可否借阅，输入包含借阅数和逾期状态，输出为允许/拒绝及原因；数据样本、指标阈值和技术栈待确认。'),
                        'product-prototype': (['outputs/PRD.md', 'ui-spec/UI-SPEC.md', 'materials/RESEARCH.md'],
                            '产品目标是明确图书借阅交互，整理产品需求、界面规范和已有研究依据；本轮交付为启动资料。'),
                    }
                    targets, goal = cases[scenario]
                    instruction = ('读取 materials/SOURCE.md 和当前项目类型模板骨架，直接整理以下资料文件：'
                        + '、'.join(targets) + '。' + goal
                        + '准确保留 SYNTHETIC-2026-10-A 来源版本、5本和30天规则及逾期约束，按模板区分已知条件与待确认事项。'
                        '完成后读取核验，在 HUD 汇报实际文件与遗留项。')
                with closing(db.get_connection()) as conn:
                    conn.execute("INSERT INTO projects(name,display_name,root,description,directory_mode,created_at) VALUES(?,?,?,?, 'shared',?)",
                                 (scenario, '原生助手验收', str(root), '合成资料准备', db.now_iso()))
                    conn.execute("INSERT INTO project_config_events(project,actor,action,detail,created_at) VALUES(?, 'fixture','project.import',?,?)",
                                 (scenario, json.dumps({'template': {'id': 'software' if scenario in ('usage', 'settings') else scenario}}), db.now_iso()))
                    conn.commit()
                    if scenario == 'settings':
                        from engine import project_team, bot_identity
                        role = project_team.create_role(conn, scenario, '旧审查\u200c', 'fixture')
                        cursor = conn.execute('INSERT INTO bots(host_type,runtime,profile,config_root,display_name) VALUES(?,?,?,?,?)',
                            ('codex', 'codex', 'synthetic-member', str(isolated_home), '合成测试成员'))
                        member = bot_identity.enroll(conn, cursor.lastrowid, scenario)
                        project_team.assign_role(conn, scenario, member, role['name'], 'fixture')
                        cursor = conn.execute('INSERT INTO bots(host_type,runtime,profile,config_root,display_name) VALUES(?,?,?,?,?)',
                            ('codex', 'codex', 'preserved-member', str(isolated_home), '保持原授权的成员'))
                        preserved = bot_identity.enroll(conn, cursor.lastrowid, scenario)
                        conn.execute('INSERT INTO project_permissions(project,profile,permission) VALUES(?,?,?)', (scenario, preserved, 'req.create'))
                        conn.commit()
                item = hud.create(scenario)
            else:
                targets = []
                item = hud.create()
                item = hud.update(item['id'], {'revision': item['revision'], 'draft': {
                    'template': 'research', 'root': str(root), 'display_name': '待完善', 'description': '市场调研'}})
                instruction = '直接把当前新建向导的项目名称改为“光伏市场研究”，简介改为“研究工商业光伏设备市场，交付带来源的决策报告”。调用配置工具完成，在 HUD 汇报。'
            item = hud.update(item['id'], {'revision': item['revision'], 'bot_id': bot_id})
            item = hud.answer(item['id'], {'revision': item['revision'], 'text': instruction, 'question_id': None})
            began = time.monotonic()
            progress_updates.clear()
            if scenario == 'settings' and args.browser_python:
                evidence = base / 'browser'
                browser = subprocess.Popen([str(args.browser_python), '-X', 'utf8', '-B',
                    str(Path(__file__).with_name('test_hud_navigation_browser.py')), '--url', f'http://127.0.0.1:{port}',
                    '--project', scenario, '--evidence', str(evidence)])
                deadline = time.monotonic() + 40
                while not (evidence / 'ready.json').exists():
                    if browser.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('navigation browser did not become ready')
                    time.sleep(.1)
            hud.run_one(item['id'])
            result = hud.view(item['id'])
            record = {'scenario': scenario, 'seconds': round(time.monotonic()-began, 2),
                      'status': result['status'], 'error': result['error'], 'session': item['id'],
                      'entries': result['entries'], 'progress': list(progress_updates), 'artifacts': []}
            records.append(record)
            save()
            assert result['status'] != 'error', result['error']
            for name in targets:
                path = root / name
                assert path.is_file(), f'Actual artifact missing: {name}'
                content = path.read_text(encoding='utf-8-sig')
                assert len(content) >= 150, f'Artifact lacks substantive content: {name}'
                record['artifacts'].append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'characters': len(content)})
            if scenario == 'testing':
                prd = (root / targets[0]).read_text(encoding='utf-8-sig')
                proto = (root / targets[1]).read_text(encoding='utf-8-sig')
                for token in ('SYNTHETIC-2026-10-A', 'R-001', 'R-002', 'R-003', 'KEEP-EXISTING-SECTION', '5', '30'):
                    assert token in prd, f'PRD missing required source information: {token}'
                for token in ('P-01', 'P-02', 'P-03'):
                    assert token in proto, f'Prototype missing page: {token}'
                with closing(db.get_connection()) as conn:
                    actual = conn.execute('SELECT description FROM projects WHERE name=?', (scenario,)).fetchone()[0]
                assert actual == '图书借阅独立测试资料已准备', actual
                record['configuration'] = {'description': actual}
            elif scenario == 'software':
                content = (root / targets[1]).read_text(encoding='utf-8-sig')
                assert '#246BCE' in content and '390' in content
            elif scenario == 'draft':
                assert result['state']['draft']['display_name'] == '光伏市场研究', result['state']['draft']
                assert result['state']['draft']['description'] == '研究工商业光伏设备市场，交付带来源的决策报告'
                record['configuration'] = result['state']['draft']
            elif scenario == 'settings':
                from engine import project_templates
                with closing(db.get_connection()) as conn:
                    actual_role = conn.execute('SELECT id,name FROM project_roles WHERE project=? AND name=?', (scenario, '审查员')).fetchone()
                    assert actual_role is not None
                    assert conn.execute('SELECT active,role_id FROM project_members WHERE project=? AND profile=?', (scenario, member)).fetchone()[:] == (1, actual_role['id'])
                    transitions = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM project_config_events WHERE project=? AND action='member.active' ORDER BY id", (scenario,))]
                    assert [r['after'] for r in transitions if r['profile'] == member] == [0, 1], transitions
                    assert conn.execute('SELECT permission FROM project_permissions WHERE project=? AND profile=?', (scenario, f'role:{actual_role["id"]}')).fetchall()[0][0] == 'task.update'
                    assert conn.execute('SELECT 1 FROM project_permissions WHERE project=? AND profile=? AND permission=?', (scenario, preserved, 'req.create')).fetchone()
                    created = conn.execute("SELECT pm.profile,pm.role_id FROM project_members pm JOIN bots b ON b.id=pm.bot_id WHERE pm.project=? AND b.profile='hud-reviewer-proof'", (scenario,)).fetchone()
                    assert created and created['role_id'] == actual_role['id']
                path = isolated_home / 'profiles/hud-reviewer-proof/AGENTS.md'
                expected = project_templates.text_resource(project_templates.TEMPLATE_ROOT / 'software', 'references/souls/reviewer.md')
                assert path.read_text(encoding='utf-8').rstrip('\r\n') == expected.rstrip('\r\n')
                assert result['state']['navigation']['page'] == 'roles'
                assert any(p['text'] == ('正在读取文件' if args.runtime == 'hermes' else '正在执行命令') for p in progress_updates), progress_updates
                record['artifacts'].append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'characters': len(expected)})
                record['configuration'] = {'role': actual_role['name'], 'member': member, 'created_profile': created['profile']}
                if browser:
                    assert browser.wait(timeout=20) == 0
                    assert json.loads((base / 'browser/result.json').read_text(encoding='utf-8'))['passed']
            elif scenario == 'usage':
                response = '\n'.join(e['content'].get('text', '') for e in result['entries'] if e['role'] == 'assistant')
                assert 'docs/' in response and 'reports/' in response and '重启' in response, response
                record['usage_answer'] = response
            else:
                contents = '\n'.join((root / name).read_text(encoding='utf-8-sig') for name in targets)
                assert 'SYNTHETIC-2026-10-A' in contents and '5' in contents and '30' in contents
            record['passed'] = True
            save()
            print(json.dumps({'scenario': scenario, 'passed': True, 'seconds': record['seconds'],
                              'artifacts': record['artifacts']}, ensure_ascii=False), flush=True)
        assert before == fingerprints(), 'Selected agent configuration changed'
        save()
    finally:
        save()
        hermes_gateway.connection = original_connection
        hud_runtime._run = original_run
        hud_runtime.progress_sink = original_sink
        from engine import hermes_connections
        hermes_connections.stop()
        server.should_exit = True
        if browser and browser.poll() is None:
            browser.terminate()
            browser.wait(timeout=10)
        worker.join(timeout=10)
        sock.close()


if __name__ == '__main__':
    main()
