"""req#122 task#214 升级入口与失败恢复：安全停机 → 备份 → 获取新版本 → 同步依赖 → 迁移验证 → 就绪验证。

子命令：
  upgrade   完整升级（默认自动发现最新正式 Release，无需用户指定版本）；失败自动回滚到升级前快照
  rollback  手动回滚到最近一次升级备份

环境变量（命令行参数优先）：
  UPGRADE_URL      新版本源码归档地址（zip/tar.gz；显式指定则跳过自动发现）
  UPGRADE_SHA256   归档 SHA256 摘要（与 UPGRADE_URL 配对）
  RELEASE_INDEX_URL  Release 索引地址（自动发现默认源；返回 {"releases":[...]} JSON，
                   每项含 version/source/sha256/prerelease，缺省时明确失败提示配置）

node 215 升级交互（用户 7212）：默认入口无需填 commit——自动发现最新正式 Release，
显示当前及最新版本，固定本次版本与资产（源码 zip + release_manifest + SHA256）；
无更新明确提示退出；Release 查询失败/缺必需资产/校验失败明确失败并保留当前可用版本。

程序/数据分离：升级只换程序（源码 + venv），不动数据目录（group.db / attachments / .env 保留在
data_dir）。升级前完整备份数据目录必要数据 + 程序源码到 backups/upgrade-<ts>/，失败恢复旧程序/数据快照。

关键不变量（node 1061 / 1066 关单核验缺陷修复）：
- 备份事务化：备份先落临时目录，完整收集 + 写 manifest.json（含文件清单/大小/SHA256 + 实例绑定绝对路径）
  后原子 rename 发布；latest_backup 只认完整快照。
- 完整性验证：删除/覆盖前校验 manifest 结构、必需文件存在 + 大小/摘要，损坏备份拒绝不删原数据。
- 快照绑定路径/实例身份：manifest 记录 program_root/db_path/attachments_dir 绝对路径，恢复前核对当前目标，
  配置变化拒绝（防跨库覆盖）。
- 迁移验证用独立工作副本（不改写唯一回滚快照），连接设 row_factory=sqlite3.Row（init_schema 按 r['name'] 读）。
- 恢复入口独立：回滚停机不依赖损坏的新版 stop/venv（用系统 python 直查/终止进程），绝不忽略停机失败放行。
- 依赖恢复可验证：回滚重建旧解释器/依赖集合（不原样复用 venv），补新依赖残留清理，失败隔离回滚证据。
- 正向升级删除语义：基于旧/新发布清单，删除新版已移除的顶层模块。
- 备份/恢复走 config.db_path()/config.attachments_dir() 统一路径（尊重 GROUP_DB/GROUP_ATTACHMENTS_DIR 覆盖）。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROGRAM_ROOT = Path(__file__).resolve().parent.parent
if str(PROGRAM_ROOT) not in sys.path:
    sys.path.insert(0, str(PROGRAM_ROOT))

from engine import config  # noqa: E402
from scripts import bootstrap as _bs  # noqa: E402
from scripts import releases  # noqa: E402

# 固定数据项：升级只换程序，这些跳过（保留在数据目录/程序根存量环境）。
# .git 纳入保护（VCS 元数据不是程序文件，不得被正向删除/回滚残留清理当程序删掉）。
_DATA_ITEMS = {"group.db", "attachments", ".env", "backups", "logs", ".venv", "install.json", ".git"}
MANIFEST = "manifest.json"
# 受管理发布清单：版本随附的程序文件清单（node 1079 缺陷1）。落地新源码时随包带来并
# 持久化到程序根；升级的覆盖/删除只认它，绝不用程序根扫描结果当清单（用户笔记等非受管
# 理文件一旦被扫描进 manifest 就会被误删/误覆盖）。缺清单视为旧版本环境，保守只落地不删。
RELEASE_MANIFEST = "release_manifest.json"


def _protected_top_names() -> set[str]:
    """受保护顶层名：固定数据项 + 自定义数据路径（含 db 文件本身，落在程序根内时）。

    删除/覆盖/备份程序文件时跳过这些，防自定义数据目录（GROUP_DATA_DIR/GROUP_DB/
    GROUP_ATTACHMENTS_DIR 指向程序根子目录）、自定义 db 文件（GROUP_DB 直接指向
    程序根内 custom.sqlite 等）与 .git 被当程序文件删除（node 1071 P0 / node 1074 缺陷2）。
    """
    names = set(_DATA_ITEMS)
    prog = config.program_root().resolve()
    for p in (config.data_dir(), Path(config.db_path()).resolve().parent,
              config.attachments_dir(), config.backups_dir(), config.logs_dir()):
        try:
            rel = p.resolve().relative_to(prog)
        except (ValueError, OSError):
            continue
        if rel.parts:
            names.add(rel.parts[0])
    # db 文件本身（db 文件直接放程序根时，其顶层名即文件名；parent=根时上面保护不到）
    try:
        db_rel = Path(config.db_path()).resolve().relative_to(prog)
    except (ValueError, OSError):
        db_rel = None
    if db_rel is not None and db_rel.parts:
        names.add(db_rel.parts[0])
        if len(db_rel.parts) > 1:
            names.add(db_rel.parts[0])  # 子目录内的 db，同时保护其顶层目录
    return names


def _delete_within_root(p: Path) -> bool:
    """删除程序根内的文件/目录，校验路径范围（拒绝越界/符号链接逃逸/根目录本身）。"""
    root = config.program_root().resolve()
    try:
        resolved = p.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        print(f"[upgrade] 跳过越界路径，拒绝删除 {p}", file=sys.stderr)
        return False
    if resolved == root:
        return False
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    return True


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def venv_python() -> Path:
    if sys.platform == "win32":
        return config.program_root() / ".venv" / "Scripts" / "python.exe"
    return config.program_root() / ".venv" / "bin" / "python"


def _venv_base_python() -> str:
    """旧 venv 的基础解释器（sys._base_executable），回滚时用同一解释器重建 venv 匹配旧解释器。

    venv 不存在或查询失败返回空串（回滚时回退到当前解释器 sys.executable）。
    """
    vp = venv_python()
    if not vp.exists():
        return ""
    code = "import sys; print(getattr(sys, '_base_executable', '') or sys.executable)"
    out = subprocess.run([str(vp), "-c", code], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def _db_file() -> Path:
    """数据库路径（尊重 GROUP_DB 覆盖）。"""
    return Path(config.db_path())


def _att_dir() -> Path:
    """附件目录（尊重 GROUP_ATTACHMENTS_DIR 覆盖）。"""
    return config.attachments_dir()


def _env_file() -> Path:
    return config.data_dir() / ".env"


def _collect_files(root: Path, exclude: set[str]) -> list[str]:
    """递归收集目录下所有文件（相对路径，用 / 分隔），跳过 exclude 命名的顶层项。"""
    files: list[str] = []
    if not root.is_dir():
        return files
    for item in sorted(root.iterdir()):
        if item.name in exclude:
            continue
        if item.is_dir():
            files.extend(f"{item.name}/{rel}" for rel in _collect_files(item, set()))
        else:
            files.append(item.name)
    return files


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_stop(require: bool = False) -> int:
    """优雅停止引擎（确认引擎/MCP 等写入方已停止）。

    统一走独立停机闸门（标准库直查 /health + 实例身份核验），不依赖 venv/stop_engine.py；
    require 参数保留向后兼容。缺 venv 不再直接放行，缺凭据一律返 1（node 1071 P1）。
    """
    return _stop_independent()


def _write_manifest(backup_dir: Path, program_files: dict[str, str], data_files: dict[str, str]) -> None:
    """写 manifest：文件清单（相对路径 -> sha256）+ 大小 + 实例绑定绝对路径。"""
    manifest = {
        "version": 2,
        "complete": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "instance": {
            "program_root": str(config.program_root()),
            "db_path": str(_db_file()),
            "attachments_dir": str(_att_dir()),
            "venv_base_python": _venv_base_python(),
        },
        "program_files": {rel: {"sha256": sha} for rel, sha in sorted(program_files.items())},
        "data_files": {rel: {"sha256": sha} for rel, sha in sorted(data_files.items())},
    }
    (backup_dir / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_manifest(backup_dir: Path) -> dict | None:
    path = backup_dir / MANIFEST
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 结构校验：必需键存在且类型正确，否则视为损坏
    if not isinstance(data, dict) or not data.get("complete"):
        return None
    if not isinstance(data.get("program_files"), dict) or not isinstance(data.get("data_files"), dict):
        return None
    return data


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _backup_db_file(src: Path, dst: Path) -> None:
    """用 sqlite3 backup API 备份数据库（node 1074 缺陷3：直接复制主文件遗漏 WAL 已提交数据）。

    backup API 走在线快照，主文件 + WAL 内容一并产出到 dst；非 SQLite 文件（或损坏）
    抛 sqlite3.Error → 上层 _backup 失败，不产半备快照（latest_backup 不会选它）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _verify_backup(backup_dir: Path) -> bool:
    """完整性验证：manifest 结构 + 每个清单项文件存在 + SHA256 匹配。损坏返回 False（拒绝回滚删原数据）。"""
    manifest = _read_manifest(backup_dir)
    if manifest is None:
        return False
    for group in ("data_files", "program_files"):
        subdir = "data" if group == "data_files" else "program"
        for rel, meta in manifest.get(group, {}).items():
            fpath = backup_dir / subdir / rel
            if not fpath.is_file():
                return False
            if isinstance(meta, dict) and meta.get("sha256"):
                if _sha256_file(fpath) != meta["sha256"]:
                    return False
    return True


