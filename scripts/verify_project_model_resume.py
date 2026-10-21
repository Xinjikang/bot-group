"""Probe installed Codex against a loopback fake provider and disposable home."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            message = dict(id='msg_mock', type='message', role='assistant', status='completed',
                           content=[dict(type='output_text', text='OK', annotations=[])])
            response = dict(id='resp_mock', object='response', status='completed', output=[message],
                            usage=dict(input_tokens=1, output_tokens=1, total_tokens=2))
            events = [dict(type='response.output_item.done', output_index=0, item=message),
                      dict(type='response.completed', response=response)]
            body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='project-model-resume-') as folder:
            home = Path(folder)
            (home / 'config.toml').write_text(
                'model = "mock-default"\nmodel_provider = "mock"\n'
                '[model_providers.mock]\nname = "mock"\nwire_api = "responses"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                'requires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n', encoding='utf-8')
            env = dict(os.environ, CODEX_HOME=folder)
            cmd = [shutil.which('codex'), 'exec', '--json', '--skip-git-repo-check']
            first = subprocess.run(cmd + ['--model', 'mock-project', 'Say OK'], cwd=folder,
                                   env=env, capture_output=True, text=True, timeout=45)
            events = [json.loads(line) for line in first.stdout.splitlines() if line.startswith('{')]
            ids = [event['thread_id'] for event in events if event.get('type') == 'thread.started']
            assert ids and requests, (first.returncode, first.stdout[-1000:], first.stderr[-1000:])
            assert requests[-1]['model'] == 'mock-project', requests[-1]['model']
            count = len(requests)
            resumed = subprocess.run(cmd + ['resume', ids[0], 'Say OK'], cwd=folder,
                                     env=env, capture_output=True, text=True, timeout=45)
            assert len(requests) > count, (resumed.returncode, resumed.stdout[-1000:], resumed.stderr[-1000:])
            assert requests[-1]['model'] == 'mock-default', requests[-1]['model']
            count = len(requests)
            changed = subprocess.run(cmd + ['--model', 'mock-changed', 'resume', ids[0], 'Say OK'], cwd=folder,
                                     env=env, capture_output=True, text=True, timeout=45)
            assert len(requests) > count, (changed.returncode, changed.stdout[-1000:], changed.stderr[-1000:])
            assert requests[-1]['model'] == 'mock-changed', requests[-1]['model']
            print('Native Codex resume: project model, clear to current default, new explicit override confirmed')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
