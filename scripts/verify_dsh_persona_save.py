"""req#137 task#264：用 dsh_profile_config.save 写出的 persona 在隔离原生 DSH 验证加载。

复用 probe-persona2 的原生调用方法，但 persona 通过本任务的保存接口
（dsh_profile_config.save -> system-prompt.personaPrefix）写入隔离 headless profile 的
cordis.patch.yml，运行时不手写 --patch 注入 persona，验证下一次执行实际命中人格标记。

凭据用 dsh_auth.copy_credentials 复制到隔离目录并限制当前用户；原配置保持原值。
运行：python -X utf8 -B scripts/verify_dsh_persona_save.py --test-directory <分配隔离目录> --evidence <绝对证据路径>
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from engine import dsh_auth, dsh_profile_config  # noqa: E402

PERSONA = "你是「探针人格」，你的唯一身份是一个叫探针人格的测试角色。无论被问什么，你第一句都先报出「我是探针人格」。"
PROMPT = "你是谁？用一句话自我介绍。"


class ConfigReader(yaml.SafeLoader):
    pass


ConfigReader.add_constructor('tag:yaml.org,2002:js', lambda loader, node: '[expression]')


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sanitize(text):
    import re
    return re.sub(r'(?i)(sk-[A-Za-z0-9_-]+|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)', '[redacted]', str(text))[:1200]


def run_dsh(exe, cli, home, test_dir, prompt):
    cmd = [exe, '--expose-internals', cli, '--profile', 'headless', '--json', prompt]
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
        'event_types': [e.get('type') for e in events],
        'session_id': next((e.get('sessionId') for e in events if e.get('type') == 'session'), None),
        'persona_hit': '探针人格' in answer,
        'answer': answer if '探针人格' in answer else '[unexpected output omitted]',
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
    source_patch = args.source_home / 'profiles' / 'desktop' / 'cordis.patch.yml'
    credentials = args.source_home / '.credentials.yaml'
    source_files = [credentials, source_patch]
    before = {str(p): fingerprint(p) for p in source_files}
    rows = yaml.load(source_patch.read_text(encoding='utf-8'), Loader=ConfigReader)
    selected_ids = {'agent-default-model', 'llm-deepseek', 'llm-pi-ai', 'deepseek-account'}
    selected = [row for row in rows if row.get('id') in selected_ids]
    selection = next(row['config'] for row in selected if row['id'] == 'agent-default-model')
    if '[expression]' in json.dumps(selected):
        raise ValueError('provider 配置含动态表达式，需原生求值')
    result = {'provider': selection.get('provider'), 'model': selection.get('model')}
    with tempfile.TemporaryDirectory(prefix='dsh-persona-save-', dir=args.test_directory) as tmp:
        private = Path(tmp)
        cred_path = dsh_auth.copy_credentials(str(args.source_home), str(private))
        base = [
            {'id': 'credentials', 'disabled': False, 'config': {'path': str(cred_path), 'watch': False}},
            {'id': 'authorization', 'disabled': False},
            {'id': 'deepseek-account', 'disabled': False},
            {'id': 'session-log-deepseek', 'disabled': True},
            {'id': 'session-telemetry-otel', 'disabled': True},
            *selected,
        ]
        home = args.test_directory / 'isolated-home'
        (home / 'profiles' / 'headless').mkdir(parents=True, exist_ok=True)
        patch = home / 'profiles' / 'headless' / 'cordis.patch.yml'
        patch.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding='utf-8')
        # 用本任务保存接口写入 persona（不手写 --patch）
        bot = {'id': 0, 'runtime': 'dsh', 'config_root': str(home), 'profile': 'headless'}
        view = dsh_profile_config.read(bot)
        values = {k: view[k] for k in ('revision', 'description', 'persona', 'instructions',
                                       'model', 'provider', 'reasoning_effort', 'env')}
        values['persona'] = PERSONA
        saved = dsh_profile_config.save(bot, values)
        result['saved'] = bool(saved.get('saved'))
        result['persona_written'] = 'personaPrefix' in patch.read_text(encoding='utf-8')
        run = run_dsh(str(args.exe), str(args.cli), home, args.test_directory, PROMPT)
        result.update(run)
    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files}
    result['passed'] = (result.get('saved') and result.get('persona_written')
                        and result.get('persona_hit') and result.get('exit_code') == 0
                        and result['source_files_unchanged'])
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