def _backup(backup_dir: Path) -> None:
    """事务化备份：数据目录必要数据 + 程序源码到临时目录，写 manifest 后原子发布到 backup_dir。

    不完整备份（中途异常）只留 .tmp 临时目录，不带 manifest，latest_backup 不会选它，也不会触发回滚删原数据。
    """
    backups_root = config.backups_dir()
    backups_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = backups_root / f".tmp-{_ts()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    data_files: dict[str, str] = {}
    program_files: dict[str, str] = {}

    # 数据快照（走 config 统一路径：db_path / attachments_dir / data_dir/.env）
    data_dst = tmp_dir / "data"
    db_file = _db_file()
    if db_file.exists():
        _backup_db_file(db_file, data_dst / "group.db")
        data_files["group.db"] = _sha256_file(data_dst / "group.db")
    env_file = _env_file()
    if env_file.exists():
        _copy_file(env_file, data_dst / ".env")
        data_files[".env"] = _sha256_file(env_file)
    att = _att_dir()
    if att.is_dir():
        for rel in _collect_files(att, set()):
            _copy_file(att / rel, data_dst / "attachments" / rel)
            data_files[f"attachments/{rel}"] = _sha256_file(att / rel)

    # 程序快照（源码，排除 .venv/数据项/.git/自定义数据目录）
    prog_dst = tmp_dir / "program"
    for rel in _collect_files(config.program_root(), _protected_top_names()):
        _copy_file(config.program_root() / rel, prog_dst / rel)
        program_files[rel] = _sha256_file(config.program_root() / rel)

    # 完整性标记 + 原子发布（rename 同卷原子）
    _write_manifest(tmp_dir, program_files, data_files)
    os.rename(tmp_dir, backup_dir)


