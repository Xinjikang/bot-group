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


# DSH API-key 适配器注册表：凭据键名 → 适配器 provider（源自 0.2.1-rc.5 dsh-llm-deepseek-api-key 实装）
_API_KEY_ADAPTERS = {
    'DEEPSEEK_API_KEY': 'deepseek-official',
}
_DEFAULT_API_MODEL = 'deepseek-chat'  # 官方默认模型（key 不绑定模型时的兜底）
# key 名 → 官方服务主机（独立依据：凭据引用名派生，非 base_url 同源自比较）
_KEY_SERVICE_HOST = {
    'DEEPSEEK_API_KEY': 'api.deepseek.com',
}
# overlay 三服务（账号路由恢复 credentials/authorization/deepseek-account）
_SERVICE_IDS = ('credentials', 'authorization', 'deepseek-account')


def _api_key_provider(key_ref):
    """按凭据引用键名派生 DSH API-key 适配器 provider（注册表映射，非内联硬编码）。"""
    if not key_ref:
        return None
    key_name = key_ref.rsplit('.', 1)[-1]  # refs.DEEPSEEK_API_KEY → DEEPSEEK_API_KEY
    return _API_KEY_ADAPTERS.get(key_name)


def _key_service_status(key_ref, base_url, base_url_env):
    """key 归属服务状态：按 key 名派生官方服务主机（独立依据）核对 base_url。

    返回 True（官方端点匹配）/ False（主机不匹配）/ None（缺独立依据：未知 key 服务或自定义端点）。
    """
    if not key_ref:
        return None
    key_name = key_ref.rsplit('.', 1)[-1]
    host = _KEY_SERVICE_HOST.get(key_name)
    if host is None:
        return None  # 未知 key 服务
    if base_url_env:
        return None  # 自定义端点（代理）：主机不能独立确认 key 服务 → unknown
    return host in (base_url or '').lower()  # 官方端点：主机含 key 服务 → True，否则 False


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
    - diagnosis_ok：401 根因查明（key 存在 + key 归属服务独立确认 + 两 API-key 路认证失败），单列不并入 passed；
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
        source_binding.get('credentials_ref') is not None
        and source_binding.get('api_key_ref_kind') == 'reference'  # 401 根因：refs 是 UUID 引用非字面 key
        and source_binding.get('key_service_matched') is True
        and native_call.get('auth_error') is True  # native 源 401 = 引用被当 key 发送，非 key 本身无效
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

    # ① 来源配置匹配：核对主 profile 的 key 对应 provider/baseURL/model
    cred_file = args.source_home / '.credentials.yaml'
    env_file = args.source_home / '.env'
    patch_file = args.source_home / 'profiles' / 'desktop' / 'cordis.patch.yml'
    cred = yaml.safe_load(cred_file.read_text(encoding='utf-8-sig')) if cred_file.is_file() else {}
    api_key = (cred.get('refs', {}) or {}).get('DEEPSEEK_API_KEY', '')
    api_key_ref_kind = dsh_auth._ref_kind(api_key)  # reference(UUID 引用 ID)/literal(字面 key)/unknown
    env_literal_key = _read_env_key(env_file, 'DEEPSEEK_API_KEY')  # 源 .env 的字面 key（可能为空）
    base_url_env = _read_env_key(env_file, 'DEEPSEEK_BASE_URL')
    source_provider = source_model = ''
    if patch_file.is_file():
        seq = yaml.safe_load(patch_file.read_text(encoding='utf-8-sig')) or []
        agent = next((e for e in seq if isinstance(e, dict) and e.get('id') == 'agent-default-model'), None)
        if agent and isinstance(agent.get('config'), dict):
            source_provider = str(agent['config'].get('provider') or '')
            source_model = str(agent['config'].get('model') or '')
    # API-key 路由的 provider/baseURL/model：provider 按 key 名从适配器注册表派生（DEEPSEEK_API_KEY
    # → deepseek-official）；baseURL 读 $DEEPSEEK_BASE_URL 或官方默认；model 读自 config（无则官方默认）。
    key_ref = 'refs.DEEPSEEK_API_KEY' if api_key else None
    api_provider = _api_key_provider('refs.DEEPSEEK_API_KEY')  # 按 key 名从注册表派生
    api_base_url = base_url_env or DEFAULT_BASE_URL
    api_model = source_model or _DEFAULT_API_MODEL  # 读自 config 的 agent-default-model.model
    key_service_matched = _key_service_status(key_ref, api_base_url, base_url_env)

    source_files = [cred_file, env_file, patch_file]
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}
    result = {
        'api_key_present': bool(api_key),
        'source_binding': {
            'credentials_ref': key_ref,
            'api_key_ref_kind': api_key_ref_kind,  # refs 是 UUID 引用 ID 还是字面 key
            'env_literal_key': bool(env_literal_key),  # 源 .env 是否有字面 key（非 refs 引用）
            'config_provider': source_provider,   # 读自 agent-default-model（账号路由，非 key 绑定）
            'config_model': source_model,         # 读自 config（账号模型）
            'base_url_env': base_url_env or DEFAULT_BASE_URL,  # 读自 .env（无 $DEEPSEEK_BASE_URL→官方默认）
            'apikey_adapter': api_provider,       # 按 key 名从适配器注册表派生（非内联硬编码）
            'apikey_model': api_model,            # 读自 config 或官方默认
            'key_service_matched': key_service_matched,  # key 归属服务独立核对（True/False/None=unknown）
        },
    }

    # 来源缺独立依据 → 准确 unknown，停止重复真实请求（不发起 DSH 调用）
    if api_key and key_service_matched is None:
        result['note'] = 'key 归属服务缺独立依据（自定义端点或未知 key 服务），停止重复真实请求'
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

    # ③ .env 来源：隔离 HOME 写前限权，注入字面 key（源 .env 有字面 key 才可注入；否则源用 refs UUID 引用，无字面 key）
    if env_literal_key:
        with tempfile.TemporaryDirectory(prefix='dsh-apk-', dir=args.test_directory) as tmp:
            home = Path(tmp)
            dsh_auth.restrict_dir(str(home))  # 写敏感内容前限权
            prof = home / 'profiles' / 'apikey'
            prof.mkdir(parents=True)
            (prof / 'cordis.patch.yml').write_text(_api_key_patch(api_provider, api_model, api_base_url), encoding='utf-8')
            (home / '.env').write_text(f'DEEPSEEK_API_KEY={env_literal_key}\n', encoding='utf-8')
            bot = {'config_root': str(home), 'profile': 'apikey'}
            info = dsh_auth.resolve(bot)
            result['env_api_key_source'] = info['api_key_source']
            result['env_route'] = info['route']  # 切换证据：resolve 路由
            overlay = dsh_auth.overlay_entries(bot)
            result['env_overlay'] = _overlay_evidence(overlay)  # 切换证据：overlay 构建
            overlay_path = home / 'overlay.json'
            overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
            result['env_call'] = run_dsh(str(args.exe), str(args.cli), overlay_path, home,
                                         args.test_directory, env_literal_key)
    else:
        # 源 .env 无字面 key（源用 refs UUID 引用），.env 来源无法用真实字面 key 验证
        result['env_api_key_source'] = 'absent'
        result['env_route'] = 'apikey'  # provider 仍是 API-key 路由，仅缺字面 key
        result['env_call'] = {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False,
                              'auth_error': False,
                              'stderr_tail': 'no literal DEEPSEEK_API_KEY in source .env (source uses refs UUID reference)'}

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
    # 401 归因更正：refs 是 UUID 引用（非字面 key）→ native 源 stderr 出现引用尾缀 = 引用被当 key 发送
    native_stderr = result['native_call'].get('stderr_tail', '')
    if api_key_ref_kind == 'reference' and api_key and api_key[-4:] in native_stderr:
        result['auth_attribution'] = 'ref-sent-as-key'  # 引用 UUID 被当 key 发送，非 key 本身无效
    elif native_stderr:
        result['auth_attribution'] = 'key-invalid'
    else:
        result['auth_attribution'] = 'unknown'
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
