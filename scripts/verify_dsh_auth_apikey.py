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

PROMPT = "Remember this test marker: PROBE_APK_991. Reply with exactly DSH_NATIVE_OK. Do not use tools."
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sanitize(text, secret=''):
    t = re.sub(r'(?i)(sk-[A-Za-z0-9_-]+|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)', '[redacted]', str(text))
    if secret:
        t = t.replace(secret, '[redacted]')
    return t[:1000]


def _read_env_key(env_path: Path, key: str) -> str:
    """读 .env 的指定键值（不输出）。"""
    if not env_path.is_file():
        return ''
    for line in env_path.read_text(encoding='utf-8-sig').splitlines():
        k, _, v = line.partition('=')
        if k.strip() == key:
            return v.strip()
    return ''


def _stderr_auth(stderr: str) -> bool:
    """识别 stderr 里的认证失败（AUTH / MISSING_CREDENTIAL / 401 / invalid key）。"""
    t = (stderr or '').lower()
    return ('authentication fails' in t or 'missing_credential' in t
            or 'missing credential' in t or '401' in t or 'invalid' in t)


def _success(call) -> bool:
    """成功判定：DSH_NATIVE_OK + exit0 + 未超时 + 无认证失败（有效 final 由 answer 隐含）。"""
    return (call.get('answer') == 'DSH_NATIVE_OK'
            and call.get('exit_code') == 0
            and not call.get('timed_out')
            and not call.get('auth_error'))


