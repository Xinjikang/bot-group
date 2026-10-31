"""req#137 task#265：用 dsh_auth 模块验证 API-key 路由（.env / 界面保存凭据两种来源）。

按用户 9669 从主 profile 引用 API key，在隔离目录建 API 调用 profile（provider=
deepseek-official + dsh-llm-deepseek-api-key 适配器），分别经 .env 注入与原生保存凭据
验证实际调用与 401 根因。只输出脱敏结果；原配置哈希保持一致。

provider 名与 401 根因依据 0.2.1-rc.5 实装源码：
- API-key 路由 provider = ``deepseek-official``（dsh-llm-deepseek 传输 + dsh-llm-deepseek-api-key 鉴权，
  请求带 x-api-key，官方端点 https://api.deepseek.com/anthropic，模型列表 GET /models）；
- 账号路由 provider = ``deepseek-account``。
运行：python -X utf8 -B scripts/verify_dsh_auth_apikey.py --exe <exe> --cli <cli> --source-home <主DSH_HOME> --test-directory <隔离目录> --evidence <证据路径>
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_auth  # noqa: E402

PROMPT = "Remember this test marker: PROBE_APK_773. Reply with exactly DSH_NATIVE_OK. Do not use tools."
# API-key 路由的隔离 profile patch（provider=deepseek-official + 官方 API-key 适配器）
PATCH = (
    "- id: agent-default-model\n"
    "  name: '@deepseek-ai/dsh-agent-default-model'\n"
    "  config:\n    provider: deepseek-official\n    model: deepseek-chat\n"
    "- id: llm-deepseek\n"
    "  name: '@deepseek-ai/dsh-llm-deepseek-api-key'\n"
    "  config:\n    apiKeyEnv: DEEPSEEK_API_KEY\n"
)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sanitize(text, secret=''):
    t = re.sub(r'(?i)(sk-[A-Za-z0-9_-]+|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)', '[redacted]', str(text))
    if secret:
        t = t.replace(secret, '[redacted]')
    return t[:1000]


def run_dsh(exe, cli, overlay_path, home, test_dir, secret):
    cmd = [exe, '--expose-internals', cli, '--profile', 'headless',
           '--patch', str(overlay_path), '--json', PROMPT]
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP")
           if k in os.environ}
    env['ELECTRON_RUN_AS_NODE'] = '1'
    env['DSH_HOME'] = str(home)
    env.pop('DEEPSEEK_API_KEY', None)
    p = subprocess.Popen(cmd, cwd=str(test_dir), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        out, err = p.communicate(timeout=150)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out, err = p.communicate(timeout=10)
    events = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                events.append(e)
        except ValueError:
            pass
    final = next((e for e in reversed(events) if e.get('type') == 'final'), {})
    answer = final.get('text', final.get('answer', ''))
    return {'exit_code': p.returncode, 'timed_out': timed_out,
            'answer': answer if answer == 'DSH_NATIVE_OK' else '[unexpected]',
            'auth_error': dsh_auth.is_auth_error(final),
            'stderr_tail': sanitize(err[-600:], secret)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', type=Path, required=True)
    ap.add_argument('--cli', type=Path, required=True)
    ap.add_argument('--source-home', type=Path, required=True)
    ap.add_argument('--test-directory', type=Path, required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    args = ap.parse_args()
    if not args.exe.is_file():
        raise ValueError(f'--exe 不是文件：{args.exe}')
    if not args.source_home.is_dir():
        raise ValueError(f'--source-home 不是目录：{args.source_home}')
    if not args.test_directory.is_dir():
        raise ValueError('--test-directory 必须是已分配的隔离目录')

    cred_file = args.source_home / '.credentials.yaml'
    cred = yaml.safe_load(cred_file.read_text(encoding='utf-8-sig'))
    api_key = (cred.get('refs', {}) or {}).get('DEEPSEEK_API_KEY', '')
    source_files = [cred_file]
    before = {str(p): fingerprint(p) for p in source_files}
    result = {'provider': 'deepseek-official', 'api_key_present': bool(api_key)}

    # .env 来源：隔离 HOME 的 .env 注入 key
    with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
        home = Path(tmp)
        prof = home / 'profiles' / 'apikey'
        prof.mkdir(parents=True)
        (prof / 'cordis.patch.yml').write_text(PATCH, encoding='utf-8')
        (home / '.env').write_text(f'DEEPSEEK_API_KEY={api_key}\n', encoding='utf-8')
        bot = {'config_root': str(home), 'profile': 'apikey'}
        info = dsh_auth.resolve(bot)
        result['env_route'] = info['route']
        result['env_api_key_source'] = info['api_key_source']
        overlay = dsh_auth.overlay_entries(bot)
        overlay_path = home / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result['env_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path,
                                     home, args.test_directory, api_key)

    # 界面保存凭据来源：把主 profile .credentials.yaml（含 refs.DEEPSEEK_API_KEY）复制为隔离 home 的 .credentials.yaml
    with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
        home = Path(tmp)
        prof = home / 'profiles' / 'apikey'
        prof.mkdir(parents=True)
        (prof / 'cordis.patch.yml').write_text(PATCH, encoding='utf-8')
        (home / '.credentials.yaml').write_bytes(cred_file.read_bytes())
        bot = {'config_root': str(home), 'profile': 'apikey'}
        info = dsh_auth.resolve(bot)
        result['native_api_key_source'] = info['api_key_source']
        (home / 'private').mkdir()
        cred = dsh_auth.copy_credentials(str(home), str(home / 'private'))
        overlay = dsh_auth.overlay_entries(bot, credentials_path=str(cred))
        overlay_path = home / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result['native_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path,
                                        home, args.test_directory, api_key)

    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