def _fetch_and_extract(url: str, sha256: str) -> Path:
    """下载新版本归档 → SHA256 校验 → 解压 → 定位源码根（复用 bootstrap 的安全下载校验链）。"""
    archive_type = "zip" if urllib.parse.urlsplit(url).path.lower().endswith(".zip") else "tar.gz"
    tmpdir = Path(tempfile.mkdtemp(prefix="bot-group-upgrade-"))
    try:
        archive = tmpdir / ("src." + archive_type)
        _bs._download(url, archive)
        _bs._verify(sha256, archive)
        extracted = tmpdir / "extracted"
        _bs._extract(archive, extracted, archive_type)
        src_root = _bs._find_source_root(extracted)
        if src_root is None:
            raise RuntimeError("[upgrade] 归档内未找到 install.sh / install.py，请确认归档为完整项目包")
        persistent = Path(tempfile.mkdtemp(prefix="bot-group-upgrade-src-"))
        for item in src_root.iterdir():
            dst = persistent / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
        return persistent
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _collect_release_manifest(src_root: Path) -> set[str] | None:
    """从新版本源码根读受管理发布清单（release_manifest.json，程序文件相对路径列表）。

    归档构建时应随包生成；清单缺文件/损坏返回 None（调用方保守处理：只落地不删）。
    node 1079 缺陷1：old_managed 必须来自受管理发布清单，不用程序根扫描。
    """
    path = src_root / RELEASE_MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("program_files"), list):
        return None
    return {str(rel) for rel in data["program_files"] if str(rel).strip()}


def _land_release_manifest(src_root: Path) -> set[str] | None:
    """落地新版本的受管理发布清单到程序根（升级后的管理基准），返回其程序文件集合。"""
    files = _collect_release_manifest(src_root)
    if files is None:
        return None
    shutil.copy2(src_root / RELEASE_MANIFEST,
                 config.program_root() / RELEASE_MANIFEST)
    return files


def _current_release_manifest() -> set[str] | None:
    """读当前程序根持久化的受管理发布清单（上次升级落地/发布包随附）。缺失返回 None。"""
    return _collect_release_manifest(config.program_root())


def _write_release_manifest(files: set[str], version: str | None = None) -> None:
    """写程序根受管理发布清单（发布包未随附时由落地文件生成，作为下次升级的基准）。"""
    payload = {"program_files": sorted(files)}
    if version:
        payload["version"] = version
    (config.program_root() / RELEASE_MANIFEST).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


VERSION_FILE = "VERSION"


def current_version() -> str | None:
    """当前安装版本（程序根 VERSION 文件，make_release 随包产出/升级落地）。缺失返 None。"""
    path = config.program_root() / VERSION_FILE
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _fetch_release_index(index_url: str | None = None) -> list[dict]:
    """拉取 Release 索引（RELEASE_INDEX_URL，JSON {"releases":[...]}）。

    node 215：查询失败明确抛错（上层返回 1 保留当前版本），绝不静默降级。
    """
    url = config._pick(index_url, ("RELEASE_INDEX_URL",), "")
    return releases.fetch_index(url)


