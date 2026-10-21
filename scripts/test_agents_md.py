"""项目 AGENTS.md 读写接口契约（req bot-group#23 task#52，规格 ENGINEERING.md 27.2）。

覆盖：正常读/写、revision 冲突 409、bot 403、超限 400、CRLF+BOM 保留、
原子替换无残留临时文件、只碰主目录那一份（worktree 字节不变）。

跑法：python -B -m unittest scripts.test_agents_md -v
"""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import agents_md, api  # noqa: E402


class _KeepOpen(sqlite3.Connection):
    """`agents_md` 每查一次都自己 close 连接（真实行为），测试里共用内存库时不能真关。"""

    def close(self):
        pass


class AgentsMdTests(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp(prefix="agents-md-test-"))
        self.conn = sqlite3.connect(":memory:", factory=_KeepOpen)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "engine" / "schema.sql").read_text(encoding="utf-8"))
        self._project("plain", self.tree / "plain", worktree_root="")
        self._project("wt", self.tree / "wt", worktree_root=str(self.tree / "wt-roots"))
        (self.tree / "plain").mkdir(parents=True, exist_ok=True)
        (self.tree / "wt").mkdir(parents=True, exist_ok=True)
        self._patch_conn()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        shutil.rmtree(self.tree, ignore_errors=True)

    def _patch_conn(self):
        patcher = patch.object(agents_md, "_conn", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _project(self, name, root, worktree_root=""):
        self.conn.execute(
            "INSERT INTO projects (name, root, description, archived, created_at, worktree_root) "
            "VALUES (?, ?, '', 0, 'now', ?)",
            (name, str(root), worktree_root),
        )

    # ---------- 读 ----------

    def test_read_missing_file_is_an_empty_valid_editing_start(self):
        view = agents_md.read("plain")
        self.assertEqual(view["project"], "plain")
        self.assertFalse(view["exists"])
        self.assertEqual(view["content"], "")
        self.assertEqual(view["revision"], "")
        self.assertEqual(view["path"], str(self.tree / "plain" / "AGENTS.md"))
        self.assertFalse(view["worktree"])
        self.assertIn("重建系统提示词", view["worktree_note"])
        self.assertEqual(view["content_error"], "")
        self.assertEqual(view["size"], 0)
        self.assertFalse(view["oversize"])

    def test_read_returns_utf8_content_and_byte_sha256(self):
        payload = "# 约定\n\n所有人读这一份。\n".encode("utf-8")
        (self.tree / "plain" / "AGENTS.md").write_bytes(payload)
        view = agents_md.read("plain")
        self.assertTrue(view["exists"])
        self.assertEqual(view["content"], payload.decode("utf-8"))
        self.assertEqual(view["revision"], hashlib.sha256(payload).hexdigest())

    def test_read_flags_worktree_projects(self):
        view = agents_md.read("wt")
        self.assertTrue(view["worktree"])
        self.assertIn("worktree", view["worktree_note"])
        self.assertIn("不即时生效", view["worktree_note"])

    def test_read_unknown_project_is_404(self):
        with self.assertRaises(api.HTTPException) as error:
            agents_md.read("nope")
        self.assertEqual(error.exception.status_code, 404)

    def test_read_rejects_missing_root_directory(self):
        (self.tree / "plain").rename(self.tree / "moved")
        with self.assertRaises(api.HTTPException) as error:
            agents_md.read("plain")
        self.assertEqual(error.exception.status_code, 400)

    # ---------- 写 ----------

    def test_save_writes_file_and_returns_new_revision(self):
        view = agents_md.read("plain")
        result = agents_md.save("plain", "# 新\n", view["revision"])
        self.assertTrue(result["saved"])
        written = (self.tree / "plain" / "AGENTS.md").read_bytes()
        self.assertEqual(written, b"# \xe6\x96\xb0\n")
        self.assertEqual(result["revision"], hashlib.sha256(written).hexdigest())
        self.assertEqual(agents_md.read("plain")["content"], "# 新\n")

    def test_save_backs_up_original_before_replace(self):
        (self.tree / "plain" / "AGENTS.md").write_bytes(b"original\n")
        view = agents_md.read("plain")
        result = agents_md.save("plain", "changed\n", view["revision"])
        self.assertIn("backup", result)
        backup = self.tree / "plain" / result["backup"]
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "AGENTS.md").read_bytes(), b"original\n")
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sha256"], hashlib.sha256(b"original\n").hexdigest())
        self.assertEqual((self.tree / "plain" / "AGENTS.md").read_bytes(), b"changed\n")

    def test_save_new_file_has_no_backup(self):
        view = agents_md.read("plain")  # 文件不存在
        result = agents_md.save("plain", "brand new\n", view["revision"])
        self.assertTrue(result["saved"])
        self.assertIsNone(result["backup"])
        self.assertFalse((self.tree / "plain" / "backups").exists())

    def test_stale_revision_is_409_and_file_untouched(self):
        (self.tree / "plain" / "AGENTS.md").write_bytes(b"original\n")
        with self.assertRaises(api.HTTPException) as error:
            agents_md.save("plain", "hijack\n", "0" * 64)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual((self.tree / "plain" / "AGENTS.md").read_bytes(), b"original\n")

    def test_save_keeps_crlf_and_bom(self):
        original = b"\xef\xbb\xbf" + "a\r\nb\r\n".encode("utf-8")
        (self.tree / "plain" / "AGENTS.md").write_bytes(original)
        revision = agents_md.read("plain")["revision"]
        agents_md.save("plain", "a\nb\nc\n", revision)
        written = (self.tree / "plain" / "AGENTS.md").read_bytes()
        self.assertTrue(written.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"a\r\nb\r\nc\r\n", written)
        # 没有裸 LF：所有 \n 都属于 \r\n
        self.assertEqual(written.count(b"\n"), written.count(b"\r\n"))

    def test_save_leaves_no_temp_file_behind(self):
        agents_md.save("plain", "hello\n", "")
        leftovers = [p.name for p in (self.tree / "plain").iterdir() if p.name != "AGENTS.md"]
        self.assertEqual(leftovers, [])

    def test_save_is_atomic_on_replace_failure(self):
        (self.tree / "plain" / "AGENTS.md").write_bytes(b"keep me\n")
        revision = agents_md.read("plain")["revision"]
        with patch.object(agents_md.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(api.HTTPException) as error:
                agents_md.save("plain", "boom\n", revision)
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual((self.tree / "plain" / "AGENTS.md").read_bytes(), b"keep me\n")
        leftovers = [p.name for p in (self.tree / "plain").iterdir() if p.name != "AGENTS.md"]
        self.assertEqual(leftovers, [])

    def test_save_rejects_oversize_and_non_string(self):
        with self.assertRaises(api.HTTPException) as over:
            agents_md.save("plain", "x" * (2 * 1024 * 1024 + 1), "")
        self.assertEqual(over.exception.status_code, 400)
        with self.assertRaises(api.HTTPException) as bad:
            agents_md.save("plain", 123, "")
        self.assertEqual(bad.exception.status_code, 400)
        self.assertFalse((self.tree / "plain" / "AGENTS.md").exists())

    def test_oversize_limit_measures_post_conversion_bytes(self):
        """CRLF 文件中 LF 会展开为 2 字节，大小限制按落盘字节判定。

        1.5MB 纯换行内容自身合规，但原文件是 CRLF → 落盘 3MB。旧实现先判 content
        再转换，会 200 通过并把 3MB 写进磁盘（随后 GET 再也读不出，文件锁死）。
        """
        original = b"# x" + bytes([13, 10, 13, 10])
        (self.tree / "plain" / "AGENTS.md").write_bytes(original)
        revision = agents_md.read("plain")["revision"]
        content = "\n" * 1_500_000           # 自身 1.5MB ≤ 2MB：过廉价早退
        self.assertLessEqual(len(content.encode("utf-8")), 2 * 1024 * 1024)
        with self.assertRaises(api.HTTPException) as error:
            agents_md.save("plain", content, revision)
        self.assertEqual(error.exception.status_code, 400)
        # 文件必须原样不动（不能写出超限文件）
        self.assertEqual((self.tree / "plain" / "AGENTS.md").read_bytes(), original)
        self.assertEqual(agents_md.read("plain")["revision"], revision)

    def test_oversize_on_disk_file_still_yields_revision_for_rescue(self):
        """磁盘文件超限时 GET 仍返回 revision，供修复后保存。"""
        (self.tree / "plain" / "AGENTS.md").write_bytes(b"y" * (2 * 1024 * 1024 + 10))
        view = agents_md.read("plain")
        self.assertTrue(view["exists"])
        self.assertTrue(view["oversize"])
        self.assertNotEqual(view["revision"], "")
        self.assertEqual(view["content"], "")
        self.assertIn("2 MB", view["content_error"])
        # 用这个 revision 就能整份覆盖救回来
        result = agents_md.save("plain", "# 修好了\n", view["revision"])
        self.assertTrue(result["saved"])
        self.assertEqual((self.tree / "plain" / "AGENTS.md").read_bytes(), "# 修好了\n".encode("utf-8"))

    def test_non_utf8_file_still_yields_revision_for_rescue(self):
        """GBK 等非 UTF-8 文件读取时，GET 仍返回 revision。"""
        (self.tree / "plain" / "AGENTS.md").write_bytes("# 约定\n".encode("gbk"))
        view = agents_md.read("plain")
        self.assertTrue(view["exists"])
        self.assertEqual(view["content"], "")
        self.assertIn("UTF-8", view["content_error"])
        self.assertNotEqual(view["revision"], "")
        result = agents_md.save("plain", "# 重写\n", view["revision"])
        self.assertTrue(result["saved"])
        self.assertEqual((self.tree / "plain" / "AGENTS.md").read_bytes(), "# 重写\n".encode("utf-8"))

    def test_save_rejects_symlink_target(self):
        target = self.tree / "outside.md"
        target.write_bytes(b"outside\n")
        link = self.tree / "plain" / "AGENTS.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted on this host")
        with self.assertRaises(api.HTTPException) as error:
            agents_md.save("plain", "overwrite\n", "")
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(target.read_bytes(), b"outside\n")

    def test_directory_named_agents_md_is_rejected(self):
        (self.tree / "plain" / "AGENTS.md").mkdir()
        for call in (lambda: agents_md.read("plain"), lambda: agents_md.save("plain", "x\n", "")):
            with self.assertRaises(api.HTTPException) as error:
                call()
            self.assertEqual(error.exception.status_code, 400)

    def test_only_touches_project_root_not_worktree(self):
        worktree_file = self.tree / "wt-roots" / "default" / "AGENTS.md"
        worktree_file.parent.mkdir(parents=True, exist_ok=True)
        worktree_file.write_bytes(b"worktree copy\n")
        before = hashlib.sha256(worktree_file.read_bytes()).hexdigest()
        agents_md.save("wt", "# 主目录版\n", "")
        after = hashlib.sha256(worktree_file.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual((self.tree / "wt" / "AGENTS.md").read_text(encoding="utf-8"), "# 主目录版\n")
        # 也不留任何 git 痕迹：模块只做文件读写，绝不 add/commit
        self.assertFalse((self.tree / "wt" / ".git").exists())

    def test_save_noop_when_bytes_unchanged(self):
        (self.tree / "plain" / "AGENTS.md").write_bytes(b"same\n")
        revision = agents_md.read("plain")["revision"]
        result = agents_md.save("plain", "same\n", revision)
        self.assertTrue(result["saved"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["revision"], revision)


class AgentsMdApiTests(unittest.TestCase):
    """接口层：鉴权口径与 /members/{profile}/config 一致（bot 403，管理员放行）。"""

    def test_bot_identity_is_forbidden(self):
        for handler in (api.get_project_agents_md, api.put_project_agents_md):
            args = ("plain",) if handler is api.get_project_agents_md else ("plain", None)
            with self.assertRaises(api.HTTPException) as error:
                handler(*args, identity=("architect", "bot"))
            self.assertEqual(error.exception.status_code, 403)

    def test_dev_and_user_identities_pass_through(self):
        captured = {}

        def fake_read(name):
            captured["name"] = name
            return {"project": name}

        with patch.object(agents_md, "read", side_effect=fake_read):
            for identity in (("", "dev"), ("user", "user")):
                response = api.get_project_agents_md("plain", identity=identity)
                self.assertEqual(json.loads(response.body)["project"], "plain")
        self.assertEqual(captured["name"], "plain")

    def test_routes_are_registered(self):
        paths = {r.path for r in api.router_api.routes}
        self.assertIn("/projects/{name}/agents-md", paths)
        methods = {
            m
            for r in api.router_api.routes
            if r.path == "/projects/{name}/agents-md"
            for m in r.methods
        }
        self.assertEqual(methods, {"GET", "PUT"})

    def test_body_model_defers_type_and_length_checks_to_save(self):
        """400（不是 422）：类型/长度校验必须在 save 里判，Pydantic 层不许拦。"""
        model = api.AgentsMdEdit(content="x" * (2 * 1024 * 1024 + 1), revision="")
        with self.assertRaises(api.HTTPException) as error:
            api.put_project_agents_md("plain", model, identity=("", "dev"))
        self.assertEqual(error.exception.status_code, 400)
        # 非字符串同理：模型收下，save 判 400
        with self.assertRaises(api.HTTPException) as error:
            api.put_project_agents_md("plain", api.AgentsMdEdit(content=123), identity=("", "dev"))
        self.assertEqual(error.exception.status_code, 400)


class AgentsMdSourceTests(unittest.TestCase):
    """静态断言：不自动 commit、不自动同步 worktree。"""

    def test_module_never_calls_git(self):
        tree = ast.parse((ROOT / "engine" / "agents_md.py").read_text(encoding="utf-8"))
        names = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        for banned in ("run", "Popen", "check_call", "check_output", "system"):
            self.assertNotIn(banned, names, f"agents_md.py 不应调用 {banned}（不许自动 commit）")

    def test_only_agents_md_path_is_built(self):
        source = (ROOT / "engine" / "agents_md.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"AGENTS.md"'), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
