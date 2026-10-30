"""Record two native turns against a loopback provider, with disposable homes and real MCP schemas."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    # Preserve tool/message array order and every string byte.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def save(base, name, text):
    (base / name).write_bytes(text.encode('utf-8'))


class Provider(ThreadingHTTPServer):
    def __init__(self):
        self.requests = []
        super().__init__(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.reply({'object': 'list', 'data': []})

    def reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/api/show'):
            self.reply({'model_info': {'general.context_length': 131072}})
            return
        self.server.requests.append(payload)
        if self.path.endswith('/chat/completions'):
            if payload.get('stream'):
                chunks = [{'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk', 'created': 1,
                           'model': 'mock-prefix', 'choices': [{'index': 0, 'finish_reason': None,
                           'delta': {'role': 'assistant', 'content': 'OK'}}]},
                          {'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk', 'created': 1,
                           'model': 'mock-prefix', 'choices': [{'index': 0, 'finish_reason': 'stop', 'delta': {}}],
                           'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}]
                self.stream(''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
                return
            self.reply({'id': 'chatcmpl-fixture', 'object': 'chat.completion', 'created': 1,
                        'model': 'mock-prefix', 'choices': [{'index': 0, 'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': 'OK'}}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
            return
        assert self.path.endswith('/responses'), self.path
        item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}
        response = {'id': 'resp_fixture', 'object': 'response', 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        events = [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': response}]
        self.stream(''.join('data: ' + json.dumps(e) + '\n\n' for e in events))

    def stream(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def record(base, index, request, rules, manifest=None):
    save(base, f'wake-{index}.request.json', json.dumps(request, ensure_ascii=False, indent=2))
    messages = request.get('messages', request.get('input', []))
    system = request.get('instructions') or ''
    system_messages = [m for m in messages if m.get('role') == 'system']
    if system_messages:
        content = system_messages[0]['content']
        system = content if len(system_messages) == 1 and isinstance(content, str) else canonical(system_messages)
    developer = canonical([m for m in messages if m.get('role') == 'developer'])
    tools = request.get('tools', [])
    def tool_names(items, prefix=''):
        for tool in items:
            name = tool.get('name', tool.get('function', {}).get('name', ''))
            if tool.get('type') == 'namespace':
                yield from tool_names(tool['tools'], prefix + name + '.')
            else:
                yield prefix + name
    names = list(tool_names(tools))
    operation_names = [n for n in names if n.endswith('operation_get')]
    if manifest is not None:
        operation_names = [n for n in tool_names(manifest) if n.endswith('operation_get')]
        assert operation_names and all(n in canonical(tools) for n in operation_names), 'Operation absent from native tool catalog'
    else:
        assert operation_names, names
    assert rules in system or rules in ''.join(
        part.get('text', '') for m in messages if m.get('role') in ('developer', 'system')
        for part in (m.get('content') if isinstance(m.get('content'), list)
                     else [{'text': m.get('content', '')}])), 'Rules absent from system/developer layer'
    values = {'rules': rules, 'system': system, 'developer': developer, 'tools': canonical(tools)}
    if manifest is not None:
        values['mcp_manifest'] = canonical(manifest)
    for key, value in values.items():
        save(base, f'wake-{index}.{key}.txt', value)
    return {**{key + '_sha256': digest(value) for key, value in values.items()},
            'tool_count': len(names), 'operation_get': operation_names}


def command_run(command, cwd, env, base, label):
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        out, err = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True) + [parent]:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        out, err = proc.communicate(timeout=10)
        raise RuntimeError(f'{label} timed out')
    finally:
        if 'out' in locals():
            (base / f'{label}.stdout.txt').write_bytes(out)
            (base / f'{label}.stderr.txt').write_bytes(err)
    assert proc.returncode == 0, f'{label} exited {proc.returncode}; inspect stderr evidence'
    return out.decode('utf-8')


def codex(args, base, home, work, provider, build_rules):
    from engine import wake
    config = ('model = "mock-prefix"\nmodel_provider = "fixture"\n'
              '[model_providers.fixture]\nname = "fixture"\nwire_api = "responses"\n'
              f'base_url = "{provider.origin}/v1"\nrequires_openai_auth = false\n'
              'request_max_retries = 0\nstream_max_retries = 0\n')
    (home / 'config.toml').write_text(config, encoding='utf-8')
    version = subprocess.check_output([str(args.codex_bin), '--version'], text=True).strip()
    sid, records = None, []
    for index in (1, 2):
        rules = build_rules()
        with patch('engine.auth.bot_token', return_value=f'fixture-credential-{index}'), \
             patch.object(wake, '_engine_url', return_value=provider.origin):
            mcp = wake._codex_mcp_config('fixture', 'prefix-probe', chat=args.kind == 'chat')
        command = [str(args.codex_bin), 'exec', '--json', '--skip-git-repo-check',
                   '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false',
                   '-c', 'developer_instructions=' + json.dumps(rules), *mcp]
        if sid:
            command += ['resume', sid]
        before = len(provider.requests)
        out = command_run(command + [f'Fixture wake signal {index}.'], work, dict(os.environ), base, f'wake-{index}')
        events = [json.loads(line) for line in out.splitlines() if line.startswith('{')]
        current = next(e['thread_id'] for e in events if e.get('type') == 'thread.started')
        assert sid is None or current == sid, 'Resume changed session'
        sid = current
        assert any(e.get('type') == 'turn.completed' for e in events), events
        assert len(provider.requests) == before + 1, 'Expected one provider request per wake'
        records.append(record(base, index, provider.requests[-1], rules))
    return version, sid, records


def hermes(args, base, home, work, provider, build_rules):
    import yaml
    from engine import wake
    from hermes_state import SessionDB
    from gateway.session_local_mcp import session_scope, _server_names
    from run_agent import AIAgent
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    sid, records = 'prefix-probe-session', []
    native_db = SessionDB(db_path=home / 'state.db')
    name = 'task_queue'
    if args.runtime == 'hermes-gateway':
        scope = session_scope(SimpleNamespace(profile_id=str(home)), sid)
        name = _server_names(scope, [{'name': 'task_queue'}])[0]
    config = {'model': {'default': 'mock-prefix', 'provider': 'custom', 'base_url': provider.origin + '/v1'},
              'updates': {'check': False}, 'models_dev': {'url': provider.origin + '/models'},
              'skills': {'creation_nudge_interval': 0}, 'memory': {'nudge_interval': 0},
              'display': {'streaming': False}}
    try:
        for index in (1, 2):
            rules = build_rules()
            with patch('engine.auth.bot_token', return_value=f'fixture-credential-{index}'), \
                 patch.object(wake, '_engine_url', return_value=provider.origin):
                server = wake._mcp_server_config('fixture', chat=args.kind == 'chat')
            server['tools'] = {'include': list(wake._MCP_TOOLS)}
            server['lazy'] = False
            config['mcp_servers'] = {name: server}
            (home / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
            discovered = discover_mcp_tools([name])
            assert discovered, 'Native MCP discovery returned no tools'
            from tools.registry import registry
            manifest = registry.get_definitions(set(discovered), quiet=True)
            agent = AIAgent(provider='custom', base_url=provider.origin + '/v1', api_key='fixture-key',
                            api_mode='chat_completions', model='mock-prefix', session_db=native_db,
                            session_id=sid, quiet_mode=True, platform='cli', cwd=str(work),
                            enabled_toolsets=[f'mcp-{name}'], ephemeral_system_prompt=rules,
                            skip_context_files=True, skip_memory=True, skip_background_review=True,
                            max_iterations=2)
            try:
                history = native_db.get_resume_conversations(sid)[0] if index > 1 else []
                if index > 1:
                    assert 'Fixture wake signal 1.' in canonical(history), 'Prior user turn missing from persisted history'
                    assert any(m.get('role') == 'assistant' for m in history), 'Prior reply missing from persisted history'
                before = len(provider.requests)
                if index > 1:
                    with patch.object(agent, '_build_system_prompt', side_effect=AssertionError('Resume rebuilt system prompt')):
                        result = agent.run_conversation(f'Fixture wake signal {index}.', conversation_history=history)
                else:
                    result = agent.run_conversation(f'Fixture wake signal {index}.', conversation_history=history)
                assert result.get('final_response') == 'OK', str(result)[:500]
                assert len(provider.requests) == before + 1, 'Expected one provider request per wake'
                records.append(record(base, index, provider.requests[-1], rules, manifest))
                assert native_db.get_session(sid)['system_prompt'], 'System text was not persisted'
            finally:
                agent.close()
                shutdown_mcp_servers()
    finally:
        native_db.close()
        shutdown_mcp_servers()
        from hermes_logging import release_profile_log_handlers
        release_profile_log_handlers(home)
        import logging
        logging.shutdown()
    version = subprocess.check_output(['git', '-C', str(args.hermes_source), 'rev-parse', 'HEAD'], text=True).strip()
    return version, sid, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=('codex', 'hermes-subprocess', 'hermes-gateway'), required=True)
    parser.add_argument('--kind', choices=('work', 'chat'), default='work')
    parser.add_argument('--codex-bin', type=Path)
    parser.add_argument('--hermes-source', type=Path)
    parser.add_argument('--mcp-python', type=Path, required=True, help='Interpreter with bot-group MCP dependencies')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not args.mcp_python.is_file():
        parser.error('--mcp-python must name an existing interpreter')
    if args.runtime == 'codex' and not (args.codex_bin and args.codex_bin.is_file()):
        parser.error('--codex-bin must name the native executable')
    if args.runtime.startswith('hermes') and not (args.hermes_source and (args.hermes_source / 'run_agent.py').is_file()):
        parser.error('--hermes-source must name installed Hermes source')
    base = args.evidence.resolve()
    if base.exists():
        parser.error('Use a new evidence directory')
    base.mkdir(parents=True)
    # Preserve only OS necessities; no user runtime/provider credentials or execution context.
    allowed = {'path', 'systemroot', 'systemdrive', 'windir', 'comspec', 'pathext', 'temp', 'tmp', 'lang', 'tz'}
    env = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    with tempfile.TemporaryDirectory(prefix='wake-prefix-') as folder, patch.dict(os.environ, env, clear=True):
        home, work = Path(folder) / 'home', Path(folder) / 'work'
        home.mkdir(); work.mkdir()
        os.environ.update(HERMES_HOME=str(home), CODEX_HOME=str(home),
                          USERPROFILE=str(home), HOME=str(home), LOCALAPPDATA=str(home / 'local'),
                          APPDATA=str(home / 'roaming'), PROGRAMDATA=str(home / 'programdata'),
                          GROUP_DB=str(Path(folder) / 'group.db'), GROUP_DATA_DIR=folder,
                          GROUP_ADMIN_PASSWORD='', HERMES_DISABLE_LAZY_INSTALLS='1',
                          TERMINAL_CWD=str(work), NO_PROXY='127.0.0.1,localhost', PYTHONUTF8='1')
        sys.path.insert(0, str(ROOT))
        if args.hermes_source:
            sys.path.insert(0, str(args.hermes_source.resolve()))
        from engine import db, wake
        wake._MCP_PYTHON = str(args.mcp_python.resolve())
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)',
                         ('prefix-probe', str(work), db.now_iso()))
            conn.commit()
        finally:
            conn.close()
        provider = Provider()
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            build_rules = lambda: wake.wake_system_prompt(args.kind, 'prefix-probe', str(work))
            function = codex if args.runtime == 'codex' else hermes
            version, sid, records = function(args, base, home, work, provider, build_rules)
            result = {'runtime': args.runtime, 'kind': args.kind, 'version': version, 'session_id': sid,
                      'wakes': records, 'stable': records[0] == records[1],
                      'boundary': 'native runtime, real MCP discovery, synthetic loopback model; Hermes gateway uses native session MCP naming with AIAgent/SessionDB, WS admission covered separately'}
            save(base, 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
            assert result['stable'], 'Prefix changed; inspect both captured requests'
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        finally:
            provider.shutdown(); provider.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    main()
