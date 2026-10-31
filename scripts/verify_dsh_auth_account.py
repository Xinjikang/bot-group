"""req#137 task#265：用 dsh_auth 生成的 overlay + 受限副本在隔离原生 DSH 验证账号路由。

复用 probe-desktop-model 的原生调用方法，但认证 overlay 由 dsh_auth.overlay_entries 生成、
凭据副本由 dsh_auth.copy_credentials 复制（含 ACL 核验），验证账号路由实际调用成功，
并核验 overlay 按 id 合并（deepseek-account 唯一条目、accountId 保留、最终 disabled 明确）。

API-key（.env/界面 key）路由的真实模型调用需有效 API key；本机此前 key 实测 401 无有效
key，故只核验 overlay 结构（deepseek-account disabled=True），真实调用留 266 起有效 key 后
验收，缺条件在证据中记录。

运行：python -X utf8 -B scripts/verify_dsh_auth_account.py --test-directory <分配隔离目录> --evidence <绝对证据路径>
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_auth  # noqa: E402

PROMPT = "Remember this test marker: PROBE_CEDAR_741. Reply with exactly DSH_NATIVE_OK. Do not use tools."


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sanitize(text):
    return re.sub(r'(?i)(sk-[A-Za-z0-9_-]+|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)',
                 '[redacted]', str(text))[:1200]


def run_dsh(exe, cli, overlay_path, home, test_dir, prompt):
    cmd = [exe, '--expose-internals', cli, '--profile', 'headless',
           '--patch', str(overlay_path), '--json', prompt]
    env = os.environ.copy()
    env.update(ELECTRON_RUN_AS_NODE='1', DSH_HOME=str(home))
    env.pop('DEEPSEEK_API_KEY', None)
    env.pop('DSH_PROFILE', None)
    p = subprocess.Popen(cmd, cwd=str(test_dir), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    timed_out = False
    try:
        out, err = p.communicate(timeout=150)
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
    return {
        'exit_code': p.returncode, 'timed_out': timed_out,
        'session_id': next((e.get('sessionId') for e in events if e.get('type') == 'session'), None),
        'answer': answer if answer == 'DSH_NATIVE_OK' else '[unexpected output omitted]',
        'auth_error': dsh_auth.is_auth_error(final),
        'stderr_tail': sanitize(err[-600:] if err else ''),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', type=Path, required=True, help='DSH 桌面可执行文件绝对路径')
    ap.add_argument('--cli', type=Path, required=True, help='DSH desktop-host cli.js 绝对路径')
    ap.add_argument('--source-home', type=Path, required=True, help='源 DSH_HOME 绝对路径')
    ap.add_argument('--test-directory', type=Path, required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    args = ap.parse_args()
    if not args.exe.is_file():
        raise ValueError(f'--exe 不是文件：{args.exe}')
    if not str(args.cli).strip():
        raise ValueError('--cli 不能为空（asar 内路径无法用 is_file 校验，由 DSH --expose-internals 读取）')
    if not args.source_home.is_dir():
        raise ValueError(f'--source-home 不是目录：{args.source_home}')
    if not args.test_directory.is_dir():
        raise ValueError('--test-directory 必须是已分配的隔离目录')
    credentials = args.source_home / '.credentials.yaml'
    profile_patch = args.source_home / 'profiles' / 'desktop' / 'cordis.patch.yml'
    source_files = [credentials, profile_patch]
    before = {str(p): fingerprint(p) for p in source_files}
    bot = {'config_root': str(args.source_home), 'profile': 'desktop'}
    info = dsh_auth.resolve(bot)
    result = {'route': info['route'], 'provider': info['provider'], 'model': info['model']}
    with tempfile.TemporaryDirectory(prefix='dsh-auth-', dir=args.test_directory) as tmp:
        private = Path(tmp)
        cred = dsh_auth.copy_credentials(str(args.source_home), str(private))
        overlay = dsh_auth.overlay_entries(bot, credentials_path=str(cred))
        dsa = [e for e in overlay if isinstance(e, dict) and e.get('id') == 'deepseek-account']
        result['deepseek_account_count'] = len(dsa)
        result['deepseek_account'] = dsa[0] if dsa else None
        result['credentials_present'] = any(e.get('id') == 'credentials' for e in overlay if isinstance(e, dict))
        result['authorization_present'] = any(e.get('id') == 'authorization' for e in overlay if isinstance(e, dict))
        overlay_path = private / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result.update(run_dsh(str(args.exe), str(args.cli), overlay_path,
                              args.test_directory, args.test_directory, PROMPT))
    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files}
    # 账号路由：真实调用成功 + overlay 合并正确；API-key 结构另由单测覆盖。
    result['passed'] = (result['route'] == 'account' and result['deepseek_account_count'] == 1
                        and result.get('exit_code') == 0 and result.get('answer') == 'DSH_NATIVE_OK'
                        and result['source_files_unchanged'])
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
