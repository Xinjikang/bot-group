"""A controlled model boundary listening on the remote plugin's real gateway WS path."""
from contextlib import contextmanager
import json
import os
import threading
from uuid import uuid4

from websockets.sync.server import serve


def isolated_git_origin(url, repository):
    """Route the fixture URL to a temporary bare repository, preserving inherited Git options."""
    index = int(os.environ.get('GIT_CONFIG_COUNT', '0'))
    return {'GIT_ALLOW_PROTOCOL': 'file', 'GIT_CONFIG_COUNT': str(index + 1),
            f'GIT_CONFIG_KEY_{index}': 'url.' + repository.as_uri() + '.insteadOf',
            f'GIT_CONFIG_VALUE_{index}': url}


class DispatchGateway:
    def __init__(self, member, home):
        self.member = member
        self.home = home
        self.sessions = {}
        self.submissions = []
        self.submit_requests = []
        self.errors = []
        self._lock = threading.Lock()

    @contextmanager
    def listen(self):
        with serve(self._receive, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
            self.port = server.socket.getsockname()[1]
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                yield self
            finally:
                server.shutdown()
                worker.join(timeout=5)
                assert not worker.is_alive(), 'isolated gateway listener did not stop'

    def fetch_ticket(self, home, *, purpose):
        assert purpose == 'interactive' and home == str(self.home), (purpose, home)
        return {'api_origin': f'http://127.0.0.1:{self.port}'}, 'isolated-fixture-ticket'

    def _receive(self, ws):
        assert ws.request.path == '/api/ws' and ws.subprotocol == 'hermes-gateway-v1'
        for raw in ws:
            req = json.loads(raw)
            with self._lock:
                try:
                    result = self._rpc(req['method'], req.get('params') or {})
                except (KeyError, ValueError, AssertionError) as exc:
                    # Do not expose RPC parameters, which can carry session credentials.
                    self.errors.append((req['method'], type(exc).__name__))
                    ws.send(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'error': {
                        'code': -32602, 'message': 'isolated gateway received unexpected RPC'}}))
                    continue
            ws.send(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}, ensure_ascii=False))

    def _rpc(self, method, args):
        if method == 'runtime.describe':
            return {'profile_id': self.member, 'capabilities': ['durable-admission-v1', 'acp-session-mcp-v1']}
        if method == 'session.list':
            return {'sessions': [s['snapshot'] for s in self.sessions.values()
                                 if s['snapshot']['info']['title'] == args['title']]}
        if method == 'session.create':
            sid = uuid4().hex
            snapshot = {'session_id': sid, 'revision': 0, 'execution_generation': 0,
                        'info': {'title': args['title'], 'source': 'acp', 'cwd': args['cwd']},
                        'running': False, 'pending': [], 'messages': [], 'prompts': [],
                        'subscription_id': uuid4().hex}
            self.sessions[sid] = {'snapshot': snapshot, 'submission': None}
            return snapshot.copy()
        sid = args['session_id']
        session = self.sessions[sid]
        snapshot = session['snapshot']
        if method == 'session.resume':
            return snapshot.copy()
        if method == 'session.mutate':
            op, payload = args['operation'], args['payload']
            assert op in ('editor_mcp', 'workspace', 'edit_policy', 'model', 'reasoning'), op
            if op == 'editor_mcp':
                session['mcp'] = payload['mcp_servers'][0]
            if op == 'edit_policy':
                snapshot['info']['edit_approval_policy'] = payload['policy']
            if op == 'workspace':
                snapshot['info']['cwd'] = payload['cwd']
            snapshot['revision'] += 1
            return {'mcp_servers': ['task_queue']}
        if method == 'prompt.submit':
            self.submit_requests.append(args['submission_id'])
            assert session['submission'] is None and session.get('mcp') is not None
            received = {'session_id': sid, 'submission_id': args['submission_id'], 'text': args['text'],
                        'admission_id': uuid4().hex, 'done': False}
            session['submission'] = received
            self.submissions.append(received)
            snapshot['running'] = True
            snapshot['execution_generation'] = 1
        elif method != 'prompt.receipt':
            raise ValueError('unsupported RPC')
        received = session['submission']
        assert received and (method != 'prompt.receipt' or args['admission_id'] == received['admission_id'])
        return {'ref': {'profile_id': self.member, 'session_id': sid}, 'admission_id': received['admission_id'],
                'input_id': received['submission_id'], 'authority_epoch': 1, 'execution_generation': 1,
                'status': 'terminal' if received['done'] else 'started',
                'outcome': 'completed' if received['done'] else None}

    def settled(self, received):
        with self._lock:
            session = self.sessions[received['session_id']]
            assert session['submission'] is received
            received['done'] = True
            session['snapshot']['running'] = False
