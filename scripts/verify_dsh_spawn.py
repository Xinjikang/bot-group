"""req#137 task#266：dsh spawn 协议真实验证（headless 首建 + --session-id 续跑 + 事件 + 会话绑定）。

用引擎实现路径（native_runtime.dsh 命令 + dsh_overlay 认证 overlay + dsh_reasoning 事件
解析 + session_adapter.dsh_save_session 会话绑定 + dsh_version 版本读取）对真实 DSH 桌面
安装跑两轮：第一轮记住标记并回复 DSH_NATIVE_OK，第二轮同一 --session-id 续跑召回标记；
退出码 0、final 完整、事件类型含 session/thinking/final，会话绑定落隔离库且可读回。
凭据副本写前限权（copy_credentials），退出清理，原配置哈希保持不变，凭据值不输出。

运行：python -X utf8 -B scripts/verify_dsh_spawn.py --entry <dsh.cmd绝对路径> \
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径>
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, native_runtime, dsh_reasoning, session_adapter  # noqa: E402

PROMPT1 = "Remember this test marker: PROBE_DSH_266. Reply with exactly DSH_NATIVE_OK. Do not use tools."
PROMPT2 = "What test marker did I ask you to remember? Reply with the marker only. Do not use tools."


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sanitize(text, secret=''):
    t = re.sub(r'(?i)(sk-[A-Za-z0-9_-]+|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)', '[redacted]', str(text))
    if secret:
        t = t.replace(secret, '[redacted]')
    return t[:1200]


def run_turn(entry, overlay_path, dsh_home, test_dir, prompt, resume=None, timeout=150):
    bot = {'runtime': 'dsh', 'config_root': str(dsh_home),
           'profile': 'desktop', 'runtime_entry': entry}
    cmd, home = native_runtime.dsh(bot, overlay_path=str(overlay_path), resume=resume)
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP")
           if k in os.environ}
    env['DSH_HOME'] = home
    env['DSH_PERMISSION_MODE'] = 'workspace-write'
    env.pop('DEEPSEEK_API_KEY', None)
    proc = subprocess.Popen(cmd, cwd=str(test_dir), env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding='utf-8', errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out, err = proc.communicate(timeout=10)
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                events.append(ev)
        except ValueError:
            pass
    final = next((e for e in reversed(events) if e.get('type') == 'final'), {})
    return {
        'exit_code': proc.returncode, 'timed_out': timed_out,
        'event_types': [e.get('type') for e in events],
        'session_id': next((e.get('sessionId') for e in events if e.get('type') == 'session'), None),
        'answer': final.get('text', '') if final.get('type') == 'final' else '',
        'had_final': bool(final and final.get('type') == 'final'),
        'stderr_tail': sanitize(err),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--entry', type=Path, required=True, help='dsh.cmd 绝对路径')
    ap.add_argument('--source-home', type=Path, required=True, help='源 DSH_HOME（账号路由凭据）')
    ap.add_argument('--test-directory', type=Path, required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f'--entry 不是文件：{args.entry}')
    if not args.source_home.is_dir():
        raise ValueError(f'--source-home 不是目录：{args.source_home}')
    if not args.test_directory.is_dir():
        raise ValueError('--test-directory 必须是已分配的隔离目录')

    source_files = [args.source_home / '.credentials.yaml',
                    args.source_home / 'profiles' / 'desktop' / 'cordis.patch.yml']
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}

    result = {
        'version': native_runtime.dsh_version(str(args.entry)),
        'verified_version': native_runtime._DSH_VERIFIED_VERSION,
    }

    # 隔离库：dsh_save_session / dsh_session_id 绑定闭环（引擎重启后可从库恢复）
    with tempfile.TemporaryDirectory(prefix='dsh-spawn-db-', dir=args.test_directory) as dbdir:
        conn = sqlite3.connect(Path(dbdir) / 'group.db')
        conn.row_factory = sqlite3.Row
        db.init_schema(conn)
        conn.execute("INSERT INTO projects(name, root, worktree_root, created_at) "
                     "VALUES('bot-group', ?, '', ?)", (str(args.test_directory), db.now_iso()))
        cur = conn.execute("INSERT INTO bots(host_type, profile, config_root, runtime, "
                           "runtime_entry, display_name, role) VALUES('dsh','dsh-bot',?,'dsh',?,?, '')",
                           (str(args.source_home), str(args.entry), 'dsh-bot'))
        bot_id = cur.lastrowid
        conn.execute("INSERT INTO project_members(project, profile, bot_id, active) "
                     "VALUES('bot-group','dsh-bot',?,1)", (bot_id,))
        conn.commit()

        # 隔离凭据副本 + overlay（同 wake.py 的 spawn 构造）
        from engine import dsh_auth
        private = tempfile.mkdtemp(prefix='dsh-spawn-cred-', dir=args.test_directory)
        try:
            cred_path = dsh_auth.copy_credentials(str(args.source_home), private)
            bot = {'runtime': 'dsh', 'config_root': str(args.source_home), 'profile': 'desktop',
                   'runtime_entry': str(args.entry)}
            overlay = native_runtime.dsh_overlay(bot, credentials_path=cred_path)
            overlay_path = Path(private) / 'overlay.json'
            overlay_path.write_text(json.dumps(overlay, ensure_ascii=False), encoding='utf-8')
            result['overlay_ids'] = [e.get('id') for e in overlay if isinstance(e, dict) and e.get('id')]

            turn1 = run_turn(str(args.entry), overlay_path, args.source_home,
                             args.test_directory, PROMPT1)
            result['turn1'] = {k: v for k, v in turn1.items() if k != 'stderr_tail'}
            if turn1['session_id']:
                saved = session_adapter.dsh_save_session(
                    conn, 'bot-group', 'dsh-bot', 'work', None, turn1['session_id'], bot_id=bot_id)
                result['binding_saved'] = saved
            turn2 = run_turn(str(args.entry), overlay_path, args.source_home,
                             args.test_directory, PROMPT2,
                             resume=turn1['session_id'])
            result['turn2'] = {k: v for k, v in turn2.items() if k != 'stderr_tail'}
            result['binding_read_back'] = session_adapter.dsh_session_id(
                conn, 'bot-group', 'dsh-bot', 'work', None, bot_id=bot_id)
        finally:
            conn.close()
            import shutil
            shutil.rmtree(private, ignore_errors=True)

    result['source_files_unchanged'] = before == {str(p): fingerprint(p) for p in source_files if p.is_file()}
    result['passed'] = (
        result.get('version') == result['verified_version']
        and turn1['exit_code'] == 0 and turn1['answer'] == 'DSH_NATIVE_OK' and turn1['had_final']
        and bool(turn1['session_id']) and result.get('binding_saved')
        and turn2['exit_code'] == 0 and 'PROBE_DSH_266' in turn2['answer']
        and turn2['session_id'] == turn1['session_id']
        and result['binding_read_back'] == turn1['session_id']
        and result['source_files_unchanged']
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
