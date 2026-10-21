"""req#122 task#212 三次关单核验退修回归（node 1028，Hermes 过滤层截断）。

根因：Hermes gateway _build_safe_env 只放行 _SAFE_ENV_KEYS/XDG_*/外部 secret/显式 server.env，
GROUP_ENGINE_URL 不在白名单被过滤，9fe59dc 的注入到不了 MCP server。

修复：engine_url 改走 XDG_ 前缀注入 env[XDG_GROUP_ENGINE_URL]（XDG_* 是放行通道，
CONTEXT_ENV=XDG_GROUP_ENGINE_EXECUTION 已验证可穿过）；config.engine_url() 增加读
XDG_GROUP_ENGINE_URL（GROUP_ENGINE_URL 存量覆盖 > XDG_GROUP_ENGINE_URL 跨进程 > host/port 派生）。

回归覆盖实际过滤层：① wake spawn env 含 XDG_GROUP_ENGINE_URL（非 GROUP_ENGINE_URL）；
② 真实调 _build_safe_env 断言 XDG_GROUP_ENGINE_URL 保留、GROUP_ENGINE_URL 被滤；
③ 过滤后环境 config.engine_url()==18042。

隔离 + mock，不改用户全局 Hermes 代码/配置、不启真实服务、不碰生产库。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import config, wake

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Hermes gateway 源码（含 _build_safe_env）位置；HERMES_HOME 可能指向 profile 子目录，
# 需向上回溯到安装根（含 hermes-agent/ 的目录）。
def _find_hermes_agent():
    candidates = []
    home = os.environ.get('HERMES_HOME', '')
    if home:
        p = Path(home)
        candidates += [p / 'hermes-agent', p.parent / 'hermes-agent', p.parent.parent / 'hermes-agent']
    candidates.append(Path('E:/home/admin/hermes') / 'hermes-agent')
    for c in candidates:
        if (c / 'tools' / 'mcp_tool_config.py').exists():
            return c
    return None


def _load_build_safe_env():
    """加载 Hermes gateway 的 _build_safe_env；Hermes 源码或依赖不可访问时返回 None（测试 skip）。

    _build_safe_env 惰性依赖 agent.secret_scope / hermes_cli.env_loader 等（间接依赖
    python-dotenv），bot-group venv 未装时缺依赖应 skip 而非 ERROR。
    """
    agent = _find_hermes_agent()
    if agent is None:
        return None
    if str(agent) not in sys.path:
        sys.path.insert(0, str(agent))
    try:
        from tools.mcp_tool_config import _build_safe_env
        # 触发 _build_safe_env 的惰性依赖，缺 python-dotenv 等在此暴露并降级 skip
        from agent.secret_scope import get_secret  # noqa: F401
        from hermes_cli.env_loader import secret_source_names  # noqa: F401
        from agent.delegation_context import delegated_child_subprocess_env  # noqa: F401
        return _build_safe_env
    except ImportError:
        return None


class CrossProcessEngineUrlTest(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for k in ('GROUP_ENGINE_URL', 'XDG_GROUP_ENGINE_URL', 'GROUP_ENGINE_PORT',
                  'GROUP_ENGINE_HOST', 'PORT', 'HOST'):
            self._saved[k] = os.environ.get(k)
            os.environ.pop(k, None)
        config._dotenv_cache.clear()
        config.apply_cli(host='127.0.0.1', port=18042, data_dir=None)

    def tearDown(self):
        config._dotenv_cache.clear()
        config.apply_cli(host=None, port=None, data_dir=None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_wake_engine_url_reflects_cli(self):
        # apply_cli(port=18042) 后 wake._engine_url() 应指向 CLI 覆盖端口，而非默认 8180
        self.assertEqual(wake._engine_url(), 'http://127.0.0.1:18042')

    def test_child_process_reads_engine_url(self):
        # 跨进程传递链路：spawn 构建 env（继承 + 注入 XDG_GROUP_ENGINE_URL=_engine_url()），
        # 真实子进程读 config.engine_url() 应得到 CLI 覆盖端口。
        child_env = os.environ.copy()
        child_env['XDG_GROUP_ENGINE_URL'] = wake._engine_url()
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from engine import config; print(config.engine_url())"
            % str(_PROJECT_ROOT)
        )
        proc = subprocess.run([sys.executable, '-c', code], env=child_env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), 'http://127.0.0.1:18042')

    def test_build_safe_env_preserves_xdg_filters_group(self):
        # ② 真实调 Hermes gateway _build_safe_env：XDG_* 放行、GROUP_ENGINE_URL 被滤。
        build_safe_env = _load_build_safe_env()
        if build_safe_env is None:
            self.skipTest('Hermes gateway 源码不可访问，跳过过滤层回归')
        test_env = {
            'XDG_GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'PATH': os.environ.get('PATH', ''),
        }
        with patch.dict(os.environ, test_env, clear=True):
            filtered = build_safe_env(None)
        self.assertIn('XDG_GROUP_ENGINE_URL', filtered)
        self.assertEqual(filtered['XDG_GROUP_ENGINE_URL'], 'http://127.0.0.1:18042')
        self.assertNotIn('GROUP_ENGINE_URL', filtered)

    def test_filtered_env_engine_url(self):
        # ③ 过滤后环境（XDG 保留、GROUP 滤掉）经 config.engine_url() 仍得 CLI 覆盖端口。
        build_safe_env = _load_build_safe_env()
        if build_safe_env is None:
            self.skipTest('Hermes gateway 源码不可访问，跳过过滤层回归')
        test_env = {
            'XDG_GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'PATH': os.environ.get('PATH', ''),
        }
        with patch.dict(os.environ, test_env, clear=True):
            filtered = build_safe_env(None)
        # filtered 只含白名单键（XDG_GROUP_ENGINE_URL 保留、GROUP_ENGINE_URL 滤掉），
        # 叠加到完整系统环境（保证子进程 Python 可初始化），再确认 config.engine_url() 读 XDG 键。
        child_env = dict(os.environ)
        child_env.update(filtered)
        child_env.pop('GROUP_ENGINE_URL', None)
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from engine import config; print(config.engine_url())"
            % str(_PROJECT_ROOT)
        )
        proc = subprocess.run([sys.executable, '-c', code], env=child_env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), 'http://127.0.0.1:18042')


def _real_hermes_root(saved_home):
    """从 HERMES_HOME 环境变量推导 Hermes 安装根（含 hermes-agent/ 与 profiles/）。"""
    if not saved_home:
        return Path('E:/home/admin/hermes')
    p = Path(saved_home)
    if (p / 'hermes-agent').exists():
        return p
    # profile 子目录（<root>/profiles/<name>）→ 回溯到 <root>
    return p.parent.parent


def _snapshot_profiles(root):
    profiles = root / 'profiles'
    if not profiles.exists():
        return None
    return sorted(p.name for p in profiles.iterdir())


def _snapshot_state_db(root):
    state_db = root / 'state.db'
    if not state_db.exists():
        return None
    return state_db.read_bytes()


class SpawnEnvInjectionTest(unittest.TestCase):
    """① wake spawn env 含 XDG_GROUP_ENGINE_URL（而非 GROUP_ENGINE_URL）。"""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path('engine/schema.sql').read_text(encoding='utf-8'))
        from engine import db
        db.init_schema(self.conn)
        from scripts.project_test_support import enable_members
        for profile in ('a', 'b', 'user'):
            self.conn.execute('INSERT INTO members(profile,display_name) VALUES(?,?)', (profile, profile))
        self.conn.execute(
            "INSERT INTO threads(id,title,creator,members,created_at,last_active_at,round,turn_profile) "
            "VALUES(1,'test','user',?,'now','now',1,'a')", (json.dumps(['a', 'b', 'user']),))
        enable_members(self.conn, ('xjk',))
        self.conn.execute(
            "INSERT INTO messages(id,ts,sender,sender_type,content,thread_id) "
            "VALUES(1,'now','user','user','opening',1)")
        for profile in ('a', 'b'):
            self.conn.execute('INSERT INTO inbox(message_id,profile) VALUES(1,?)', (profile,))
        self.conn.commit()
        self._saved = {}
        for k in ('GROUP_ENGINE_URL', 'XDG_GROUP_ENGINE_URL', 'GROUP_ENGINE_PORT',
                  'GROUP_ENGINE_HOST', 'PORT', 'HOST'):
            self._saved[k] = os.environ.get(k)
            os.environ.pop(k, None)
        config._dotenv_cache.clear()
        config.apply_cli(host='127.0.0.1', port=18042, data_dir=None)
        # 隔离 session_adapter：spawn 会读 HERMES_HOME 定位 state.db/profiles，把 HERMES_HOME
        # 指临时目录双保险（即便 available 意外放行也不写真实 Hermes），并快照真实安装根状态。
        self._saved_home = os.environ.get('HERMES_HOME')
        self._real_home = _real_hermes_root(self._saved_home)
        self._profiles_before = _snapshot_profiles(self._real_home)
        self._state_db_before = _snapshot_state_db(self._real_home)
        self._tmp_home = tempfile.mkdtemp(prefix='hermes-test-')
        os.environ['HERMES_HOME'] = self._tmp_home

    def tearDown(self):
        config._dotenv_cache.clear()
        config.apply_cli(host=None, port=None, data_dir=None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._saved_home is None:
            os.environ.pop('HERMES_HOME', None)
        else:
            os.environ['HERMES_HOME'] = self._saved_home
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def test_spawn_injects_xdg_engine_url(self):
        from engine import db, events, heartbeat
        conn = MagicMock(wraps=self.conn)
        conn.close = MagicMock()
        proc = MagicMock()
        proc.pid = 123
        captured_env = {}

        def start_child(*args, **kwargs):
            captured_env.update(kwargs['env'])
            return proc
        proc.returncode = 0
        try:
            with patch.object(wake, '_workdir_for', return_value=str(_PROJECT_ROOT)), \
                 patch.object(wake, '_hermes_bin', return_value='hermes'), \
                 patch.object(wake, 'find_bot_processes', return_value=[]), \
                 patch.object(wake, 'wake_system_prompt', return_value='prompt'), \
                 patch('engine.project_rules.sync', return_value=None), \
                 patch('engine.session_adapter.available', return_value=False), \
                 patch.object(wake.subprocess, 'Popen', side_effect=start_child) as popen, \
                 patch.object(wake.threading, 'Thread'), \
                 patch.object(db, 'get_connection', return_value=conn), \
                 patch.object(events.bus, 'publish'), \
                 patch.object(heartbeat, 'on_exit'):
                result = wake._spawn('a', 'chat', 'signal', 1, 'xjk')
                self.assertTrue(result['ok'], result)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(captured_env.get('XDG_GROUP_ENGINE_URL'), 'http://127.0.0.1:18042')
                self.assertNotIn('GROUP_ENGINE_URL', captured_env)
            # ③ 断言隔离：测试后真实 Hermes 根 profiles/state.db 无变化（未污染生产）
            self.assertEqual(_snapshot_profiles(self._real_home), self._profiles_before)
            self.assertEqual(_snapshot_state_db(self._real_home), self._state_db_before)
        finally:
            wake._running_sessions.clear()


if __name__ == '__main__':
    unittest.main()