def _pick_formal_release(entries: list[dict]) -> dict:
    """选最新正式 Release：跳过 prerelease=true 的预发布项；对版本排序取最大。

    版本比较：v 前缀剥掉后按数值段逐段比（v1.10.0 > v1.9.0），无法解析的项跳过。
    """
    return releases.pick_formal(entries)


def discover_release(index_url: str | None = None) -> releases.Release:
    """自动发现最新正式 Release，返回 {"version","source","sha256"}。

    node 215 验收第 5 条：缺必需资产（source/sha256）明确失败，由上层保留当前版本。
    """
    return releases.resolve(_pick_formal_release(_fetch_release_index(index_url)))


def _resolve_upgrade_target(url: str | None, sha256: str | None,
                            index_url: str | None = None) -> releases.Release | None:
    """解析升级目标。node 215：默认自动发现最新正式 Release；显式 --url/--sha256 优先。

    返回 (url, sha256)；无更新返 None（明确提示后由调用方退出 0）。
    """
    if url and sha256:
        return releases.Release(None, url, sha256)
    if url or sha256:
        raise RuntimeError("--url 与 --sha256 必须成对提供；或都不提供走默认自动发现")
    target = discover_release(index_url)
    cur = current_version()
    print(f"[upgrade] 当前版本：{cur or '<未知>'}")
    print(f"[upgrade] 最新正式 Release：{target.version}")
    current_key = releases.version_key(cur or "")
    if current_key and current_key >= releases.version_key(target.version):
        print(f"[upgrade] 当前版本 {cur} 已是最新或更高版本，无需更新")
        return None
    return target


def _validate_release(new_src: Path, target: releases.Release) -> None:
    """停机前核对固定目标的外置清单、包内清单、VERSION 和完整文件集合。"""
    if target.version is None:  # 兼容显式归档入口
        return
    manifest = releases.checked_manifest(target)
    embedded = json.loads((new_src / RELEASE_MANIFEST).read_text(encoding="utf-8"))
    if embedded != manifest:
        raise RuntimeError("包内清单与固定 Release 的外置清单不一致")
    paths = manifest.get("program_files")
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
        raise RuntimeError("Release 缺少有效的文件级清单")
    for rel in paths:
        if (not rel or "\\" in rel or rel.startswith("/") or ":" in rel
                or any(p in ("", ".", "..") for p in rel.split("/"))):
            raise RuntimeError(f"Release 清单路径无效：{rel}")
    actual = {p.relative_to(new_src).as_posix() for p in new_src.rglob("*") if p.is_file()}
    if set(paths) | {RELEASE_MANIFEST} != actual or VERSION_FILE not in paths:
        raise RuntimeError("Release 文件集合与清单不一致或缺少 VERSION")
    if (new_src / VERSION_FILE).read_text(encoding="utf-8").strip() != target.version:
        raise RuntimeError("源码 VERSION 与固定 Release 版本不一致")


def _iter_package_files(src_root: Path) -> list[tuple[Path, str]]:
    """枚举发布包内所有文件（不含受保护顶层项），返回 (源路径, 相对路径) 列表。"""
    items: list[tuple[Path, str]] = []
    for item in src_root.iterdir():
        if item.name in _protected_top_names() or item.name == RELEASE_MANIFEST:
            continue
        if item.is_dir():
            items.extend((p, f"{item.name}/{rel}")
                         for rel in _collect_files(item, set())
                         for p in [item / rel])
        else:
            items.append((item, item.name))
    return items


def _land_program(src_root: Path, old_managed: set[str] | None = None, *,
                  backup_dir: Path | None = None) -> set[str]:
    """落地新源码到程序根，跳过数据项（group.db/attachments/.env/backups/logs/.venv/install.json/.git 保留）。

    node 1084 缺陷2：文件级替换——逐文件落地（copy2 单文件覆盖），目录只 mkdir 合并、
    绝不整目录删除/重建，受管目录内的清单外文件（engine/user_notes.txt 等）原样保留。
    覆盖删除只认受管理发布清单（old_managed，文件级相对路径）：dst 已存在且不在旧清单
    → 用户文件一律不删不覆盖，跳过落地并告警（node 1074 缺陷1）。返回实际落地的文件
    相对路径集合（供回滚清理用）。old_managed=None 走旧语义（直接覆盖），仅保留给
    非升级路径兼容，生产路径始终传清单与 backup_dir。每次复制前持久化待写路径，
    回滚可清理复制中途产生的新增文件及半写文件。
    """
    program_root = config.program_root()
    landed: set[str] = set()
    for src_file, rel in _iter_package_files(src_root):
        dst = program_root / rel
        if dst.exists():
            if old_managed is not None and rel not in old_managed:
                print(f"[upgrade] 程序根已存在非清单文件 {rel}，跳过落地不覆盖", file=sys.stderr)
                continue
        if backup_dir is not None:
            _write_new_manifest(backup_dir, landed | {rel})
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst)
        landed.add(rel)
    return landed


