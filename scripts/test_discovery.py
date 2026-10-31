"""req#87 task#150：宿主发现 + Bot 注册/复用接口测试。

覆盖（验收标准三条）：
- 扫描 Hermes profiles/ 与 Codex 配置根产出候选（假目录结构，不依赖真机）
- 同名 profile 双宿主靠 host_type+config_root 区分
- 注册/复用 Bot：已绑定复用（created=False、不覆盖 display_name）、
  未绑定注册（created=True）、非法输入 400、带 project 加入当前项目

运行：python -B -m unittest scripts.test_discovery -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engine import api, db, discovery  # noqa: E402


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 假 Hermes 根：default + profiles/{alice,bob}
        self.hroot = Path(self.tmp.name) / "hermes"
        (self.hroot / "profiles" / "alice").mkdir(parents=True)
        (self.hroot / "profiles" / "alice" / "state.db").write_text("")
        (self.hroot / "profiles" / "bob").mkdir(parents=True)
        (self.hroot / "profiles" / "bob" / "config.yaml").write_text("")
        (self.hroot / "profiles" / "empty-shell").mkdir(parents=True)  # 无工件 → 不发现
        (self.hroot / "config.yaml").write_text("")  # default
        # 假 Codex 根：coco 一个候选 + cache 干扰目录
        self.croot = Path(self.tmp.name) / "codex"
        (self.croot / "coco").mkdir(parents=True)
        (self.croot / "coco" / "config.toml").write_text('model = "test-model"\n')
        (self.croot / "cache").mkdir(parents=True)  # 无工件 → 不发现
        # 假 DSH 根：desktop 一个候选 + empty-shell 干扰目录
        self.droot = Path(self.tmp.name) / "dsh"
        (self.droot / "profiles" / "desktop").mkdir(parents=True)
        (self.droot / "profiles" / "desktop" / "cordis.patch.yml").write_text("plugins: []\n")
        (self.droot / "profiles" / "empty-shell").mkdir(parents=True)  # 无 patch → 不发现
        self._envs = {}
        for k in ("HERMES_HOME", "CODEX_HOME", "DSH_HOME", "GROUP_DB"):
            self._envs[k] = os.environ.get(k)
        os.environ["HERMES_HOME"] = str(self.hroot)
        os.environ["CODEX_HOME"] = str(self.croot)
        os.environ["DSH_HOME"] = str(self.droot)
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        os.environ["GROUP_DB"] = self.dbfile
        conn = db.get_connection()
        db.init_schema(conn)
        conn.close()
        self.client = TestClient(FastAPI(), client=('127.0.0.1', 4321))
        self.client.app.include_router(api.router_api)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for k, v in self._envs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_scan_produces_candidates(self):
        conn = db.get_connection()
        try:
            cands = discovery.scan_candidates(conn)
        finally:
            conn.close()
        got = {(c["host_type"], c["profile"]) for c in cands}
        self.assertIn(("hermes", "default"), got)
        self.assertIn(("hermes", "alice"), got)
        self.assertIn(("hermes", "bob"), got)
        self.assertIn(("codex", "coco"), got)
        self.assertNotIn(("hermes", "empty-shell"), got, "空壳目录不发现")
        self.assertNotIn(("codex", "cache"), got, "无工件目录不发现")
        # 内置 user 不参与发现
        self.assertFalse(any(c["profile"] == "user" for c in cands))

    def test_nested_codex_home_discovery_and_launch(self):
        from engine.bot_identity import codex_settings
        home = self.croot / 'profiles' / 'dev-01'
        home.mkdir(parents=True)
        (home / 'config.toml').write_text('model_reasoning_effort = "medium"')
        (home.parent / 'empty').mkdir()
        (home.parent / 'config.toml').write_text('')
        candidates = self.client.get('/discovery/scan').json()['candidates']
        nested = [c for c in candidates if c['host_type'] == 'codex' and c['profile'] == 'dev-01']
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]['config_root'], str(home))
        self.assertFalse(nested[0]['bound'])
        self.assertEqual(codex_settings(nested[0]), (str(home.resolve()), None))
        self.assertFalse(any(c['profile'] in ('profiles', 'empty') for c in candidates))

    def test_same_profile_dual_host_distinguished(self):
        # alice 同时存在于 hermes 与 codex 根：双宿主候选靠 host_type 区分
        (self.croot / "alice").mkdir()
        (self.croot / "alice" / "config.toml").write_text('model = "test-model"\n')
        conn = db.get_connection()
        try:
            cands = discovery.scan_candidates(conn)
        finally:
            conn.close()
        alices = {(c["host_type"], c["config_root"]) for c in cands if c["profile"] == "alice"}
        self.assertEqual(len(alices), 2, f"双宿主应各自成候选: {alices}")
        keys = {(c["host_type"], c["config_root"], c["profile"]) for c in cands if c["profile"] == "alice"}
        self.assertEqual(len(keys), 2)

    def test_codex_auxiliary_files_are_not_independent_homes(self):
        fixtures = {
            'browser': ('config.toml', 'approval_mode = "on-request"\n'),
            'sessions-only': ('sessions.json', '[]'),
            'empty-config': ('config.toml', ''),
            'invalid-config': ('config.toml', 'model = ['),
            'other-auth': ('auth.json', '{"session":"synthetic"}'),
            'key-home': ('auth.json', '{"OPENAI_API_KEY":"synthetic-key"}'),
            'token-home': ('auth.json', '{"tokens":{"access_token":"synthetic-token"}}'),
        }
        for name, (filename, content) in fixtures.items():
            home = self.croot / name
            home.mkdir()
            (home / filename).write_text(content, encoding='utf-8')
        for endpoint in ('/discovery/scan', '/profiles/candidates'):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200, response.text)
            names = {c['profile'] for c in response.json()['candidates'] if c['host_type'] == 'codex'}
            self.assertEqual(names, {'coco', 'key-home', 'token-home'})

    def test_registered_candidates_require_live_configuration_and_preserve_records(self):
        external = Path(self.tmp.name) / 'external-home'
        external.mkdir()
        config = external / 'config.toml'
        config.write_text('model = "external-model"\n')
        browser = self.croot / 'browser'
        browser.mkdir()
        (browser / 'config.toml').write_text('approval_mode = "on-request"\n')
        records = [
            ('codex', 'profile-13', self.croot / 'profiles' / 'profile-13', 'daxia'),
            ('codex', 'browser', browser, 'Browser'),
            ('codex', 'default', external, 'External Codex'),
            ('hermes', 'deleted', self.hroot, 'Deleted Hermes'),
            ('hermes', 'alice', self.hroot, 'Alice'),
        ]
        conn = db.get_connection()
        try:
            ids = {}
            for host, profile, root, display in records:
                ids[display] = discovery.register_bot(conn, host, profile, str(root), display)['bot_id']
            conn.commit()
            before = [tuple(row) for row in conn.execute('SELECT * FROM bots ORDER BY id')]
            response = self.client.get('/profiles/candidates')
            self.assertEqual(response.status_code, 200, response.text)
            visible = {c['bot_id'] for c in response.json()['candidates']}
            self.assertIn(ids['External Codex'], visible)
            self.assertIn(ids['Alice'], visible)
            for name in ('daxia', 'Browser', 'Deleted Hermes'):
                self.assertNotIn(ids[name], visible)
            config.unlink()
            refreshed = self.client.get('/profiles/candidates').json()['candidates']
            self.assertNotIn(ids['External Codex'], {c['bot_id'] for c in refreshed})
            self.assertEqual(before, [tuple(row) for row in conn.execute('SELECT * FROM bots ORDER BY id')])
        finally:
            conn.close()

    def test_named_codex_profiles_are_checked_at_their_registered_root(self):
        self.croot.joinpath('config.toml').write_text('[profiles.review]\nmodel="test-model"\n')
        self.croot.joinpath('build.config.toml').write_text('model="build-model"\n')
        external = Path(self.tmp.name) / 'external-codex'
        external.mkdir()
        external.joinpath('config.toml').write_text('[profiles.editor]\nmodel="editor-model"\n')
        conn = db.get_connection()
        try:
            ids = [discovery.register_bot(conn, 'codex', name, str(root))['bot_id']
                   for name, root in [('review', self.croot), ('build', self.croot), ('editor', external)]]
            conn.commit()
            visible = {c['bot_id'] for c in self.client.get('/profiles/candidates').json()['candidates']}
            self.assertTrue(set(ids) <= visible)
            self.croot.joinpath('config.toml').write_text('model="default-model"\n')
            self.croot.joinpath('build.config.toml').unlink()
            external.joinpath('config.toml').write_text('model="default-model"\n')
            visible = {c['bot_id'] for c in self.client.get('/profiles/candidates').json()['candidates']}
            self.assertTrue(set(ids).isdisjoint(visible))
        finally:
            conn.close()

    def test_unreadable_codex_config_does_not_abort_other_candidates(self):
        home = self.croot / 'blocked'
        home.mkdir()
        target = home / 'config.toml'
        target.write_text('model="test-model"\n')
        read_text = Path.read_text
        def restricted(path, *args, **kwargs):
            if path == target:
                raise PermissionError('Unreadable fixture')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', restricted):
            response = self.client.get('/profiles/candidates')
        self.assertEqual(response.status_code, 200, response.text)
        names = {c['profile'] for c in response.json()['candidates'] if c['host_type'] == 'codex'}
        self.assertEqual(names, {'coco'})

    def test_register_and_reuse_bot(self):
        # 未绑定 → 注册新 Bot
        r1 = self.client.post("/bots/register", json={
            "host_type": "hermes", "profile": "alice",
            "config_root": str(self.hroot), "display_name": "爱丽丝"})
        self.assertEqual(r1.status_code, 200)
        body1 = r1.json()
        self.assertTrue(body1["created"])
        # 已绑定 → 复用（created=False，不覆盖 display_name）
        r2 = self.client.post("/bots/register", json={
            "host_type": "hermes", "profile": "alice",
            "config_root": str(self.hroot), "display_name": "改名尝试"})
        self.assertEqual(r2.status_code, 200)
        body2 = r2.json()
        self.assertFalse(body2["created"])
        self.assertEqual(body2["bot_id"], body1["bot_id"])
        conn = db.get_connection()
        try:
            row = conn.execute("SELECT display_name FROM bots WHERE id=?", (body1["bot_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["display_name"], "爱丽丝", "复用不覆盖 display_name")

    def test_register_invalid_inputs(self):
        for bad in (
            {"host_type": "web", "profile": "x", "config_root": "/r"},  # 非法宿主
            {"host_type": "hermes", "profile": "Bad Name", "config_root": "/r"},  # 非法名
            {"host_type": "hermes", "profile": "user", "config_root": "/r"},  # 内置 user
            {"host_type": "hermes", "profile": "x"},  # 缺 config_root
        ):
            r = self.client.post("/bots/register", json=bad)
            self.assertEqual(r.status_code, 400, f"{bad} 应 400")

    def test_register_with_project_joins(self):
        conn = db.get_connection()
        try:
            conn.execute("INSERT INTO projects(name, root, created_at) VALUES('p1', ?, ?)",
                         (str(Path(self.tmp.name) / "p1-root"), db.now_iso()))
            conn.commit()
        finally:
            conn.close()
        r = self.client.post("/bots/register", json={
            "host_type": "codex", "profile": "coco", "config_root": str(self.croot),
            "display_name": "扣扣", "project": "p1"})
        self.assertEqual(r.status_code, 200)
        member = r.json()['member_profile']
        conn = db.get_connection()
        try:
            m = conn.execute('SELECT profile,source FROM members WHERE profile=?', (member,)).fetchone()
            pm = conn.execute("SELECT bot_id, active FROM project_members "
                              "WHERE project='p1' AND profile=?", (member,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(m, "花名册应落行")
        self.assertEqual(m["source"], "api")
        self.assertIsNotNone(pm, "应加入项目")
        self.assertEqual(pm["active"], 1)
        # 重复注册（已绑定）+ project → pm 既有行 UPDATE 而非双行
        r2 = self.client.post("/bots/register", json={
            "host_type": "codex", "profile": "coco", "config_root": str(self.croot),
            "project": "p1"})
        self.assertEqual(r2.status_code, 200)
        conn = db.get_connection()
        try:
            rows = conn.execute("SELECT COUNT(*) c FROM project_members "
                                "WHERE project='p1' AND profile=?", (member,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(rows["c"], 1, "重复注册不产生 pm 双行")

    def test_scan_api_bound_flag(self):
        # 注册一个 Bot 后再扫描：候选应标 bound=True + bot_id
        self.client.post("/bots/register", json={
            "host_type": "hermes", "profile": "bob", "config_root": str(self.hroot)})
        r = self.client.get("/discovery/scan")
        self.assertEqual(r.status_code, 200)
        cands = r.json()["candidates"]
        bob = next(c for c in cands if c["profile"] == "bob" and c["host_type"] == "hermes")
        self.assertTrue(bob["bound"])
        self.assertIsNotNone(bob["bot_id"])
        alice = next(c for c in cands if c["profile"] == "alice" and c["host_type"] == "hermes")
        self.assertFalse(alice["bound"])
        self.assertIsNone(alice["bot_id"])

    def test_sandbox_notice_is_advisory_and_enrollment_preserves_policy(self):
        from engine import codex_sandbox_notice
        import tomllib
        path = self.croot / 'coco' / 'config.toml'
        path.write_text('sandbox_mode = "workspace-write"\napproval_policy = "never"\napprovals_reviewer = "user"\n', encoding='utf-8')
        before = path.read_bytes()
        with patch.object(codex_sandbox_notice.sys, 'platform', 'win32'):
            candidates = self.client.get('/discovery/scan').json()['candidates']
        candidate = next(c for c in candidates if c['host_type'] == 'codex' and c['profile'] == 'coco')
        self.assertEqual(candidate['sandbox_notice']['code'], 'unspecified')
        self.assertEqual(candidate['approval_notice']['code'], 'not_enabled')
        self.assertEqual(path.read_bytes(), before)
        conn = db.get_connection()
        try:
            conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)',
                         ('sandbox-advisory', str(Path(self.tmp.name) / 'project'), db.now_iso()))
            conn.commit()
        finally:
            conn.close()
        response = self.client.post('/bots/register', json={
            'host_type': 'codex', 'profile': 'coco', 'config_root': str(self.croot),
            'project': 'sandbox-advisory'})
        self.assertEqual(response.status_code, 200, response.text)
        config = tomllib.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(config['approval_policy'], 'never')
        self.assertEqual(config['approvals_reviewer'], 'user')
        self.assertEqual(config['sandbox_mode'], 'workspace-write')
        self.assertNotIn('windows', config)
        self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [(Path(self.tmp.name) / 'project').as_posix()])

    def test_no_hosts_returns_empty(self):
        # 双根都不可用 → 空列表不报错（冷启动文案由前端渲染）
        os.environ["HERMES_HOME"] = str(Path(self.tmp.name) / "nope")
        os.environ["CODEX_HOME"] = str(Path(self.tmp.name) / "nope2")
        os.environ["DSH_HOME"] = str(Path(self.tmp.name) / "nope3")
        r = self.client.get("/discovery/scan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["candidates"], [])

    def test_scan_dsh_produces_candidates(self):
        conn = db.get_connection()
        try:
            cands = discovery.scan_candidates(conn)
        finally:
            conn.close()
        dsh = {(c["host_type"], c["profile"], c["config_root"]) for c in cands if c["host_type"] == "dsh"}
        self.assertEqual(dsh, {("dsh", "desktop", str(self.droot))})
        # empty-shell 无 patch 不发现
        self.assertFalse(any(c["profile"] == "empty-shell" and c["host_type"] == "dsh" for c in cands))

    def test_register_dsh_bot(self):
        r = self.client.post("/bots/register", json={
            "host_type": "dsh", "profile": "desktop",
            "config_root": str(self.droot), "display_name": "DSH 桌机"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["created"])

    def test_validate_dsh_home(self):
        # 合法目录 + profile → ok
        r = self.client.post("/bots/validate-dsh", json={
            "config_root": str(self.droot), "profile": "desktop"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        # 目录不存在 → 指明失败项并保留路径
        missing = str(Path(self.tmp.name) / "nope")
        r = self.client.post("/bots/validate-dsh", json={"config_root": missing, "profile": "desktop"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertTrue(any("目录不存在" in e for e in body["errors"]))
        self.assertEqual(body["config_root"], missing)
        # profile 目录不存在 → 指明失败项
        r = self.client.post("/bots/validate-dsh", json={"config_root": str(self.droot), "profile": "ghost"})
        self.assertFalse(r.json()["ok"])
        self.assertTrue(any("profile 目录不存在" in e for e in r.json()["errors"]))

    def test_dsh_candidate_available_after_delete(self):
        # 注册后删除 profile patch → candidate_available 判失效（保留成员和历史）
        reg = self.client.post("/bots/register", json={
            "host_type": "dsh", "profile": "desktop", "config_root": str(self.droot)})
        bot_id = reg.json()["bot_id"]
        conn = db.get_connection()
        try:
            row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            self.assertTrue(discovery.candidate_available(row))
        finally:
            conn.close()
        (self.droot / "profiles" / "desktop" / "cordis.patch.yml").unlink()
        conn = db.get_connection()
        try:
            row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            self.assertFalse(discovery.candidate_available(row))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
