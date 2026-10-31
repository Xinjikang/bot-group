"""req#137 task#265：用 dsh_auth 模块验证 API-key 路由（.env / 界面保存凭据两种来源）。

按用户 9669 从主 profile 引用 API key，在隔离目录建 API 调用 profile，分别经 .env 注入
与原生保存凭据验证实际调用与 401 根因。只输出脱敏结果；原配置哈希保持一致。

API-key 路由 provider/baseURL 从主 profile 实际配置解析（非硬套官方默认）：
- provider 须为 agent-default-model.provider == ``deepseek-official``（0.2.1-rc.5 实装
  OWNER_PACKAGES：deepseek-official → dsh-llm-deepseek-api-key，账号路由为 deepseek-account）；
- baseURL：显式 config.baseURL 优先，其次 $DEEPSEEK_BASE_URL，均缺 → 未确认。
无法确认时记 unknown 并停止请求，不得直接归因 key-invalid。
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


_DEFAULT_API_MODEL = 'deepseek-chat'  # 官方默认模型（config 无模型时的兜底，非 provider/baseURL 绑定）
# API-key 路由 provider 名（0.2.1-rc.5 实装 OWNER_PACKAGES：deepseek-official → dsh-llm-deepseek-api-key）
_API_KEY_PROVIDER = 'deepseek-official'
# overlay 三服务（账号路由恢复 credentials/authorization/deepseek-account）
_SERVICE_IDS = ('credentials', 'authorization', 'deepseek-account')


def _read_profile_config(source_home):
    """从主 profile 的 cordis.patch.yml 读 agent-default-model 的 provider/model 与任意插件的 config.baseURL。

    返回 ``(provider, model, baseurl)``；仅键名/非凭据值。
    """
    provider = model = baseurl = ''
    for p in (Path(source_home) / 'profiles').glob('*/cordis.patch.yml'):
        try:
            doc = yaml.safe_load(p.read_text(encoding='utf-8-sig'))
        except Exception:
            continue
        for entry in doc if isinstance(doc, list) else []:
            if not isinstance(entry, dict):
                continue
            cfg = entry.get('config') or {}
            if not isinstance(cfg, dict):
                continue
            if entry.get('id') == 'agent-default-model':
                provider = cfg.get('provider') or provider
                model = cfg.get('model') or model
            if cfg.get('baseURL'):
                baseurl = cfg['baseURL']
    return provider, model, baseurl


def _api_key_provider(profile_provider):
    """API-key 路由 provider 须从主 profile 实际配置确认（非注册表硬套）。

    仅当 agent-default-model.provider 为官方 API-key 路由（deepseek-official）时确认；
    账号路由（deepseek-account）/缺省 → None（API-key 路由未选中，unknown）。
    """
    return _API_KEY_PROVIDER if profile_provider == _API_KEY_PROVIDER else None


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
    - diagnosis_ok：401 根因查明（API-key 路由 provider/baseURL 从配置确认 + key 存在 + 两 API-key 路认证失败），单列不并入 passed；
    - passed：账号 + 两 API-key 三来源均成功（exit0/未超时/有效 final/无 auth_error）+ 切换 + 原配置不变。
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
        and source_binding.get('credentials_ref') is not None
        and env_call.get('auth_error') is True
        and native_call.get('auth_error') is True  # 两 API-key 源 401 = key 对已确认端点无效
    )
    passed = account_ok and env_ok and native_ok and switch and source_files_unchanged
    return {'account_ok': account_ok, 'env_ok': env_ok, 'native_ok': native_ok,
            'account_back_ok': account_back_ok,
            'switch_account_to_apikey': switch, 'diagnosis_ok': diagnosis_ok, 'passed': passed}


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


def _run_account(exe, cli, source_home, test_directory, secret):
    """账号路由实际执行（resolve + copy_credentials + overlay_entries + run_dsh）。

    返回 (route, overlay_evidence, call)，供切换序列的首段与切回段复用。
    """
    bot = {'config_root': str(source_home), 'profile': 'desktop'}
    info = dsh_auth.resolve(bot)
    with tempfile.TemporaryDirectory(prefix='dsh-acc-', dir=test_directory) as tmp:
        private = Path(tmp)
        cred = dsh_auth.copy_credentials(str(source_home), str(private))
        overlay = dsh_auth.overlay_entries(bot, credentials_path=str(cred))
        overlay_path = private / 'overlay.json'
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
        call = run_dsh(str(exe), str(cli), overlay_path, test_directory, test_directory, secret)
    return info['route'], _overlay_evidence(overlay), call


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

    # ① 来源配置匹配：从主 profile 实际配置核对 key 对应 provider/baseURL/model（非硬套官方默认）
    cred_file = args.source_home / '.credentials.yaml'
    env_file = args.source_home / '.env'
    patch_file = args.source_home / 'profiles' / 'desktop' / 'cordis.patch.yml'
    cred = yaml.safe_load(cred_file.read_text(encoding='utf-8-sig')) if cred_file.is_file() else {}
    api_key = (cred.get('refs', {}) or {}).get('DEEPSEEK_API_KEY', '')
    base_url_env = _read_env_key(env_file, 'DEEPSEEK_BASE_URL')
    source_provider, source_model, config_baseurl = _read_profile_config(args.source_home)
    # API-key 路由 provider 须为 agent-default-model.provider == deepseek-official（账号路由 deepseek-account
    # 表示 API-key 未选中）；baseURL：显式 config.baseURL 优先，其次 $DEEPSEEK_BASE_URL，均缺 → 未确认。
    key_ref = 'refs.DEEPSEEK_API_KEY' if api_key else None
    api_provider = _api_key_provider(source_provider)
    api_base_url = config_baseurl or base_url_env  # 均缺 → ''（未从配置确认）
    api_model = source_model or _DEFAULT_API_MODEL  # 读自 config 的 agent-default-model.model
    service_confirmed = api_provider is not None and bool(api_base_url)

    source_files = [cred_file, env_file, patch_file]
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}
    result = {
        'api_key_present': bool(api_key),
        'source_binding': {
            'credentials_ref': key_ref,
            'config_provider': source_provider,   # 读自 agent-default-model.provider
            'config_model': source_model,         # 读自 agent-default-model.model
            'config_base_url': config_baseurl or None,  # 读自插件 config.baseURL（显式）
            'base_url_env': base_url_env or None,       # 读自 .env $DEEPSEEK_BASE_URL
            'apikey_provider': api_provider,      # 仅当 config provider==deepseek-official 时确认
            'apikey_base_url': api_base_url or None,    # config.baseURL 或 $DEEPSEEK_BASE_URL；None=未确认
            'apikey_model': api_model,            # 读自 config 或官方默认
            'service_confirmed': service_confirmed,  # provider+baseURL 均从配置确认才 True
        },
    }

    # 无法从主 profile 确认 API-key 路由的 provider/baseURL → unknown，停止重复真实请求（不发起 DSH 调用）
    if not service_confirmed:
        result['note'] = ('API-key 路由 provider/baseURL 未在主 profile 配置中确认'
                          '（agent-default-model.provider 非 deepseek-official，且 config.baseURL/'
                          '$DEEPSEEK_BASE_URL 均未设置），停止请求，不硬套官方端点')
        result['auth_attribution'] = 'unknown'
        result['passed'] = False
        result['diagnosis_ok'] = False
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    # ② 切换序列：账号路由（首段）→ API-key 路由（env/native）→ 账号路由切回（末段）
    account_route, account_overlay_ev, account_call = _run_account(
        args.exe, args.cli, args.source_home, args.test_directory, api_key)
    result['account_route'] = account_route  # 切换证据：resolve 路由
    result['account_overlay'] = account_overlay_ev  # 切换证据：overlay 构建
    result['account_call'] = account_call

    # ③ .env 来源：隔离 HOME 写前限权，.env 注入 key（refs 值即字面 key，直接注入）
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
        result['env_route'] = info['route']  # 切换证据：resolve 路由
        overlay = dsh_auth.overlay_entries(bot)
        result['env_overlay'] = _overlay_evidence(overlay)  # 切换证据：overlay 构建
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
        args.exe, args.cli, args.source_home, args.test_directory, api_key)
    result['account_back_route'] = account_back_route
    result['account_back_overlay'] = account_back_overlay_ev
    result['account_back_call'] = account_back_call

    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files if p.is_file()}
    # 切换证据：resolve 路由 + overlay 构建 + 切回序列（account→apikey→apikey→account）
    result['switch_sequence'] = [result['account_route'], result['env_route'],
                                 result['native_route'], result['account_back_route']]
    # ⑥ 四态合成判定（assess 单测覆盖成功/全失败/单路失败/路由错漏/漏切回）
    result.update(assess(account_call, result['env_call'], result['native_call'], account_back_call,
                         result['source_binding'], result['source_files_unchanged'],
                         result['switch_sequence']))
    # 401 归因：refs 值即字面 key，被 dsh-llm-deepseek-api-key 当 x-api-key 直发到已确认端点；401 = key 对该端点无效
    native_stderr = result['native_call'].get('stderr_tail', '')
    if native_stderr and result['native_call'].get('auth_error'):
        result['auth_attribution'] = 'key-invalid'  # key 值对已确认端点无效（非解析缺失，非引用）
    else:
        result['auth_attribution'] = 'unknown'
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