def _write_new_manifest(backup_dir: Path, landed: set[str]) -> None:
    """原子保存已写及即将写入的路径；保存失败时保留上一份回滚清单。"""
    path = backup_dir / "new_manifest.json"
    temporary = backup_dir / "new_manifest.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump({"program_files": sorted(landed)}, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _new_program_files(src_root: Path) -> set[str]:
    """新版本源码的顶层文件/目录名集合（用于正向删除语义：删除新版已移除的顶层模块）。"""
    names: set[str] = set()
    for item in src_root.iterdir():
        if item.name in _protected_top_names():
            continue
        names.add(item.name)
        if item.is_dir():
            for rel in _collect_files(item, set()):
                names.add(f"{item.name}/{rel}")
    return names


def _sync_deps() -> int:
    """同步依赖：复用 install.py install（失效旧状态 + 重建/更新 venv + 装新 requirements.lock + 自检）。"""
    install_py = config.program_root() / "scripts" / "install.py"
    if not install_py.exists():
        print("[upgrade] 未找到 scripts/install.py，落地新源码不完整", file=sys.stderr)
        return 1
    return subprocess.run([sys.executable, str(install_py), "install"]).returncode


def _verify_migration(backup_db: Path) -> int:
    """迁移验证：复制备份 db 到独立工作副本，用新 venv 在副本上试跑 engine.db.init_schema。

    不改写唯一回滚快照；连接设 row_factory=sqlite3.Row（init_schema 按 r['name'] 读）。
    """
    vp = venv_python()
    if not vp.exists():
        return 1
    if not backup_db.exists():
        return 0
    work = Path(tempfile.mkdtemp(prefix="bot-group-migrate-"))
    work_db = work / "group.db"
    shutil.copy2(backup_db, work_db)
    code = (
        "import sqlite3, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "from engine import db; "
        "conn = sqlite3.connect(sys.argv[2]); "
        "conn.row_factory = sqlite3.Row; "
        "db.init_schema(conn); conn.close(); "
        "print('migration OK')"
    )
    rc = subprocess.run(
        [str(vp), "-c", code, str(config.program_root()), str(work_db)]
    ).returncode
    shutil.rmtree(work, ignore_errors=True)
    return rc


def _verify_ready() -> int:
    """就绪验证：install.py check（venv/状态/Python 版本/依赖摘要/锁定清单摘要）。"""
    install_py = config.program_root() / "scripts" / "install.py"
    return subprocess.run([sys.executable, str(install_py), "check"]).returncode


def _check_instance_binding(manifest: dict) -> bool:
    """快照绑定路径/实例身份：核对 manifest 记录的绝对路径与当前目标一致，配置变化拒绝（防跨库覆盖）。"""
    inst = manifest.get("instance")
    if not isinstance(inst, dict):
        return False
    return (
        inst.get("program_root") == str(config.program_root())
        and inst.get("db_path") == str(_db_file())
        and inst.get("attachments_dir") == str(_att_dir())
    )


def _remove_new_version_files(backup_dir: Path) -> None:
    """回滚前删除「升级引入且旧清单没有」的程序文件（node 1074 缺陷1：删除只认受管理发布清单）。

    只删升级落地清单（new_manifest.json）里有、而旧备份 manifest 里没有的文件；
    程序根下任何非清单文件（用户文件/笔误产物）一律不删。
    """
    new_manifest_path = backup_dir / "new_manifest.json"
    if not new_manifest_path.exists():
        return  # 未记录落地清单（旧格式备份）——保守起见不删任何文件
    try:
        landed = json.loads(new_manifest_path.read_text(encoding="utf-8")).get("program_files", [])
    except (OSError, ValueError):
        return
    program_root = config.program_root()
    old_manifest = _read_manifest(backup_dir)
    old_set = set(old_manifest.get("program_files", {}).keys()) if old_manifest else set()
    protected = _protected_top_names()
    for rel in landed:
        top = rel.split("/", 1)[0]
        if top in protected or rel in old_set:
            continue
        _delete_within_root(program_root / rel)


