"""req#122 task#214 升级回归：安全停机/备份事务化/获取/落地/依赖同步/迁移验证/回滚。

隔离 + mock，不碰真实程序根/数据目录/生产库。覆盖验收 4 条 + node 1061 关单核验七缺陷：
①备份事务化 ②迁移 row_factory ③迁移独立副本 ④回滚停机闸门 ⑤依赖恢复失败传播
⑥程序恢复发布清单 ⑦config 统一路径。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import upgrade  # noqa: E402
from scripts import make_release  # noqa: E402


def _make_real_sqlite(path) -> None:
    """测试夹具：建真实 SQLite 库（upgrade._backup 现走 sqlite3 backup API，非字节流复制）。"""
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('dbdata')")
    conn.commit()
    conn.close()


class UpgradeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.prog = self.root / "program"
        self.data = self.root / "data"
        self.prog.mkdir()
        self.data.mkdir()
        (self.prog / "engine").mkdir()
        (self.prog / "engine" / "main.py").write_text("old", encoding="utf-8")
        (self.prog / "scripts").mkdir()
        (self.prog / "scripts" / "install.py").write_text("old", encoding="utf-8")
        (self.prog / "requirements.lock").write_text("fastapi==1\n", encoding="utf-8")
        (self.prog / ".venv").mkdir()
        _make_real_sqlite(self.data / "group.db")
        (self.data / ".env").write_text("PORT=8180\n", encoding="utf-8")
        (self.data / "attachments").mkdir()
        (self.data / "attachments" / "a.txt").write_text("att", encoding="utf-8")
        self._pp = mock.patch.object(upgrade.config, "program_root", return_value=self.prog)
        self._dp = mock.patch.object(upgrade.config, "data_dir", return_value=self.data)
        self._bp = mock.patch.object(upgrade.config, "backups_dir", return_value=self.data / "backups")
        self._env = mock.patch.dict(os.environ, {"GROUP_DB": "", "GROUP_ATTACHMENTS_DIR": ""})
        self._network = mock.patch.object(socket.socket, "connect",
            side_effect=AssertionError("升级单元测试禁止真实网络连接；请显式模拟停机和下载"))
        self._network_ex = mock.patch.object(socket.socket, "connect_ex",
            side_effect=AssertionError("升级单元测试禁止真实网络连接"))
        # 隔离：停机闸门用标准库直查 /health，必须 mock 掉 host/port 避免探测真实 8180 引擎
        self._hp = mock.patch.object(upgrade.config, "host", return_value="127.0.0.1")
        self._pt = mock.patch.object(upgrade.config, "port", return_value=1)
        self._pp.start()
        self._dp.start()
        self._bp.start()
        self._hp.start()
        self._pt.start()
        self._env.start()
        self._network.start()
        self._network_ex.start()
        # node 1089 缺陷1：缺旧清单 do_upgrade fail-closed 中止。基座默认建立持久化
        # 旧清单（程序根现有程序文件 = 受管），模拟「上次升级落地过清单」的正常环境；
        # 缺清单场景由各测试显式 unlink 后断言 fail-closed。
        self._seed_manifest()

    def _seed_manifest(self, extra=()):
        """程序根写入持久化 release_manifest（默认受管 = 基座程序文件集 + extra）。"""
        files = {"engine/main.py", "scripts/install.py", "requirements.lock"} | set(extra)
        (self.prog / "release_manifest.json").write_text(
            json.dumps({"program_files": sorted(files)}), encoding="utf-8")

    def tearDown(self):
        self._network_ex.stop()
        self._network.stop()
        self._env.stop()
        self._pt.stop()
        self._hp.stop()
        self._bp.stop()
        self._dp.stop()
        self._pp.stop()
        self._tmp.cleanup()


class BackupTest(UpgradeTestBase):
    def test_backup_atomic_publish_with_manifest(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        self.assertTrue((backup / upgrade.MANIFEST).exists())
        self.assertTrue((backup / "data" / "group.db").exists())
        self.assertTrue((backup / "data" / ".env").exists())
        self.assertTrue((backup / "data" / "attachments" / "a.txt").exists())
        self.assertTrue((backup / "program" / "engine" / "main.py").exists())
        self.assertTrue((backup / "program" / "requirements.lock").exists())
        self.assertFalse((backup / "program" / ".venv").exists())
        self.assertFalse((backup / "program" / "group.db").exists())

    def test_backup_without_manifest_not_selected(self):
        # 缺陷①：latest_backup 只选带 manifest 的完整快照，不完整备份不触发回滚删原数据
        backups = self.data / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        (backups / "upgrade-broken").mkdir()
        (backups / "upgrade-broken" / "data").mkdir()
        self.assertIsNone(upgrade.latest_backup())
        self.assertEqual(upgrade.do_rollback(), 1)

    def test_backup_uses_config_paths(self):
        # 缺陷⑦：备份走 config.db_path()/attachments_dir() 统一路径（尊重 GROUP_DB/GROUP_ATTACHMENTS_DIR）
        custom_db = self.root / "custom" / "custom.db"
        custom_db.parent.mkdir(parents=True)
        _make_real_sqlite(custom_db)
        custom_att = self.root / "custom" / "atts"
        custom_att.mkdir(parents=True)
        (custom_att / "f.txt").write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GROUP_DB": str(custom_db),
                                          "GROUP_ATTACHMENTS_DIR": str(custom_att)}):
            backup = self.root / "b"
            upgrade._backup(backup)
        restored = backup / "data" / "group.db"
        self.assertTrue(restored.exists())
        import sqlite3
        conn = sqlite3.connect(str(restored))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)
        self.assertTrue((backup / "data" / "attachments" / "f.txt").exists())


class LandProgramTest(UpgradeTestBase):
    def test_land_program_skips_data_items(self):
        src = self.root / "src"
        (src / "engine").mkdir(parents=True)
        (src / "engine" / "main.py").write_text("new", encoding="utf-8")
        (src / "group.db").write_text("new-db", encoding="utf-8")
        (src / "requirements.lock").write_text("fastapi==2\n", encoding="utf-8")
        upgrade._land_program(src)
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.prog / "requirements.lock").read_text(encoding="utf-8"), "fastapi==2\n")
        import sqlite3
        conn = sqlite3.connect(str(self.data / "group.db"))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)

    def test_land_program_preserves_user_file_not_in_manifest(self):
        # node 1074 缺陷1：覆盖删除只认受管理发布清单，非清单用户文件不删不覆盖
        backup = self.root / "b"
        upgrade._backup(backup)
        manifest = upgrade._read_manifest(backup)
        old_managed = upgrade._top_level(manifest.get("program_files", {}))
        (self.prog / "user_notes.txt").write_text("user", encoding="utf-8")
        src = self.root / "src"
        src.mkdir()
        (src / "user_notes.txt").write_text("hijack", encoding="utf-8")  # 新包同名文件试图覆盖
        upgrade._land_program(src, old_managed)
        self.assertEqual((self.prog / "user_notes.txt").read_text(encoding="utf-8"), "user")

    def test_land_program_overwrites_managed_file(self):
        # 清单内文件（engine/main.py）仍正常覆盖（文件级替换）
        backup = self.root / "b"
        upgrade._backup(backup)
        src = self.root / "src"
        (src / "engine").mkdir(parents=True)
        (src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        old_managed = {"engine/main.py"}
        upgrade._land_program(src, old_managed)
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "v2")

    def test_land_program_directory_replacement_preserves_unmanaged_files(self):
        # node 1084 缺陷2：目录级替换删未受管文件——改文件级替换后，
        # 受管目录内清单外文件（engine/user_notes.txt）落地后保留
        (self.prog / "engine" / "user_notes.txt").write_text("USER-DATA", encoding="utf-8")
        src = self.root / "src"
        (src / "engine").mkdir(parents=True)
        (src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        old_managed = {"engine/main.py"}
        upgrade._land_program(src, old_managed)
        self.assertEqual((self.prog / "engine" / "user_notes.txt").read_text(encoding="utf-8"), "USER-DATA")
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "v2")

    def test_land_program_without_managed_set_overwrites_by_file(self):
        # old_managed=None 旧语义（直接覆盖）现为文件级：不删目录内清单外文件
        (self.prog / "engine" / "user_notes.txt").write_text("USER-DATA", encoding="utf-8")
        src = self.root / "src"
        (src / "engine").mkdir(parents=True)
        (src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        upgrade._land_program(src)
        self.assertEqual((self.prog / "engine" / "user_notes.txt").read_text(encoding="utf-8"), "USER-DATA")
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "v2")


class SafeStopTest(UpgradeTestBase):
    def test_password_protected_health_and_shutdown_use_installation_credentials(self):
        from types import SimpleNamespace
        from starlette.requests import Request
        from engine import main as engine_main
        (self.data / ".env").write_text(
            "GROUP_ADMIN_PASSWORD=isolated-password\nGROUP_SECRET=isolated-secret\n", encoding="utf-8")
        app = SimpleNamespace(state=SimpleNamespace(
            instance_id="isolated-instance", server=object(), shutdown=mock.Mock()))
        app.state.shutdown.begin.return_value = {"status": "stopping"}
        calls = []
        with mock.patch.dict(os.environ), mock.patch.object(engine_main, "app", app):
            os.environ.pop("GROUP_ADMIN_PASSWORD", None)
            os.environ.pop("GROUP_SECRET", None)

            class Opener:
                def open(inner, request, timeout):
                    path = upgrade.urllib.parse.urlparse(request.full_url).path
                    scope = {"type": "http", "path": path, "method": request.get_method(),
                             "route": SimpleNamespace(path=path),
                             "headers": [(key.lower().encode(), value.encode())
                                         for key, value in request.header_items()]}
                    incoming = Request(scope)
                    if path == "/health":
                        result = engine_main.health(incoming)
                    else:
                        identity = engine_main._resolve_identity(
                            authorization=incoming.headers.get("authorization"), request=incoming)
                        result = engine_main.request_shutdown(identity=identity)
                    calls.append((request.get_method(), path))
                    return io.BytesIO(json.dumps(result).encode())

            with mock.patch.object(upgrade.urllib.request, "build_opener", return_value=Opener()), \
                    mock.patch.object(upgrade, "_pid_alive", return_value=False), \
                    mock.patch.object(upgrade, "_port_closed", return_value=True):
                self.assertEqual(upgrade._stop_independent(), 0)
            self.assertEqual(calls, [("GET", "/health"), ("POST", "/admin/shutdown")])
            app.state.shutdown.begin.assert_called_once()

    def test_maintenance_credential_preserves_process_environment_precedence(self):
        from engine import auth
        (self.data / ".env").write_text("GROUP_ADMIN_PASSWORD=file-password\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GROUP_ADMIN_PASSWORD": "process-password"}):
            header = upgrade._admin_headers()["Authorization"]
            self.assertTrue(auth.verify_token(header.removeprefix("Bearer ")))
            self.assertEqual(auth._password(), "process-password")

    def _health(self, **kw):
        base = {"status": "ok", "pid": 12345, "instance_id": "abc", "graceful_shutdown": 1,
                "program_root": str(self.prog), "db_path": str(self.data / "group.db")}
        base.update(kw)
        return base

    def _fake_shutdown(self, payload):
        opener = mock.Mock()
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.StringIO(json.dumps(payload))
        opener.open.return_value = resp
        return mock.patch.object(upgrade.urllib.request, "build_opener", return_value=opener)

    def test_stop_no_engine_returns_0(self):
        # 端口拒绝连接（无引擎监听）→ 0（已停止，无写入方）
        with mock.patch.object(upgrade, "_engine_health", return_value=None):
            self.assertEqual(upgrade._stop_independent(), 0)

    def test_stop_foreign_service_returns_1(self):
        # 端口上有非引擎服务 → 1（无法确认，不误杀）
        with mock.patch.object(upgrade, "_engine_health", side_effect=RuntimeError("非引擎服务")):
            self.assertEqual(upgrade._stop_independent(), 1)

    def test_stop_old_engine_no_identity_returns_1(self):
        # 缺凭据（无 graceful_shutdown/instance_id）→ 1，不误杀（node 1071 P1）
        with mock.patch.object(upgrade, "_engine_health", return_value={"status": "ok"}):
            self.assertEqual(upgrade._stop_independent(), 1)

    def test_stop_invalid_pid_returns_1(self):
        with mock.patch.object(upgrade, "_engine_health", return_value=self._health(pid="bad")):
            self.assertEqual(upgrade._stop_independent(), 1)

    def test_stop_graceful_shutdown_success_returns_0(self):
        with mock.patch.object(upgrade, "_engine_health", return_value=self._health()), \
                self._fake_shutdown({"instance_id": "abc"}), \
                mock.patch.object(upgrade, "_pid_alive", return_value=False), \
                mock.patch.object(upgrade, "_port_closed", return_value=True):
            self.assertEqual(upgrade._stop_independent(), 0)

    def test_stop_foreign_or_unbound_engine_never_posts(self):
        for fields in ({"program_root": str(self.root / "other")},
                       {"db_path": str(self.root / "other.db")},
                       {"program_root": None}, {"db_path": ""}):
            with self.subTest(fields=fields), \
                    mock.patch.object(upgrade, "_engine_health", return_value=self._health(**fields)), \
                    mock.patch.object(upgrade.urllib.request, "build_opener") as opener:
                self.assertEqual(upgrade._stop_independent(), 1)
                opener.assert_not_called()

    def test_fixture_blocks_real_network_even_when_port_is_production(self):
        with mock.patch.object(upgrade.config, "port", return_value=8180):
            with self.assertRaisesRegex(AssertionError, "禁止真实网络"):
                upgrade._stop_independent()

    def test_stop_shutdown_timeout_returns_1(self):
        # 引擎未在超时内退出 → 1（绝不忽略停机失败放行）
        with mock.patch.object(upgrade, "_engine_health", return_value=self._health()), \
                self._fake_shutdown({"instance_id": "abc"}), \
                mock.patch.object(upgrade, "_pid_alive", return_value=True), \
                mock.patch.object(upgrade, "_port_closed", return_value=False), \
                mock.patch.object(upgrade.time, "sleep"), \
                mock.patch.object(upgrade.time, "monotonic", side_effect=[0, 1, 1000]):
            self.assertEqual(upgrade._stop_independent(), 1)

    def test_stop_timeout_returns_1(self):
        # node 1074 缺陷4：/health 超时 ≠ 拒绝连接，不得当「无引擎」返 0 放行
        with mock.patch.object(upgrade, "_engine_health",
                               side_effect=RuntimeError("健康检查超时")):
            self.assertEqual(upgrade._stop_independent(), 1)

    def test_engine_health_timeout_raises(self):
        # node 1074 缺陷4：超时异常分类——socket.timeout/TimeoutError 一律抛 RuntimeError
        import urllib.error as urlerr
        for exc in (urlerr.URLError(socket.timeout("timed out")), TimeoutError("timed out")):
            with self.subTest(exc=type(exc).__name__):
                opener = mock.Mock()
                opener.open.side_effect = exc
                with mock.patch.object(upgrade.urllib.request, "build_opener", return_value=opener), \
                        mock.patch.object(upgrade.config, "host", return_value="127.0.0.1"), \
                        mock.patch.object(upgrade.config, "port", return_value=1):
                    with self.assertRaises(RuntimeError):
                        upgrade._engine_health()

    def test_engine_health_refused_returns_none(self):
        # 明确拒绝连接（无监听）→ None（无引擎）
        import urllib.error as urlerr
        opener = mock.Mock()
        opener.open.side_effect = urlerr.URLError(ConnectionRefusedError("refused"))
        with mock.patch.object(upgrade.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(upgrade.config, "host", return_value="127.0.0.1"), \
                mock.patch.object(upgrade.config, "port", return_value=1):
            self.assertIsNone(upgrade._engine_health())

    def test_engine_health_connection_reset_raises(self):
        # node 1079 缺陷2：ConnectionReset 等非超时非拒绝 reason 不得当「无引擎」放行
        import urllib.error as urlerr
        for reason in (ConnectionResetError("reset"), PermissionError("denied"), OSError("weird")):
            with self.subTest(reason=type(reason).__name__):
                opener = mock.Mock()
                opener.open.side_effect = urlerr.URLError(reason)
                with mock.patch.object(upgrade.urllib.request, "build_opener", return_value=opener), \
                        mock.patch.object(upgrade.config, "host", return_value="127.0.0.1"), \
                        mock.patch.object(upgrade.config, "port", return_value=1):
                    with self.assertRaises(RuntimeError):
                        upgrade._engine_health()

    def test_stop_connection_reset_returns_1(self):
        # node 1079 缺陷2：停机闸门遇 ConnectionReset → 1（不放行）
        with mock.patch.object(upgrade, "_engine_health",
                               side_effect=RuntimeError("健康检查异常")):
            self.assertEqual(upgrade._stop_independent(), 1)

    def test_safe_stop_delegates_to_independent(self):
        # _safe_stop 统一走独立停机闸门，require 参数不再改行为（node 1071 P1）
        with mock.patch.object(upgrade, "_stop_independent", return_value=0) as m:
            self.assertEqual(upgrade._safe_stop(require=False), 0)
            self.assertEqual(upgrade._safe_stop(require=True), 0)
            self.assertEqual(m.call_count, 2)


class VerifyMigrationTest(UpgradeTestBase):
    def test_verify_migration_row_factory_and_isolated_copy(self):
        # 缺陷②+③：迁移连接 row_factory=sqlite3.Row；用独立副本（不写回滚快照）
        backup_db = self.root / "src.db"
        backup_db.write_bytes(b"db-bytes")
        with mock.patch.object(upgrade, "venv_python", return_value=Path(sys.executable)), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(upgrade._verify_migration(backup_db), 0)
        args = run.call_args[0][0]
        code = args[args.index("-c") + 1]
        self.assertIn("conn.row_factory = sqlite3.Row", code)
        self.assertNotEqual(args[-1], str(backup_db))
        self.assertEqual(backup_db.read_bytes(), b"db-bytes")

    def test_verify_migration_no_backup_db_returns_0(self):
        with mock.patch.object(upgrade, "venv_python", return_value=Path(sys.executable)):
            self.assertEqual(upgrade._verify_migration(self.root / "missing.db"), 0)


class WalBackupTest(UpgradeTestBase):
    """node 1074 缺陷3：备份漏 WAL——直接复制主文件丢 SQLite WAL 已提交数据，改 sqlite3 backup API。"""

    def test_backup_includes_wal_committed_data(self):
        # 重建库为 WAL 模式，写入后不 checkpoint（数据驻留 WAL），备份必须含该行
        import sqlite3
        db_file = Path(upgrade.config.db_path())
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(db_file) + suffix).unlink()
            except FileNotFoundError:
                pass
        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('wal-committed')")
        conn.commit()
        # 不关闭连接：保持 WAL 文件存在，模拟引擎停机前快照场景
        try:
            backup = self.root / "b"
            upgrade._backup(backup)
            restored = backup / "data" / "group.db"
            self.assertTrue(restored.exists())
            self.assertFalse((backup / "data" / "group.db-wal").exists())  # backup API 产出单文件快照
            check = sqlite3.connect(str(restored))
            rows = check.execute("SELECT v FROM t").fetchall()
            check.close()
            self.assertIn(("wal-committed",), rows)
        finally:
            conn.close()

    def test_backup_db_file_is_valid_sqlite(self):
        # 备份产物必须是可用 SQLite 库（backup API 语义，非文件字节流）
        import sqlite3
        conn = sqlite3.connect(str(Path(upgrade.config.db_path())))
        conn.execute("INSERT INTO t VALUES ('extra')")
        conn.commit()
        conn.close()
        backup = self.root / "b"
        upgrade._backup(backup)
        restored = backup / "data" / "group.db"
        check = sqlite3.connect(str(restored))
        check.execute("SELECT count(*) FROM sqlite_master").fetchone()
        check.close()


class RollbackTest(UpgradeTestBase):
    def _rollback_mocks(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(upgrade, "_stop_independent", return_value=0))
        stack.enter_context(mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)))
        stack.enter_context(mock.patch.object(upgrade, "_verify_ready", return_value=0))
        return stack

    def test_rollback_restores_data_and_program(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        (self.prog / "engine" / "main.py").write_text("broken", encoding="utf-8")
        import sqlite3
        conn = sqlite3.connect(str(self.data / "group.db"))
        conn.execute("UPDATE t SET v='broken-db'")
        conn.commit()
        conn.close()
        with self._rollback_mocks():
            self.assertEqual(upgrade._rollback(backup), 0)
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "old")
        conn = sqlite3.connect(str(self.data / "group.db"))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)

    def test_rollback_independent_stop_failure_aborts(self):
        # 缺陷③：恢复入口独立——停机失败绝不忽略放行
        backup = self.root / "b"
        upgrade._backup(backup)
        with mock.patch.object(upgrade, "_stop_independent", return_value=1):
            self.assertEqual(upgrade._rollback(backup), 1)

    def test_rollback_removes_new_version_files(self):
        # 缺陷⑥：程序恢复基于发布清单，回滚删除升级新增文件（不残留新版本文件）
        backup = self.root / "b"
        upgrade._backup(backup)
        # 模拟升级落地清单记录了 new_feature.py（升级引入、旧清单没有）
        (self.prog / "engine" / "new_feature.py").write_text("new", encoding="utf-8")
        upgrade._write_new_manifest(backup, {"engine/new_feature.py"})
        with self._rollback_mocks():
            self.assertEqual(upgrade._rollback(backup), 0)
        self.assertFalse((self.prog / "engine" / "new_feature.py").exists())
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "old")

    def test_rollback_preserves_user_file_without_landed_manifest(self):
        # node 1074 缺陷1：回滚只删落地清单内文件；无落地清单（旧格式备份）不删任何文件
        backup = self.root / "b"
        upgrade._backup(backup)
        (self.prog / "user_notes.txt").write_text("user", encoding="utf-8")
        with self._rollback_mocks():
            self.assertEqual(upgrade._rollback(backup), 0)
        self.assertTrue((self.prog / "user_notes.txt").exists())

    def test_rollback_preserves_user_file_even_in_landed_manifest(self):
        # 落地清单外的用户文件即使升级后存在也不删（只删 landed - old_set）
        backup = self.root / "b"
        upgrade._backup(backup)
        (self.prog / "user_notes.txt").write_text("user", encoding="utf-8")
        upgrade._write_new_manifest(backup, {"engine/main.py", "requirements.lock"})
        with self._rollback_mocks():
            self.assertEqual(upgrade._rollback(backup), 0)
        self.assertTrue((self.prog / "user_notes.txt").exists())

    def test_rollback_install_failure_propagates(self):
        # 缺陷⑤：依赖恢复失败传播 + 就绪校验，不忽略 install 返回码
        backup = self.root / "b"
        upgrade._backup(backup)
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(upgrade._rollback(backup), 1)

    def test_rollback_install_failure_writes_evidence(self):
        # 缺陷④：依赖重建失败隔离回滚证据
        backup = self.root / "b"
        upgrade._backup(backup)
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=1)):
            upgrade._rollback(backup)
        self.assertTrue((backup / "rollback_evidence.json").exists())

    def test_rollback_no_complete_snapshot_returns_1(self):
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.object(upgrade, "_stop_independent", return_value=0):
            self.assertEqual(upgrade._rollback(empty), 1)


class ManifestIntegrityTest(UpgradeTestBase):
    # 缺陷①：manifest 完整性验证——删除/覆盖前验证必需文件存在+大小/摘要+清单结构
    def test_verify_backup_detects_missing_file(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        (backup / "program" / "engine" / "main.py").unlink()
        self.assertFalse(upgrade._verify_backup(backup))

    def test_verify_backup_detects_corrupted_sha(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        (backup / "program" / "engine" / "main.py").write_text("tampered", encoding="utf-8")
        self.assertFalse(upgrade._verify_backup(backup))

    def test_verify_backup_bad_structure(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        (backup / upgrade.MANIFEST).write_text("{not json", encoding="utf-8")
        self.assertFalse(upgrade._verify_backup(backup))

    def test_rollback_corrupted_backup_rejected_not_delete_data(self):
        # 损坏备份拒绝，不删原数据
        backup = self.root / "b"
        upgrade._backup(backup)
        (backup / "program" / "engine" / "main.py").unlink()
        orig_prog = (self.prog / "engine" / "main.py").read_text(encoding="utf-8")
        with mock.patch.object(upgrade, "_stop_independent", return_value=0):
            self.assertEqual(upgrade._rollback(backup), 1)
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), orig_prog)

    def test_latest_backup_skips_corrupted(self):
        backups = self.data / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        good = backups / "upgrade-good"
        upgrade._backup(good)
        bad = backups / "upgrade-bad"
        upgrade._backup(bad)
        (bad / "program" / "engine" / "main.py").unlink()
        self.assertEqual(upgrade.latest_backup(), good)


class InstanceBindingTest(UpgradeTestBase):
    # 缺陷②：快照绑定路径/实例身份——恢复前核对当前目标，配置变化拒绝
    def test_check_instance_binding_mismatch_rejected(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        manifest = upgrade._read_manifest(backup)
        with mock.patch.object(upgrade.config, "db_path", return_value=str(self.root / "other.db")):
            self.assertFalse(upgrade._check_instance_binding(manifest))

    def test_rollback_instance_mismatch_rejected(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
                mock.patch.object(upgrade.config, "db_path", return_value=str(self.root / "other.db")):
            self.assertEqual(upgrade._rollback(backup), 1)


class ForwardDeleteTest(UpgradeTestBase):
    # 正向升级删除语义——基于旧/新受管理发布清单删新版已移除的受管文件（文件级）
    def test_remove_forward_deletes_removed_managed_file(self):
        backup = self.root / "b"
        upgrade._backup(backup)
        # 旧受管清单含 engine/legacy.py（真实发布文件），新版本移除了它
        (self.prog / "engine" / "legacy.py").write_text("legacy", encoding="utf-8")
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        (new_src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        (new_src / "scripts").mkdir()
        (new_src / "scripts" / "install.py").write_text("pass", encoding="utf-8")
        upgrade._remove_forward(new_src, backup,
                                old_managed={"engine/main.py", "engine/legacy.py", "scripts/install.py"})
        self.assertFalse((self.prog / "engine" / "legacy.py").exists())
        self.assertTrue((self.prog / "engine" / "main.py").exists())  # 新包仍含 → 不删

    def test_remove_forward_never_deletes_directory(self):
        # node 1084 缺陷2：清单项现场为目录 → 拒绝整目录删除（保守跳过）
        (self.prog / "engine_old").mkdir()
        (self.prog / "engine_old" / "user.txt").write_text("keep", encoding="utf-8")
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        upgrade._remove_forward(new_src, self.root / "b", old_managed={"engine_old"})
        self.assertTrue((self.prog / "engine_old" / "user.txt").exists())

    def test_remove_forward_preserves_unmanaged_in_managed_dir(self):
        # node 1084 缺陷2：受管目录内清单外文件不被正向删除波及（文件级删除）
        (self.prog / "engine" / "user_notes.txt").write_text("USER-DATA", encoding="utf-8")
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        (new_src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        # 旧清单只声明 engine/legacy.py（已移除）与 engine/main.py
        upgrade._remove_forward(new_src, self.root / "b",
                                old_managed={"engine/main.py", "engine/legacy.py"})
        self.assertTrue((self.prog / "engine" / "user_notes.txt").exists())

    def test_remove_forward_without_managed_manifest_deletes_nothing(self):
        # node 1079 缺陷1：无受管清单依据 → fail-closed 不删（哪怕备份 manifest 里有）
        backup = self.root / "b"
        upgrade._backup(backup)
        (self.prog / "engine_old").mkdir()
        (self.prog / "engine_old" / "legacy.py").write_text("legacy", encoding="utf-8")
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        upgrade._remove_forward(new_src, backup, old_managed=None)
        self.assertTrue((self.prog / "engine_old" / "legacy.py").exists())





class ProtectedPathsTest(UpgradeTestBase):
    """P0 数据保护：删除只基于受管理发布清单边界，保护 .git/自定义数据目录/用户文件 + 路径范围校验。"""

    def test_backup_excludes_git(self):
        (self.prog / ".git").mkdir()
        (self.prog / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        backup = self.root / "b"
        upgrade._backup(backup)
        self.assertFalse((backup / "program" / ".git").exists())

    def test_remove_forward_protects_git(self):
        # .git 不在发布清单，正向删除不得删它
        (self.prog / ".git").mkdir()
        (self.prog / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        backup = self.root / "b"
        upgrade._backup(backup)
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        upgrade._remove_forward(new_src, backup)
        self.assertTrue((self.prog / ".git" / "HEAD").exists())

    def test_protected_top_names_includes_custom_data_dir(self):
        # 自定义数据目录落在程序根内时，被纳入受保护顶层名
        custom_data = self.prog / "custom_data"
        custom_data.mkdir()
        with mock.patch.object(upgrade.config, "data_dir", return_value=custom_data):
            self.assertIn("custom_data", upgrade._protected_top_names())

    def test_protected_top_names_includes_custom_db_file_itself(self):
        # node 1074 缺陷2：GROUP_DB 直接指向程序根内 custom.sqlite 时，db 文件顶层名本身受保护
        custom_db = self.prog / "custom.sqlite"
        custom_db.write_text("db", encoding="utf-8")
        with mock.patch.object(upgrade.config, "db_path", return_value=str(custom_db)):
            self.assertIn("custom.sqlite", upgrade._protected_top_names())

    def test_remove_forward_protects_custom_db_file(self):
        # node 1074 缺陷2：自定义 db 文件直接放程序根（parent=根），不得被正向删除
        custom_db = self.prog / "custom.sqlite"
        _make_real_sqlite(custom_db)
        with mock.patch.object(upgrade.config, "db_path", return_value=str(custom_db)):
            backup = self.root / "b"
            upgrade._backup(backup)
            new_src = self.root / "newsrc"
            (new_src / "engine").mkdir(parents=True)
            upgrade._remove_forward(new_src, backup)
        import sqlite3
        conn = sqlite3.connect(str(custom_db))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)

    def test_land_program_skips_custom_db_file(self):
        # node 1074 缺陷2：新包内同名 db 文件不得覆盖程序根内自定义 db
        custom_db = self.prog / "custom.sqlite"
        _make_real_sqlite(custom_db)
        with mock.patch.object(upgrade.config, "db_path", return_value=str(custom_db)):
            backup = self.root / "b"
            upgrade._backup(backup)
            manifest = upgrade._read_manifest(backup)
            old_managed = upgrade._top_level(manifest.get("program_files", {}))
            src = self.root / "src"
            src.mkdir()
            (src / "custom.sqlite").write_text("hijack", encoding="utf-8")
            upgrade._land_program(src, old_managed)
        import sqlite3
        conn = sqlite3.connect(str(custom_db))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)

    def test_remove_forward_protects_custom_data_dir(self):
        # 程序根内的自定义数据目录不得被当程序文件删除
        custom_data = self.prog / "custom_data"
        custom_data.mkdir()
        (custom_data / "user.db").write_text("userdata", encoding="utf-8")
        with mock.patch.object(upgrade.config, "data_dir", return_value=custom_data):
            backup = self.root / "b"
            upgrade._backup(backup)
            new_src = self.root / "newsrc"
            (new_src / "engine").mkdir(parents=True)
            upgrade._remove_forward(new_src, backup)
        self.assertTrue((self.prog / "custom_data" / "user.db").exists())

    def test_delete_within_root_rejects_escape(self):
        # 路径范围校验：拒绝删除程序根之外的路径
        outside = self.root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        self.assertFalse(upgrade._delete_within_root(outside))
        self.assertTrue(outside.exists())

    def test_remove_forward_preserves_user_file(self):
        # 用户文件（非发布清单项、非数据项）不得被正向删除：只删旧清单有而新版没有的顶层项
        backup = self.root / "b"
        upgrade._backup(backup)
        (self.prog / "user_notes.txt").write_text("user", encoding="utf-8")
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        upgrade._remove_forward(new_src, backup)
        self.assertTrue((self.prog / "user_notes.txt").exists())


class RollbackRebuildTest(UpgradeTestBase):
    """P1 旧依赖/解释器重建：回滚依赖恢复重建旧解释器/依赖集合（--rebuild --python <旧基础解释器>）。"""

    def test_backup_records_old_venv_base_python(self):
        with mock.patch.object(upgrade, "_venv_base_python", return_value="C:/py310/python.exe"):
            backup = self.root / "b"
            upgrade._backup(backup)
        manifest = upgrade._read_manifest(backup)
        self.assertEqual(manifest["instance"]["venv_base_python"], "C:/py310/python.exe")

    def test_rollback_install_uses_rebuild_and_old_interpreter(self):
        backup = self.root / "b"
        with mock.patch.object(upgrade, "_venv_base_python", return_value="C:/py310/python.exe"):
            upgrade._backup(backup)
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade._rollback(backup), 0)
        args = run.call_args_list[-1][0][0]
        self.assertIn("--rebuild", args)
        self.assertIn("--python", args)
        self.assertIn("C:/py310/python.exe", args)

    def test_rollback_install_always_rebuilds_even_without_base(self):
        # 缺旧基础解释器时仍 --rebuild（重建清掉升级残留新依赖），不回退 --python
        backup = self.root / "b"
        upgrade._backup(backup)
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade._rollback(backup), 0)
        args = run.call_args_list[-1][0][0]
        self.assertIn("--rebuild", args)
        self.assertNotIn("--python", args)


class ReleaseManifestTest(UpgradeTestBase):
    """node 1079 缺陷1：old_managed 必须来自受管理发布清单，绝不用程序根扫描。

    原始复现顺序（bot-36 复现）：先建用户笔记 → 再备份 → 备份把笔记扫描进
    manifest.program_files → old_managed 含 notes.txt → 落地同名覆盖 + 正向删除删笔记。
    """

    def test_notes_created_before_backup_survive_upgrade(self):
        # 先建笔记（会被 _backup 扫描进 manifest——这是旧行为的事实），再备份，
        # 然后升级落地：笔记必须保留（新版 old_managed 用发布清单，不认程序根扫描结果）
        (self.prog / "notes.txt").write_text("user-notes", encoding="utf-8")
        backup = self.root / "b"
        upgrade._backup(backup)
        m = upgrade._read_manifest(backup)
        # 确认旧备份确实把笔记扫进了 manifest（复现前提成立）
        self.assertIn("notes.txt", m["program_files"])
        # do_upgrade 的清单来源：当前程序根无 release_manifest，新包随附清单不含笔记
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        (new_src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        (new_src / "release_manifest.json").write_text(
            json.dumps({"program_files": ["engine/main.py"]}), encoding="utf-8")
        # 模拟 do_upgrade 的清单决策（不跑完整 do_upgrade，聚焦清单来源）
        old_managed = upgrade._current_release_manifest()
        if old_managed is None:
            old_managed = upgrade._collect_release_manifest(new_src)
        old_tops = {r.split("/", 1)[0] for r in old_managed}
        self.assertNotIn("notes.txt", old_tops)  # 笔记不在受管清单 → 不删不覆盖
        landed = upgrade._land_program(new_src, old_tops)
        self.assertEqual((self.prog / "notes.txt").read_text(encoding="utf-8"), "user-notes")

    def test_old_backup_manifest_scan_not_used_for_delete(self):
        # 回滚清理也不认程序根扫描：备份 manifest 混入的笔记不在落地清单 → 回滚不删
        (self.prog / "notes.txt").write_text("user-notes", encoding="utf-8")
        backup = self.root / "b"
        upgrade._backup(backup)
        self.assertIn("notes.txt", upgrade._read_manifest(backup)["program_files"])
        upgrade._write_new_manifest(backup, {"engine/main.py", "engine/extra.py"})  # 落地清单（extra 为升级引入）
        (self.prog / "engine" / "extra.py").write_text("x", encoding="utf-8")
        with mock.patch.object(upgrade, "_stop_independent", return_value=0), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade._rollback(backup), 0)
        self.assertEqual((self.prog / "notes.txt").read_text(encoding="utf-8"), "user-notes")
        self.assertFalse((self.prog / "engine" / "extra.py").exists())  # 清单内新增仍删

    def test_release_manifest_landed_and_reused(self):
        # 新包随附清单落地程序根，下次升级直接复用（不再依赖程序根扫描）
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        (new_src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        (new_src / "release_manifest.json").write_text(
            json.dumps({"program_files": ["engine/main.py"]}), encoding="utf-8")
        landed = upgrade._land_release_manifest(new_src)
        self.assertEqual(landed, {"engine/main.py"})
        self.assertTrue((self.prog / "release_manifest.json").exists())
        self.assertEqual(upgrade._current_release_manifest(), {"engine/main.py"})

    def test_release_manifest_missing_treated_conservatively(self):
        # node 1089 语义：程序根无持久化清单（老存量环境）→ _current_release_manifest
        # 返 None；落地层在显式给定受管集合时保守跳过非清单文件（不删不覆盖）。
        (self.prog / "release_manifest.json").unlink()  # 基座预置清单模拟「已升级环境」，此处显式移除
        self.assertIsNone(upgrade._current_release_manifest())
        (self.prog / "notes.txt").write_text("user-notes", encoding="utf-8")
        new_src = self.root / "newsrc"
        (new_src / "engine").mkdir(parents=True)
        (new_src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        old_managed: set[str] = set()
        for item in new_src.iterdir():
            if item.name in upgrade._protected_top_names():
                continue
            if item.is_dir():
                old_managed.update(f"{item.name}/{r}" for r in upgrade._collect_files(item, set()))
            else:
                old_managed.add(item.name)
        old_tops = {r.split("/", 1)[0] for r in old_managed}
        self.assertNotIn("notes.txt", old_tops)
        landed = upgrade._land_program(new_src, old_tops)
        self.assertEqual((self.prog / "notes.txt").read_text(encoding="utf-8"), "user-notes")


class DoUpgradeTest(UpgradeTestBase):
    def _make_zip(self, version, with_manifest=True):
        src = self.root / f"v{version}"
        (src / "engine").mkdir(parents=True)
        (src / "engine" / "main.py").write_text(f"v{version}", encoding="utf-8")
        (src / "scripts").mkdir()
        (src / "scripts" / "install.py").write_text("pass\n", encoding="utf-8")
        (src / "requirements.lock").write_text(f"fastapi=={version}\n", encoding="utf-8")
        if with_manifest:
            # 发布包随附受管理发布清单（node 1084：升级入口要求可辨识的受管边界）
            (src / "release_manifest.json").write_text(json.dumps({"program_files": [
                "engine/main.py", "scripts/install.py", "requirements.lock",
            ]}), encoding="utf-8")
        zp = self.root / f"v{version}.zip"
        with zipfile.ZipFile(zp, "w") as z:
            for root, _, files in os.walk(src):
                for f in files:
                    full = Path(root) / f
                    z.write(full, full.relative_to(src))
        sha = hashlib.sha256(zp.read_bytes()).hexdigest()
        return zp, sha

    def _patch_download(self, zp):
        return mock.patch.object(upgrade._bs, "_download",
                                 side_effect=lambda url, dest: shutil.copyfile(zp, dest))

    def test_do_upgrade_success(self):
        zp, sha = self._make_zip(2)
        with self._patch_download(zp), \
                mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                mock.patch.object(upgrade, "_sync_deps", return_value=0), \
                mock.patch.object(upgrade, "_verify_migration", return_value=0), \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 0)
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "v2")
        import sqlite3
        conn = sqlite3.connect(str(self.data / "group.db"))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)

    def test_do_upgrade_failure_rolls_back(self):
        zp, sha = self._make_zip(2)
        with self._patch_download(zp), \
                mock.patch.object(upgrade, "_stop_independent", return_value=0) as stop, \
                mock.patch.object(upgrade, "_sync_deps", return_value=1), \
                mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 1)
        self.assertEqual(stop.call_count, 2)  # Normal stop and rollback stop are both isolated.
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "old")
        import sqlite3
        conn = sqlite3.connect(str(self.data / "group.db"))
        rows = conn.execute("SELECT v FROM t").fetchall()
        conn.close()
        self.assertIn(("dbdata",), rows)

    def test_backup_collision_never_enters_rollback(self):
        zp, sha = self._make_zip(2)
        with self._patch_download(zp), \
                mock.patch.object(upgrade, "_ts", return_value="fixed-second"), \
                mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                mock.patch.object(upgrade, "_sync_deps", return_value=0), \
                mock.patch.object(upgrade, "_verify_migration", return_value=0), \
                mock.patch.object(upgrade, "_verify_ready", return_value=0), \
                mock.patch.object(upgrade, "_rollback") as rollback:
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 0)
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 1)
            rollback.assert_not_called()
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "v2")

    def test_failed_preparation_never_enters_rollback(self):
        import sqlite3
        zp, sha = self._make_zip(2)
        for phase, error in (("_backup", sqlite3.DatabaseError("bad snapshot")),
                             ("_fetch_and_extract", RuntimeError("bad download"))):
            with self.subTest(phase=phase), \
                    self._patch_download(zp), \
                    mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                    mock.patch.object(upgrade, phase, side_effect=error), \
                    mock.patch.object(upgrade, "_rollback") as rollback:
                self.assertEqual(upgrade.do_upgrade(str(zp), sha), 1)
                rollback.assert_not_called()

    def test_do_upgrade_writes_new_manifest(self):
        # node 1074 缺陷1 配套：升级成功写落地清单，回滚才能只删清单内新增文件
        zp, sha = self._make_zip(2)
        with self._patch_download(zp), \
                mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                mock.patch.object(upgrade, "_sync_deps", return_value=0), \
                mock.patch.object(upgrade, "_verify_migration", return_value=0), \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 0)
        backups = upgrade.config.backups_dir()
        dirs = sorted(d for d in backups.iterdir() if d.name.startswith("upgrade-"))
        self.assertTrue(dirs)
        import json as _json
        landed = _json.loads((dirs[-1] / "new_manifest.json").read_text(encoding="utf-8"))
        self.assertIn("engine/main.py", landed["program_files"])

    def test_do_upgrade_preserves_user_file(self):
        # node 1074 缺陷1：非清单用户文件升级全程保留（新包同名文件不覆盖用户文件）
        # 先备份（建立旧发布清单），再放用户文件（清单外）
        backup0 = self.root / "b0"
        upgrade._backup(backup0)
        m0 = upgrade._read_manifest(backup0)
        self.assertNotIn("user_notes.txt", m0["program_files"])  # 备份时用户文件还不存在
        old_managed = upgrade._top_level(m0.get("program_files", {}))
        (self.prog / "user_notes.txt").write_text("user", encoding="utf-8")
        # 新包带同名 user_notes.txt 试图覆盖
        src = self.root / "src"
        (src / "engine").mkdir(parents=True)
        (src / "engine" / "main.py").write_text("v2", encoding="utf-8")
        (src / "user_notes.txt").write_text("hijack", encoding="utf-8")
        landed = upgrade._land_program(src, old_managed)
        self.assertEqual((self.prog / "user_notes.txt").read_text(encoding="utf-8"), "user")
        self.assertNotIn("user_notes.txt", landed)

    def test_do_upgrade_missing_old_manifest_fails_closed(self):
        # node 1089 缺陷1（清单语义定稿§5）：程序根缺持久化清单 → fail-closed 中止升级，
        # 不覆盖不删除任何文件、不产混合版本、不标成功。哪怕新包随附清单也不得当作旧归属。
        (self.prog / "release_manifest.json").unlink()  # 模拟极老存量环境（无清单从未升级）
        (self.prog / "notes.txt").write_text("USER-DATA", encoding="utf-8")
        zp, sha = self._make_zip(2)  # 新包随附清单（含 notes.txt 冲突源的语义不再重要）
        with self._patch_download(zp), \
                mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                mock.patch.object(upgrade, "_rollback") as rollback:
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 1)
            rollback.assert_not_called()  # 落地前中止 → 程序未变更，不得进回滚
        # 现场完全保留：用户笔记与旧程序文件原样，无任何覆盖/删除
        self.assertEqual((self.prog / "notes.txt").read_text(encoding="utf-8"), "USER-DATA")
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.prog / "requirements.lock").read_text(encoding="utf-8"), "fastapi==1\n")

    def test_do_upgrade_persisted_manifest_excludes_skipped_user_file(self):
        # node 1089 缺陷2：首次升级落地跳过的同名用户文件，不得被完整复制的随附清单
        # 划成受管——持久化清单只记「仍存在的旧受管 ∪ 实际落地」，第二次升级不得覆盖。
        # 场景（bot-36 复现）：旧程序根有用户 engine/user_notes.txt=USER-DATA，
        # 新包带同名 engine/user_notes.txt=PACKAGE-DATA 且随附清单含它：
        # 第一次落地跳过（旧清单不含它→用户文件），持久化清单若完整复制随附清单
        # 会把它收编，第二次升级即覆盖。修复后两次升级它都必须存活。
        (self.prog / "engine" / "user_notes.txt").write_text("USER-DATA", encoding="utf-8")
        zp, sha = self._make_zip(2)  # 随附清单只声明三个程序文件，不含 user_notes.txt
        # 手工构造带冲突 user_notes.txt 的包（随附清单故意含它，考验「不完整复制」）
        src = self.root / "v2"
        (src / "engine" / "user_notes.txt").write_text("PACKAGE-DATA", encoding="utf-8")
        (src / "release_manifest.json").write_text(json.dumps({"program_files": [
            "engine/main.py", "scripts/install.py", "requirements.lock",
            "engine/user_notes.txt",  # 随附清单试图收编冲突路径
        ]}), encoding="utf-8")
        zp2 = self.root / "v2c.zip"
        with zipfile.ZipFile(zp2, "w") as z:
            for r, _, fs in os.walk(src):
                for f in fs:
                    full = Path(r) / f
                    z.write(full, full.relative_to(src))
        sha2 = hashlib.sha256(zp2.read_bytes()).hexdigest()
        for i in (1, 2):  # 连续两次升级（第二次验证持久化清单没有收编用户文件）
            if i == 2:
                time.sleep(1.1)  # 备份目录秒级时间戳：两次升级须隔秒避免重名 WinError 183
            with self._patch_download(zp2), \
                    mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                    mock.patch.object(upgrade, "_sync_deps", return_value=0), \
                    mock.patch.object(upgrade, "_verify_migration", return_value=0), \
                    mock.patch.object(upgrade, "_verify_ready", return_value=0):
                self.assertEqual(upgrade.do_upgrade(str(zp2), sha2), 0)
            self.assertEqual((self.prog / "engine" / "user_notes.txt").read_text(encoding="utf-8"),
                             "USER-DATA")

    def test_do_upgrade_managed_dir_preserves_unmanaged_file(self):
        # node 1084 缺陷2 原始顺序回归：旧/新清单均只声明 engine/main.py，
        # 旧目录额外已有 engine/user_notes.txt=USER-DATA；do_upgrade 全链后
        # user_notes.txt 必须保留（文件级替换，不再整目录删除重建）。
        # 先在程序根持久化旧清单（升级入口的 old_managed 来源）
        (self.prog / "release_manifest.json").write_text(json.dumps(
            {"program_files": ["engine/main.py", "scripts/install.py", "requirements.lock"]}),
            encoding="utf-8")
        (self.prog / "engine" / "user_notes.txt").write_text("USER-DATA", encoding="utf-8")
        zp, sha = self._make_zip(2)  # 新包清单同旧清单（仅 main.py 更新）
        with self._patch_download(zp), \
                mock.patch.object(upgrade, "_safe_stop", return_value=0), \
                mock.patch.object(upgrade, "_sync_deps", return_value=0), \
                mock.patch.object(upgrade, "_verify_migration", return_value=0), \
                mock.patch.object(upgrade, "_verify_ready", return_value=0):
            self.assertEqual(upgrade.do_upgrade(str(zp), sha), 0)
        self.assertEqual((self.prog / "engine" / "user_notes.txt").read_text(encoding="utf-8"), "USER-DATA")
        self.assertEqual((self.prog / "engine" / "main.py").read_text(encoding="utf-8"), "v2")


class PartialLandingRollbackTest(UpgradeTestBase):
    def test_copy_failures_restore_snapshot_and_remove_partial_new_files(self):
        for failed_rel in ('engine/main.py', 'engine/z_partial.py'):
            with self.subTest(failed_rel=failed_rel):
                src = self.root / 'incoming'
                (src / 'engine').mkdir(parents=True)
                for rel in ('engine/a_new.py', 'engine/main.py', 'engine/z_partial.py', 'engine/user_notes.txt'):
                    (src / rel).write_text('NEW', encoding='utf-8')
                (self.prog / 'engine' / 'user_notes.txt').write_text('USER', encoding='utf-8')
                original_copy = upgrade.shutil.copy2
                original_items = upgrade._iter_package_files

                def copy(source, destination, *args, **kwargs):
                    if Path(source) == src / failed_rel:
                        Path(destination).write_text('PARTIAL', encoding='utf-8')
                        raise PermissionError('synthetic copy failure after destination write')
                    return original_copy(source, destination, *args, **kwargs)

                with mock.patch.object(upgrade, '_fetch_and_extract', return_value=src), \
                        mock.patch.object(upgrade, '_ts', return_value=Path(failed_rel).stem), \
                        mock.patch.object(upgrade, '_iter_package_files', side_effect=lambda root: sorted(original_items(root), key=lambda item: item[1])), \
                        mock.patch.object(upgrade, '_stop_independent', return_value=0), \
                        mock.patch.object(upgrade.shutil, 'copy2', side_effect=copy), \
                        mock.patch.object(upgrade.subprocess, 'run', return_value=mock.Mock(returncode=0)), \
                        mock.patch.object(upgrade, '_verify_ready', return_value=0):
                    self.assertEqual(upgrade.do_upgrade('isolated.zip', '0' * 64), 1)
                self.assertEqual((self.prog / 'engine' / 'main.py').read_text(), 'old')
                self.assertEqual((self.prog / 'engine' / 'user_notes.txt').read_text(), 'USER')
                self.assertFalse((self.prog / 'engine' / 'a_new.py').exists())
                self.assertFalse((self.prog / 'engine' / 'z_partial.py').exists())
                backup = self.data / 'backups' / ('upgrade-' + Path(failed_rel).stem)
                journal = json.loads((backup / 'new_manifest.json').read_text(encoding='utf-8'))['program_files']
                self.assertIn(failed_rel, journal)
                self.assertNotIn('engine/user_notes.txt', journal)

    def test_journal_publish_failure_keeps_prior_record_and_prevents_next_copy(self):
        src = self.root / 'incoming'
        (src / 'engine').mkdir(parents=True)
        (src / 'engine' / 'a_new.py').write_text('NEW', encoding='utf-8')
        (src / 'engine' / 'b_new.py').write_text('NEW', encoding='utf-8')
        original_replace = upgrade.os.replace
        original_items = upgrade._iter_package_files
        publishes = []

        def replace(source, destination):
            if Path(destination).name == 'new_manifest.json':
                publishes.append(Path(destination))
                if len(publishes) == 2:
                    raise OSError('synthetic journal publish failure')
            return original_replace(source, destination)

        with mock.patch.object(upgrade, '_fetch_and_extract', return_value=src), \
                mock.patch.object(upgrade, '_iter_package_files', side_effect=lambda root: sorted(original_items(root), key=lambda item: item[1])), \
                mock.patch.object(upgrade, '_stop_independent', return_value=0), \
                mock.patch.object(upgrade.os, 'replace', side_effect=replace), \
                mock.patch.object(upgrade.subprocess, 'run', return_value=mock.Mock(returncode=0)), \
                mock.patch.object(upgrade, '_verify_ready', return_value=0), \
                mock.patch.object(upgrade, '_sync_deps') as install:
            self.assertEqual(upgrade.do_upgrade('isolated.zip', '0' * 64), 1)
        install.assert_not_called()
        self.assertEqual(len(publishes), 2)
        self.assertEqual(json.loads(publishes[0].read_text(encoding='utf-8'))['program_files'], ['engine/a_new.py'])
        self.assertFalse((self.prog / 'engine' / 'a_new.py').exists())
        self.assertFalse((self.prog / 'engine' / 'b_new.py').exists())
        self.assertEqual((self.prog / 'engine' / 'main.py').read_text(), 'old')


class RollbackCmdTest(UpgradeTestBase):
    def test_rollback_no_backup_returns_1(self):
        self.assertEqual(upgrade.do_rollback(), 1)


class MakeReleaseTest(unittest.TestCase):
    """node 1094：make_release 构建必须收齐运行时模板资源（templates/）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 最小仓库结构：engine + templates 三模板 + 顶层入口
        (self.root / "engine").mkdir()
        (self.root / "engine" / "main.py").write_text("pass\n", encoding="utf-8")
        for tid in ("software", "research", "writing"):
            base = self.root / "templates" / "projects" / tid
            base.mkdir(parents=True)
            (base / "manifest.json").write_text(json.dumps(
                {"id": tid, "version": "1", "name": tid, "description": "d",
                 "roles": [], "files": [], "directory_modes": [], "workflow": {}}),
                encoding="utf-8")
            (base / "AGENTS.md.tmpl").write_text("tmpl", encoding="utf-8")
        (self.root / "install.py").write_text("pass\n", encoding="utf-8")
        # make_release.main 用 __file__ 推断仓库根；测试直接构造受管集合与打包调用
        self.files = make_release._managed_files(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_managed_files_collects_templates(self):
        # node 1094 核心：templates/projects/<id>/manifest.json 必须入受管清单
        for tid in ("software", "research", "writing"):
            self.assertIn(f"templates/projects/{tid}/manifest.json", self.files)
            self.assertIn(f"templates/projects/{tid}/AGENTS.md.tmpl", self.files)
        self.assertIn("engine/main.py", self.files)
        self.assertIn("install.py", self.files)

    def test_archive_contains_manifest_and_templates(self):
        out = self.root / "dist"
        rc = make_release.make_release(self.root, out, "probe", "t")
        self.assertEqual(rc, 0)
        zp = out / "probe-t.zip"
        self.assertTrue(zp.exists())
        with zipfile.ZipFile(zp) as z:
            names = set(z.namelist())
            m = json.loads(z.read("release_manifest.json"))
        self.assertIn("release_manifest.json", names)
        self.assertIn("VERSION", names)  # node 215：包内版本标识
        for tid in ("software", "research", "writing"):
            self.assertIn(f"templates/projects/{tid}/manifest.json", names)
            self.assertIn(f"templates/projects/{tid}/manifest.json",
                          m["program_files"])
        # 清单与包内容零差异（VERSION 与清单文件本身除外，二者由升级流程独立落地）
        self.assertEqual(sorted(set(m["program_files"]) - names), [])
        self.assertEqual(sorted(n for n in names
                                if n not in ("release_manifest.json", "VERSION")
                                and n not in set(m["program_files"])), [])


class ReleaseDiscoveryTest(UpgradeTestBase):
    """node 215 验收第 5 条：默认升级自动发现最新正式 Release。"""

    def test_pick_formal_release_skips_prerelease_and_sorts(self):
        releases = [
            {"version": "1.9.0", "prerelease": False},
            {"version": "v1.10.0"},  # v 前缀 + 数值段比较（1.10 > 1.9）
            {"version": "v2.0.0-rc1", "prerelease": True},  # 预发布跳过
            {"version": "abc", "prerelease": False},  # 无法解析跳过
        ]
        picked = upgrade._pick_formal_release(releases)
        self.assertEqual(picked["version"], "v1.10.0")

    def test_pick_formal_release_rejects_all_prerelease(self):
        with self.assertRaises(RuntimeError):
            upgrade._pick_formal_release([{"version": "1.0.0", "prerelease": True}])

    def test_discover_release_requires_complete_assets(self):
        index = [{"version": "2.0.0", "source": "http://x/v2.zip", "sha256": ""}]
        with mock.patch.object(upgrade, "_fetch_release_index", return_value=index):
            with self.assertRaises(RuntimeError):
                upgrade.discover_release()

    def test_resolve_target_no_update_returns_none(self):
        # 当前版本 = 最新正式版本 → 无更新明确提示（None → main 退出 0）
        (self.prog / "VERSION").write_text("2.0.0", encoding="utf-8")
        index = [{"version": "2.0.0", "source": "http://x/v2.zip", "sha256": "ab" * 32}]
        index[0].update(manifest="http://x/release_manifest.json", manifest_sha256="cd" * 32)
        with mock.patch.object(upgrade, "_fetch_release_index", return_value=index):
            self.assertIsNone(upgrade._resolve_upgrade_target(None, None))

    def test_resolve_target_newer_release_returns_url_sha(self):
        (self.prog / "VERSION").write_text("1.0.0", encoding="utf-8")
        index = [{"version": "2.0.0", "source": "http://x/v2.zip", "sha256": "cd" * 32}]
        index[0].update(manifest="http://x/release_manifest.json", manifest_sha256="ef" * 32)
        with mock.patch.object(upgrade, "_fetch_release_index", return_value=index):
            self.assertEqual(upgrade._resolve_upgrade_target(None, None),
                             upgrade.releases.Release("2.0.0", "http://x/v2.zip", "cd" * 32,
                                                      "http://x/release_manifest.json", "ef" * 32))

    def test_resolve_target_explicit_url_wins(self):
        # 显式 --url/--sha256 优先，不做发现
        with mock.patch.object(upgrade, "_fetch_release_index",
                               side_effect=AssertionError("不应触达发现源")):
            self.assertEqual(upgrade._resolve_upgrade_target("http://y.zip", "ef" * 32),
                             upgrade.releases.Release(None, "http://y.zip", "ef" * 32))

    def test_resolve_target_half_explicit_rejected(self):
        with self.assertRaises(RuntimeError):
            upgrade._resolve_upgrade_target("http://y.zip", None)
        with self.assertRaises(RuntimeError):
            upgrade._resolve_upgrade_target(None, "ab" * 32)

    def test_missing_index_url_uses_public_release_source(self):
        with mock.patch.dict(os.environ, {"RELEASE_INDEX_URL": ""}), \
             mock.patch.object(upgrade.releases, "fetch_index", return_value=[]) as fetch:
            self.assertEqual(upgrade._fetch_release_index(), [])
            fetch.assert_called_once_with(upgrade.releases.DEFAULT_INDEX_URL)

    def test_main_upgrade_no_update_exits_zero(self):
        # main 无参：无更新 → 0（真实入口）
        (self.prog / "VERSION").write_text("1.5.0", encoding="utf-8")
        index = [{"version": "1.5.0", "source": "http://x.zip", "sha256": "ab" * 32}]
        index[0].update(manifest="http://x/release_manifest.json", manifest_sha256="cd" * 32)
        with mock.patch.object(upgrade, "_fetch_release_index", return_value=index):
            self.assertEqual(upgrade.main(["upgrade"]), 0)

    def test_main_upgrade_index_failure_keeps_current(self):
        # 发现源失败 → 明确退出 1，保留当前版本
        with mock.patch.object(upgrade, "_fetch_release_index",
                               side_effect=OSError("net down")):
            self.assertEqual(upgrade.main(["upgrade"]), 1)


if __name__ == "__main__":
    unittest.main()