def _api_key_patch(provider, model, base_url):
    """按核对的 provider/baseURL/model 生成隔离 profile patch。"""
    return (
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        f"  config:\n    provider: {provider}\n    model: {model}\n"
        "- id: llm-deepseek\n"
        "  name: '@deepseek-ai/dsh-llm-deepseek-api-key'\n"
        f"  config:\n    apiKeyEnv: DEEPSEEK_API_KEY\n    baseURL: {base_url}\n"
    )


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
    auth_error = dsh_auth.is_auth_error(final) or _stderr_auth(err)
    return {'exit_code': p.returncode, 'timed_out': timed_out,
            'answer': answer if answer == 'DSH_NATIVE_OK' else '[unexpected]',
            'auth_error': auth_error,
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

    # ① 来源配置匹配：核对主 profile 的 key 对应 provider/baseURL/model
    cred_file = args.source_home / '.credentials.yaml'
    env_file = args.source_home / '.env'
    patch_file = args.source_home / 'profiles' / 'desktop' / 'cordis.patch.yml'
    cred = yaml.safe_load(cred_file.read_text(encoding='utf-8-sig')) if cred_file.is_file() else {}
    api_key = (cred.get('refs', {}) or {}).get('DEEPSEEK_API_KEY', '')
    base_url_env = _read_env_key(env_file, 'DEEPSEEK_BASE_URL')
    source_provider = source_model = ''
    if patch_file.is_file():
        seq = yaml.safe_load(patch_file.read_text(encoding='utf-8-sig')) or []
        agent = next((e for e in seq if isinstance(e, dict) and e.get('id') == 'agent-default-model'), None)
        if agent and isinstance(agent.get('config'), dict):
            source_provider = str(agent['config'].get('provider') or '')
            source_model = str(agent['config'].get('model') or '')
    # API-key 路由的 provider/baseURL/model：provider 为 DSH 的 API-key 适配器名（dsh-llm-deepseek-api-key，
    # 派生自 0.2.1-rc.5 ASAR 实装，非 key 绑定配置值）；baseURL 读 $DEEPSEEK_BASE_URL 或官方默认；model 为
    # 官方默认 deepseek-chat（key 不绑定具体模型，401 发生在模型列表 GET /models 之前）。
    api_provider = 'deepseek-official'
    api_base_url = base_url_env or DEFAULT_BASE_URL
    api_model = 'deepseek-chat'

    source_files = [cred_file, env_file, patch_file]
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}
    result = {
        'api_key_present': bool(api_key),
        'source_binding': {
            'credentials_ref': 'refs.DEEPSEEK_API_KEY' if api_key else None,
            'config_provider': source_provider,   # 读自 agent-default-model（账号路由，非 key 绑定）
            'config_model': source_model,         # 读自 config（账号模型，非 key 绑定）
            'base_url_env': base_url_env or DEFAULT_BASE_URL,  # 读自 .env（无 $DEEPSEEK_BASE_URL→官方默认）
            'apikey_adapter': api_provider,       # DSH API-key 适配器名（派生自 ASAR，非配置值）
            'apikey_model': api_model,            # 官方默认模型（key 不绑定模型）
        },
    }

    # ② 切换序列：账号路由实际执行（第一段）→ API-key 路由实际执行（后两段）
    account_bot = {'config_root': str(args.source_home), 'profile': 'desktop'}
    account_info = dsh_auth.resolve(account_bot)
    result['account_route'] = account_info['route']
    with tempfile.TemporaryDirectory(prefix='dsh-acc-', dir=args.test_directory) as tmp:
        private = Path(tmp)
        cred = dsh_auth.copy_credentials(str(args.source_home), str(private))
        account_overlay = dsh_auth.overlay_entries(account_bot, credentials_path=str(cred))
        account_overlay_path = private / 'overlay.json'
        account_overlay_path.write_text(json.dumps(account_overlay, ensure_ascii=False), encoding='utf-8')
        account_call = run_dsh(str(args.exe), str(args.cli), account_overlay_path,
                               args.test_directory, args.test_directory, api_key)
    account_ok = _success(account_call)
    result['account_call'] = account_call
    result['account_ok'] = account_ok

    # ③ .env 来源：隔离 HOME 写前限权，.env 注入 key
    with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
        home = Path(tmp)
        dsh_auth.restrict_dir(str(home))  # 写敏感内容前限权
        prof = home / 'profiles' / 'apikey'
        prof.mkdir(parents=True)
        (prof / 'cordis.patch.yml').write_text(_api_key_patch(api_provider, api_model, api_base_url), encoding='utf-8')
        (home / '.env').write_text(f'DEEPSEEK_API_KEY={api_key}\n', encoding='utf-8')
        bot = {'config_root': str(home), 'profile': 'apikey'}
        info = dsh_auth.resolve(bot)
        result['env_api_key_source'] = info['api_key_source']
        overlay = dsh_auth.overlay_entries(bot)
        overlay_path = home / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result['env_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path, home,
                                     args.test_directory, api_key)

    # ④ 界面保存凭据来源：copy_credentials 私有副本 + native 路由
    with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
        home = Path(tmp)
        dsh_auth.restrict_dir(str(home))
        prof = home / 'profiles' / 'apikey'
        prof.mkdir(parents=True)
        (prof / 'cordis.patch.yml').write_text(_api_key_patch(api_provider, api_model, api_base_url), encoding='utf-8')
        (home / '.credentials.yaml').write_bytes(cred_file.read_bytes())
        bot = {'config_root': str(home), 'profile': 'apikey'}
        info = dsh_auth.resolve(bot)
        result['native_api_key_source'] = info['api_key_source']
        (home / 'private').mkdir()
        cred = dsh_auth.copy_credentials(str(home), str(home / 'private'))
        overlay = dsh_auth.overlay_entries(bot, credentials_path=str(cred))
        overlay_path = home / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result['native_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path, home,
                                        args.test_directory, api_key)

    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files if p.is_file()}
    # ⑤ 三来源成功判定（9703 口径）：账号 + 两 API-key 均成功（exit0/未超时/有效 final/无 auth_error）才通过
    env_ok = _success(result['env_call'])
    native_ok = _success(result['native_call'])
    result['env_ok'] = env_ok
    result['native_ok'] = native_ok
    # 切换序列：账号路由成功 + 两 API-key 路由实际执行（账号→API-key）
    result['switch_account_to_apikey'] = account_ok and not env_ok and not native_ok
    # 401 根因查明：单列诊断（不并入整体 passed）
    result['diagnosis_ok'] = (
        result['source_binding']['credentials_ref'] is not None
        and result['env_call']['auth_error'] is True
        and result['native_call']['auth_error'] is True
    )
    result['passed'] = (
        account_ok and env_ok and native_ok
        and result['switch_account_to_apikey']
        and result['source_files_unchanged']
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