def _rollback(backup_dir: Path) -> int:
    """回滚：验证快照完整性及绑定 → 独立停机 → 恢复数据、程序及旧依赖。"""
    manifest = _read_manifest(backup_dir)
    if manifest is None:
        print("[upgrade] 备份目录无完整快照（缺 manifest/结构损坏），无法回滚", file=sys.stderr)
        return 1
    # 完整性验证：损坏备份拒绝不删原数据
    if not _verify_backup(backup_dir):
        print("[upgrade] 备份完整性校验失败（缺文件/摘要不符），拒绝回滚不删原数据", file=sys.stderr)
        return 1
    # 快照绑定路径/实例身份：配置变化拒绝（防跨库覆盖）
    if not _check_instance_binding(manifest):
        print("[upgrade] 快照绑定路径与当前实例不一致，拒绝回滚（防跨库覆盖）", file=sys.stderr)
        return 1

    # Validate the snapshot before any effect on a running engine.
    if _stop_independent() != 0:
        print("[upgrade] 停机失败，回滚中止（不忽略停机失败放行）", file=sys.stderr)
        return 1

    # 恢复数据快照（走 config 统一路径；db 先清 WAL/-shm 边车再整文件覆盖，防陈旧 WAL 回放进恢复库）
    data_src = backup_dir / "data"
    for name in ("group.db", ".env"):
        s = data_src / name
        if s.exists():
            dst = _db_file() if name == "group.db" else _env_file()
            if name == "group.db":
                for suffix in ("-wal", "-shm", "-journal"):
                    try:
                        Path(str(dst) + suffix).unlink()
                    except FileNotFoundError:
                        pass
            _copy_file(s, dst)
    att_src = data_src / "attachments"
    if att_src.is_dir():
        dst = _att_dir()
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(att_src, dst)

    # 恢复程序源码：先删升级新增残留（只删落地清单内文件），再从备份恢复
    _remove_new_version_files(backup_dir)
    prog_src = backup_dir / "program"
    if prog_src.is_dir():
        for rel in manifest.get("program_files", {}):
            s = prog_src / rel
            if s.exists():
                dst = config.program_root() / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, dst)

    # 依赖恢复可验证：重建旧解释器/依赖集合（不原样复用 venv），失败隔离证据 + 传播返回码
    install_py = config.program_root() / "scripts" / "install.py"
    if install_py.exists():
        args = [sys.executable, str(install_py), "install", "--rebuild"]
        base = manifest.get("instance", {}).get("venv_base_python")
        if base:
            args += ["--python", base]
        rc = subprocess.run(args).returncode
        if rc != 0:
            _mark_rollback_evidence(backup_dir, f"依赖重建失败 rc={rc}")
            print("[upgrade] 依赖重建失败，回滚未完成", file=sys.stderr)
            return rc
        if _verify_ready() != 0:
            _mark_rollback_evidence(backup_dir, "依赖重建后就绪校验失败")
            print("[upgrade] 依赖重建后就绪校验失败，回滚未完成", file=sys.stderr)
            return 1
    _mark_rollback_evidence(backup_dir, "回滚完成")
    print("[upgrade] 已回滚到备份快照")
    return 0


def _mark_rollback_evidence(backup_dir: Path, status: str) -> None:
    """记录回滚证据（失败隔离）。"""
    evidence = backup_dir / "rollback_evidence.json"
    try:
        data = {}
        if evidence.exists():
            data = json.loads(evidence.read_text(encoding="utf-8"))
        data[time.strftime("%Y-%m-%dT%H:%M:%S")] = status
        evidence.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _admin_headers() -> dict:
    """Load the installation environment and sign a local maintenance request."""
    from scripts.service import _load_env
    from engine import auth
    _load_env()
    if not auth.configured():
        return {}
    return {"Authorization": "Bearer " + auth.login(auth._password())["token"]}


def _engine_health() -> dict | None:
    """查询引擎 /health 返回健康信息；仅明确拒绝连接（无监听）返回 None，其余异常一律拒绝。

    node 1079 缺陷2：URLError 的 ConnectionReset/PermissionDenied 等非超时 reason 原先
    也被当「无引擎」返 None 放行。修复：只有 ConnectionRefusedError（明确无监听）才返
    None；超时/连接重置/权限等其他一切异常抛 RuntimeError 由上层返 1（fail-closed）。
    """
    url = f"http://{config.host()}:{config.port()}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(url + "/health", headers=_admin_headers())
        with opener.open(request, timeout=5) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"端口 {config.port()} 上的服务无法确认是引擎（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            return None  # 明确无监听
        detail = f"（reason: {reason}）" if reason else ""
        raise RuntimeError(f"端口 {config.port()} 健康检查异常，无法确认引擎已停止{detail}") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"端口 {config.port()} 健康检查超时，无法确认引擎已停止") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"端口 {config.port()} 健康检查超时，无法确认引擎已停止") from exc
    except OSError as exc:
        # Windows 拒绝连接常以 WinError 10061/ConnectionAborteded 形态穿透 OSError
        if getattr(exc, "errno", None) in (errno.ECONNREFUSED,) or isinstance(exc, ConnectionRefusedError):
            return None
        raise RuntimeError(f"端口 {config.port()} 健康检查异常，无法确认引擎已停止（{exc}）") from exc


