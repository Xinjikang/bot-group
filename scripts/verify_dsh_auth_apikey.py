"""req#137 task#265：用 dsh_auth 模块验证 API-key 路由（.env / 界面保存凭据两种来源）。

在隔离目录建 API 调用 profile（provider/model/baseURL 由参数注入，凭据来源文件/键名由参数
注入），分别经 .env 注入与原生保存凭据验证实际调用与失败根因；账号路由沿用独立账号 profile。
只输出脱敏结果；原配置哈希保持一致。

API-key 路由 provider 为 deepseek-official（0.2.1-rc.5 实装 OWNER_PACKAGES：
deepseek-official → dsh-llm-deepseek-api-key，账号路由 deepseek-account → dsh-llm-deepseek-account）。
baseURL 解析序：显式 --api-base-url → DSH 适配器原生默认（https://api.deepseek.com/anthropic）。
失败分类：missing_credential（凭据缺失）/ auth（401 等 key 被端点拒绝）/ init（初始化失败），
诊断据分类区分，不把任意 auth_error/invalid 字符串一律归因 key-invalid。

运行：python -X utf8 -B scripts/verify_dsh_auth_apikey.py --exe <exe> --cli <cli> \
  --account-home <账号DSH_HOME> --account-profile <账号profile> \
  --api-key-file <凭据来源文件> --api-key-name <键名> \
  --api-provider <provider> --api-model <model> --api-base-url <baseURL> \
  --test-directory <隔离目录> --evidence <证据路径>
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

# API-key 路由 provider 名（0.2.1-rc.5 实装 OWNER_PACKAGES）
_API_KEY_PROVIDER = 'deepseek-official'
# DSH API-key 适配器原生默认端点（0.2.1-rc.5 实装 PUBLIC_BASE_URL；显式 baseURL 优先）
_DEFAULT_API_BASE_URL = 'https://api.deepseek.com/anthropic'
# 所选适配器条目 id（baseURL 取自所选适配器配置，非任意插件）
_ADAPTER_IDS = ('llm-deepseek', 'llm-deepseek-account')
# overlay 三服务（账号路由恢复 credentials/authorization/deepseek-account）
_SERVICE_IDS = ('credentials', 'authorization', 'deepseek-account')


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


def _read_profile_config(source_home, profile):
    """从指定 profile 的 cordis.patch.yml 读 agent-default-model 的 provider/model 与所选适配器的 config.baseURL。

    只解析显式选定的 profile（不跨 profiles/* 混取）；baseURL 取自所选适配器
    （llm-deepseek / llm-deepseek-account）配置，非任意插件的 config.baseURL。
    返回 ``(provider, model, baseurl)``；仅键名/非凭据值。
    """
    patch = Path(source_home) / 'profiles' / profile / 'cordis.patch.yml'
    provider = model = baseurl = ''
    if patch.is_file():
        try:
            doc = yaml.safe_load(patch.read_text(encoding='utf-8-sig'))
        except Exception:
            doc = []
        for entry in doc if isinstance(doc, list) else []:
            if not isinstance(entry, dict):
                continue
            cfg = entry.get('config') if isinstance(entry.get('config'), dict) else {}
            eid = str(entry.get('id') or '')
            if eid == 'agent-default-model':
                provider = cfg.get('provider') or provider
                model = cfg.get('model') or model
            elif eid in _ADAPTER_IDS and cfg.get('baseURL'):
                baseurl = cfg['baseURL']
    return provider, model, baseurl


def _success(call) -> bool:
    """成功判定：DSH_NATIVE_OK + exit0 + 未超时 + 无失败（missing_credential/auth/init）。"""
    return (call.get('answer') == 'DSH_NATIVE_OK'
            and call.get('exit_code') == 0
            and not call.get('timed_out')
            and call.get('failure_kind') is None)


def _attribution(env_kind, native_kind):
    """API-key 两源失败归因：区分 missing_credential / auth / init，不把任意失败归因 key-invalid。"""
    kinds = {k for k in (env_kind, native_kind) if k}
    if kinds == {'auth'}:
        return 'key-invalid'
    if kinds == {'missing_credential'}:
        return 'missing-credential'
    if kinds == {'init'}:
        return 'init-error'
    if not kinds:
        return None
    return 'mixed'


def _overlay_evidence(overlay):
    """overlay 构建证据：按 id 取三服务（credentials/authorization/deepseek-account），
    disabled 取条目顶层（非 config），不含凭据值。"""
    by_id = {e['id']: e for e in overlay if isinstance(e, dict) and e.get('id')}
    out = []
    for sid in _SERVICE_IDS:
        e = by_id.get(sid)
        if e is None:
            out.append({'id': sid, 'present': False})
        else:
            out.append({'id': sid, 'name': e.get('name'), 'disabled': e.get('disabled'),
                        'provider': (e.get('config') or {}).get('provider')})
    return out


def assess(account_call, env_call, native_call, account_back_call, source_binding, source_files_unchanged, routes):
    """四态合成判定：账号 + 两 API-key + 切回账号调用 → 整体结论。

    - switch：resolve 路由正确（account→apikey→apikey→account，含切回）+ 四段 run_dsh 均实际调用 + 切回 account 成功；
    - diagnosis_ok：401 根因查明（service 已确认 + key 存在 + 两 API-key 源均 auth 失败），单列不并入 passed；
    - passed：账号 + 两 API-key 三来源均成功（exit0/未超时/有效 final/无失败）+ 切换 + 原配置不变。
    """
    account_route, env_route, native_route, account_back_route = routes
    account_ok = _success(account_call)
    env_ok = _success(env_call)
    native_ok = _success(native_call)
    account_back_ok = _success(account_back_call)
    switch = (account_route == 'account' and env_route == 'apikey' and native_route == 'apikey'
              and account_back_route == 'account'
              and bool(account_call) and bool(env_call) and bool(native_call)
              and account_back_ok)  # 切回 account 成功
    diagnosis_ok = (
        source_binding.get('service_confirmed') is True
        and source_binding.get('api_key_present') is True
        and env_call.get('failure_kind') == 'auth'
        and native_call.get('failure_kind') == 'auth'  # 两 API-key 源 401 = key 对已确认端点无效
    )
    passed = account_ok and env_ok and native_ok and account_back_ok and switch and source_files_unchanged
    return {'account_ok': account_ok, 'env_ok': env_ok, 'native_ok': native_ok,
            'account_back_ok': account_back_ok,
            'switch_account_to_apikey': switch, 'diagnosis_ok': diagnosis_ok, 'passed': passed}


def _api_key_patch(provider, model, base_url, api_key_name):
    """按 provider/baseURL/model 生成隔离 profile patch（apiKeyEnv 取凭据键名）。"""
    return (
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        f"  config:\n    provider: {provider}\n    model: {model}\n"
        "- id: llm-deepseek\n"
        "  name: '@deepseek-ai/dsh-llm-deepseek-api-key'\n"
        f"  config:\n    apiKeyEnv: {api_key_name}\n    baseURL: {base_url}\n"
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
    failure_kind = dsh_auth.classify_auth_error(final, err)
    return {'exit_code': p.returncode, 'timed_out': timed_out,
            'answer': answer if answer == 'DSH_NATIVE_OK' else '[unexpected]',
            'failure_kind': failure_kind,
            'stderr_tail': sanitize(err[-600:], secret)}


def _run_account(exe, cli, account_home, account_profile, test_directory, secret):
    """账号路由实际执行（resolve + copy_credentials + overlay_entries + run_dsh）。

    返回 (route, overlay_evidence, call)，供切换序列的首段与切回段复用。
    """
    bot = {'config_root': str(account_home), 'profile': account_profile}
    info = dsh_auth.resolve(bot)
    with tempfile.TemporaryDirectory(prefix='dsh-acc-', dir=test_directory) as tmp:
        private = Path(tmp)
        cred = dsh_auth.copy_credentials(str(account_home), str(private))
        overlay = dsh_auth.overlay_entries(bot, credentials_path=str(cred))
        overlay_path = private / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        call = run_dsh(str(exe), str(cli), overlay_path, test_directory, test_directory, secret)
    return info['route'], _overlay_evidence(overlay), call


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', type=Path, required=True)
    ap.add_argument('--cli', type=Path, required=True)
    ap.add_argument('--account-home', type=Path, required=True)     # 账号路由 DSH_HOME
    ap.add_argument('--account-profile', default='desktop')         # 账号 profile 名
    ap.add_argument('--api-key-file', type=Path, required=True)     # 凭据来源文件（.env）
    ap.add_argument('--api-key-name', default='DEEPSEEK_API_KEY')  # 凭据键名
    ap.add_argument('--api-provider', default='deepseek-official') # API provider
    ap.add_argument('--api-model', default='deepseek-flash')        # API model
    ap.add_argument('--api-base-url', default='')                   # 显式 baseURL（空→DSH 原生默认）
    ap.add_argument('--api-profile', default='apikey')             # 隔离 API profile 名
    ap.add_argument('--test-directory', type=Path, required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    args = ap.parse_args()
    if not args.exe.is_file():
        raise ValueError(f'--exe 不是文件：{args.exe}')
    if not args.account_home.is_dir():
        raise ValueError(f'--account-home 不是目录：{args.account_home}')
    if not args.api_key_file.is_file():
        raise ValueError(f'--api-key-file 不是文件：{args.api_key_file}')
    if not args.test_directory.is_dir():
        raise ValueError('--test-directory 必须是已分配的隔离目录')

    # ① 来源配置：读凭据来源正文 + 账号 profile 配置 + API provider/baseURL/model 输入
    api_key = _read_env_key(args.api_key_file, args.api_key_name)  # 仅内存，不输出
    api_provider = args.api_provider
    api_base_url = args.api_base_url or _DEFAULT_API_BASE_URL  # 显式或 DSH 适配器原生默认
    api_model = args.api_model
    service_confirmed = api_provider == _API_KEY_PROVIDER and bool(api_key)

    # 账号 profile 配置（显式选定 profile，不跨 profiles 混取）
    account_provider, account_model, account_baseurl = _read_profile_config(
        args.account_home, args.account_profile)

    source_files = [args.api_key_file,
                    args.account_home / 'profiles' / args.account_profile / 'cordis.patch.yml',
                    args.account_home / '.credentials.yaml',
                    args.account_home / '.env']
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}
    result = {
        'api_key_present': bool(api_key),
        'source_binding': {
            'api_key_file': str(args.api_key_file),   # 凭据来源文件（外部路径）
            'api_key_name': args.api_key_name,        # 凭据键名
            'api_key_present': bool(api_key),         # 凭据正文是否存在
            'api_provider': api_provider,             # API provider（参数注入）
            'api_model': api_model,                   # API model（参数注入）
            'api_base_url': api_base_url,             # 显式或 DSH 原生默认
            'api_base_url_explicit': bool(args.api_base_url),
            'api_profile': args.api_profile,           # 隔离 API profile 名
            'account_home': str(args.account_home),    # 账号路由 HOME
            'account_profile': args.account_profile,   # 账号 profile 名
            'account_provider': account_provider,      # 账号 profile 的 provider（来源证据）
            'account_model': account_model,            # 账号 profile 的 model（来源证据）
            'service_confirmed': service_confirmed,     # provider 选中 API-key 路由 + key 存在
        },
    }

    # 无法确认 API-key 路由（provider 非 deepseek-official 或凭据缺失）→ 停请求
    if not service_confirmed:
        result['note'] = ('API-key 路由未确认：provider=%r 非 %r 或凭据缺失'
                          % (api_provider, _API_KEY_PROVIDER))
        result['auth_attribution'] = 'unknown'
        result['passed'] = False
        result['diagnosis_ok'] = False
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    # ② 切换序列：账号路由（首段）→ API-key 路由（env/native）→ 账号路由切回（末段）
    account_route, account_overlay_ev, account_call = _run_account(
        args.exe, args.cli, args.account_home, args.account_profile, args.test_directory, api_key)
    result['account_route'] = account_route  # 切换证据：resolve 路由
    result['account_overlay'] = account_overlay_ev  # 切换证据：overlay 构建
    result['account_call'] = account_call

    # ③ .env 来源：隔离 HOME 写前限权，.env 注入 key（来自凭据来源文件正文）
    with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
        home = Path(tmp)
        dsh_auth.restrict_dir(str(home))  # 写敏感内容前限权
        prof = home / 'profiles' / args.api_profile
        prof.mkdir(parents=True)
        (prof / 'cordis.patch.yml').write_text(
            _api_key_patch(api_provider, api_model, api_base_url, args.api_key_name), encoding='utf-8')
        (home / '.env').write_text(f'{args.api_key_name}={api_key}\n', encoding='utf-8')
        bot = {'config_root': str(home), 'profile': args.api_profile}
        info = dsh_auth.resolve(bot)
        result['env_api_key_source'] = info['api_key_source']
        result['env_route'] = info['route']  # 切换证据：resolve 路由
        overlay = dsh_auth.overlay_entries(bot)
        result['env_overlay'] = _overlay_evidence(overlay)  # 切换证据：overlay 构建
        overlay_path = home / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result['env_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path, home,
                                     args.test_directory, api_key)

    # ④ 界面保存凭据来源：写 refs（key 正文）+ copy_credentials 私有副本 + native 路由
    with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
        home = Path(tmp)
        dsh_auth.restrict_dir(str(home))
        prof = home / 'profiles' / args.api_profile
        prof.mkdir(parents=True)
        (prof / 'cordis.patch.yml').write_text(
            _api_key_patch(api_provider, api_model, api_base_url, args.api_key_name), encoding='utf-8')
        (home / '.credentials.yaml').write_text(
            f'version: 1\nrefs:\n  {args.api_key_name}: {api_key}\n', encoding='utf-8')
        bot = {'config_root': str(home), 'profile': args.api_profile}
        info = dsh_auth.resolve(bot)
        result['native_api_key_source'] = info['api_key_source']
        result['native_route'] = info['route']  # 切换证据：resolve 路由
        (home / 'private').mkdir()
        cred = dsh_auth.copy_credentials(str(home), str(home / 'private'))
        overlay = dsh_auth.overlay_entries(bot, credentials_path=str(cred))
        result['native_overlay'] = _overlay_evidence(overlay)  # 切换证据：overlay 构建
        overlay_path = home / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        result['native_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path, home,
                                        args.test_directory, api_key)

    # ⑤ 切回账号路由（末段）：API-key 验证后切回 account，纳入 switch
    account_back_route, account_back_overlay_ev, account_back_call = _run_account(
        args.exe, args.cli, args.account_home, args.account_profile, args.test_directory, api_key)
    result['account_back_route'] = account_back_route
    result['account_back_overlay'] = account_back_overlay_ev
    result['account_back_call'] = account_back_call

    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files if p.is_file()}
    # 切换证据：resolve 路由 + overlay 构建 + 切回序列（account→apikey→apikey→account）
    result['switch_sequence'] = [result['account_route'], result['env_route'],
                                 result['native_route'], result['account_back_route']]
    result.update(assess(account_call, result['env_call'], result['native_call'], account_back_call,
                         result['source_binding'], result['source_files_unchanged'],
                         result['switch_sequence']))
    # 失败归因：区分 missing_credential / auth / init（不把任意 auth_error 归因 key-invalid）
    result['auth_attribution'] = _attribution(
        result['env_call'].get('failure_kind'), result['native_call'].get('failure_kind'))
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