def _port_closed(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return False
    except OSError:
        return True


def _stop_independent() -> int:
    """独立停机闸门：不依赖 venv/新版 stop_engine.py，用标准库直查引擎 /health，实例身份核验后优雅退出。

    - 端口拒绝连接（无引擎监听）→ 0（已停止，无写入方）。
    - /health 无 graceful_shutdown/instance_id（旧引擎或端口被其他服务占用）→ 1（无法确认，不误杀）。
    - 缺凭据（读不到 pid/instance_id）→ 1。
    - 优雅退出失败/实例变化/超时 → 1（绝不忽略停机失败放行）。
    """
    try:
        health = _engine_health()
    except RuntimeError as exc:
        print(f"[upgrade] {exc}，拒绝放行", file=sys.stderr)
        return 1
    if health is None:
        return 0  # 无引擎监听，已停止
    if health.get("graceful_shutdown") != 1 or not health.get("instance_id"):
        print("[upgrade] 运行的是旧版引擎（无实例身份），无法确认停机，请手动停止后重试", file=sys.stderr)
        return 1
    pid = health.get("pid")
    instance = health.get("instance_id")
    if not isinstance(pid, int) or pid <= 0:
        print("[upgrade] 引擎进程标识无效，无法确认停机", file=sys.stderr)
        return 1
    for key, expected in (("program_root", config.program_root()), ("db_path", _db_file())):
        actual = health.get(key)
        if not isinstance(actual, str) or not actual or Path(actual).resolve() != Path(expected).resolve():
            print(f"[upgrade] 引擎 {key} 与本次升级目标不匹配或缺失，拒绝停机；请核对目标并手动停止旧版引擎", file=sys.stderr)
            return 1
    host, port = config.host(), config.port()
    url = f"http://{host}:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url + "/admin/shutdown", headers=_admin_headers(), method="POST")
        with opener.open(req, timeout=5) as resp:
            result = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        print("[upgrade] 引擎优雅退出请求失败，拒绝放行", file=sys.stderr)
        return 1
    if result.get("instance_id") != instance:
        print("[upgrade] 引擎实例已变化，拒绝继续", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if not _pid_alive(pid) and _port_closed(host, port):
            print("[upgrade] 引擎已优雅退出（进程与端口已释放）")
            return 0
        time.sleep(0.3)
    print("[upgrade] 引擎退出超时，拒绝放行", file=sys.stderr)
    return 1


def _pid_alive(pid: int) -> bool:
    # 复用原生进程句柄检查。中文 tasklist 输出与 PYTHONUTF8=1 的编码冲突会让
    # subprocess 文本读取线程失败；句柄检查同时避免依赖本地化文本格式。
    from engine.browser_resources import _pid_alive as check_alive
    return check_alive(pid)


def latest_backup() -> Path | None:
    """最近一次完整升级备份（只选带 manifest 完整快照的目录）。"""
    backups = config.backups_dir()
    if not backups.is_dir():
        return None
    dirs = sorted((d for d in backups.iterdir() if d.is_dir() and d.name.startswith("upgrade-")),
                  reverse=True)
    for d in dirs:
        if _read_manifest(d) is not None and _verify_backup(d):
            return d
    return None


def do_upgrade(url: str | None = None, sha256: str | None = None,
               index_url: str | None = None) -> int:
    backup_dir = config.backups_dir() / f"upgrade-{_ts()}"
    new_src: Path | None = None
    mutation_started = False
    try:
        target = _resolve_upgrade_target(url, sha256, index_url)
        if target is None:
            return 0
        print("[upgrade] 获取固定目标并校验源码、清单与版本")
        new_src = _fetch_and_extract(target.source, target.sha256)
        _validate_release(new_src, target)
        old_managed = _current_release_manifest()
        if old_managed is None:
            raise RuntimeError("当前环境缺受管理发布清单（release_manifest.json），"
                               "请走可核验迁移流程建立旧版本文件归属后重试")
        print("[upgrade] 安全停机（停止引擎/MCP 等写入方）")
        if _safe_stop() != 0:
            print("[upgrade] 旧引擎停止失败，中止升级", file=sys.stderr)
            return 1
        print(f"[upgrade] 备份数据与程序快照到 {backup_dir}")
        _backup(backup_dir)
        from dataclasses import asdict
        (backup_dir / "upgrade_target.json").write_text(
            json.dumps({"current_version": current_version(), "target": asdict(target)},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        # node 1079 缺陷1 + node 1084 缺陷1：old_managed 用受管理发布清单（当前持久化的
        # release_manifest 或新包随附的），绝不用程序根扫描（备份扫描会把用户笔记混进
        # manifest）。node 1089 缺陷1 + 清单语义定稿§5：缺旧归属依据（程序根无持久化
        # 清单）→ fail-closed 中止升级，绝不把新包清单当旧归属（新包清单只声明新包文件，
        # 与旧环境文件归属无关，拿它当 old_managed 会把旧用户文件划成「非受管→跳过」、
        # 把新包同名文件划成「受管→覆盖」，两向都错）。不覆盖、不删除任何文件，
        # 不产出混合版本，不标记升级成功。
        old_managed = _current_release_manifest()
        if old_managed is None:
            print(
                "[upgrade] 当前环境缺受管理发布清单（release_manifest.json），无法安全升级。"
                "请走可核验迁移流程建立清单（安装 release_manifest 或人工核对程序文件归属"
                "后写入程序根 release_manifest.json），再重试升级。",
                file=sys.stderr,
            )
            raise RuntimeError("缺旧受管理清单，升级中止（现场保留）")
        old_managed = {r for r in old_managed if str(r).strip()}
        print("[upgrade] 落地新源码（保留数据目录）")
        mutation_started = True
        landed = _land_program(new_src, old_managed, backup_dir=backup_dir)
        _write_new_manifest(backup_dir, landed)
        # 正向删除语义：基于旧/新发布清单，删除新版已移除的顶层模块
        _remove_forward(new_src, backup_dir, old_managed)
        # node 1089 缺陷2：持久化清单绝不完整复制新包随附清单——随附清单可能含本次
        # 落地被跳过的路径（同名用户文件），完整复制会把用户文件划成受管，下次升级即
        # 被覆盖。持久化清单只记「仍存在的旧受管文件 ∪ 本次实际落地 landed」：新包移除
        # 的文件刚被 _remove_forward 删掉（不存在即不收录）；落地被跳过的同名用户文件
        # 从未入旧清单（用户文件），被跳过的新包文件不入 landed，两者天然被排除。
        still_managed = {r for r in old_managed
                         if (config.program_root() / r).is_file()}
        _write_release_manifest(still_managed | landed, target.version)
        if target.version is not None and current_version() != target.version:
            raise RuntimeError("VERSION 属于清单外用户文件，保留原文件并恢复旧版本")
        print("[upgrade] 同步依赖（复用 install）")
        if _sync_deps() != 0:
            raise RuntimeError("依赖同步失败")
        print("[upgrade] 迁移验证（独立工作副本试跑 init_schema）")
        if _verify_migration(backup_dir / "data" / "group.db") != 0:
            raise RuntimeError("数据库迁移验证失败")
        print("[upgrade] 就绪验证（install.py check）")
        if _verify_ready() != 0:
            raise RuntimeError("就绪验证失败")
        print("[upgrade] 升级成功，请运行 start 启动新引擎恢复任务派发")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"[upgrade] 升级失败：{exc}", file=sys.stderr)
        if mutation_started:
            print("[upgrade] 回滚到升级前快照")
            _rollback(backup_dir)
        else:
            print("[upgrade] 程序尚未变更，保留现场并退出")
        return 1
    finally:
        if new_src is not None:
            shutil.rmtree(new_src, ignore_errors=True)


def _remove_forward(new_src: Path, backup_dir: Path, old_managed: set[str] | None = None) -> None:
    """正向升级删除语义：基于旧/新受管理发布清单，删除新版已移除的受管文件。

    node 1079 缺陷1 + node 1084 缺陷2：old/new 清单均为文件级相对路径。只删
    「旧清单声明受管、新包（或新版清单）不再包含」的文件，逐文件删除，绝不整目录
    删除（受管目录内的清单外用户文件保留）。old_managed 传 None 时保守不删
    （fail-closed）。
    """
    if not old_managed:
        return  # 无受管理清单依据 → 不删任何东西（保守）
    new_files = {rel for _, rel in _iter_package_files(new_src)}
    program_root = config.program_root()
    for rel in old_managed:
        if rel in new_files:
            continue
        target = program_root / rel
        if target.is_dir():
            # 清单声明为文件但现场是目录：边界异常，拒绝整目录删除（保守跳过并告警）
            print(f"[upgrade] 受管清单项 {rel} 现为目录，跳过删除待人工核对", file=sys.stderr)
            continue
        if target.is_file():
            _delete_within_root(target)


def _top_level(program_files: dict) -> set[str]:
    """从 manifest program_files 的相对路径提取顶层名。"""
    tops: set[str] = set()
    for rel in program_files:
        tops.add(rel.split("/", 1)[0])
    return tops


def do_rollback() -> int:
    backup = latest_backup()
    if backup is None:
        print("[upgrade] 未找到完整升级备份，无法回滚", file=sys.stderr)
        return 1
    return _rollback(backup)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    up = sub.add_parser("upgrade", help="完整升级（默认自动发现最新正式 Release，无需填 commit）")
    up.add_argument("--url", default=os.environ.get("UPGRADE_URL", "").strip(),
                    help="显式指定源码归档地址（与 --sha256 成对；缺省走自动发现）")
    up.add_argument("--sha256", default=os.environ.get("UPGRADE_SHA256", "").strip(),
                    help="显式指定归档 SHA256（与 --url 成对）")
    up.add_argument("--index-url", default=None,
                    help="发布索引 URL（优先于环境与数据目录 .env 的 RELEASE_INDEX_URL）")
    sub.add_parser("rollback", help="回滚到最近一次升级备份")
    args = parser.parse_args(argv)

    if args.command == "upgrade":
        return do_upgrade(args.url or None, args.sha256 or None, args.index_url)
    if args.command == "rollback":
        return do_rollback()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
