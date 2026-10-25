"""下载源码归档 -> SHA256 校验 -> 解压 -> 落地持久安装目录 -> 执行 install。

只用标准库（urllib/hashlib/zipfile/tarfile/shutil/subprocess），Windows 无 Git Bash 也能跑。
供应链安全：下载到本地文件校验 SHA256 后执行（不裸管道执行）；发布页命令绑定具体 tag 版本 + 摘要。

环境变量（发布页注入）：
  BOOTSTRAP_URL      源码归档下载地址（zip 或 tar.gz，必填）
  BOOTSTRAP_SHA256   归档 SHA256 摘要（必填）
  INSTALL_DIR        持久安装目录（默认当前工作目录）

关键：临时目录只存下载/解压，install 装到持久目录（INSTALL_DIR），临时目录用完即删，
不删落地后的源码/venv/入口（修复 node 1046 缺陷1/2）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.parse
import zipfile
from pathlib import Path


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(expected: str, path: Path) -> None:
    actual = _sha256_file(path)
    if actual.lower() != expected.strip().lower():
        raise RuntimeError(f"[bootstrap] SHA256 校验失败：期望 {expected}，实际 {actual}（源码可能被篡改或下载损坏）")


def _extract(archive: Path, dest: Path, archive_type: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive_type == "zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:  # tar.gz
        with tarfile.open(archive) as t:
            t.extractall(dest)


def _find_source_root(extracted: Path) -> Path | None:
    """Locate a source root, including releases with the legacy scripts layout."""
    candidates = [extracted]
    for root, dirs, files in os.walk(extracted):
        if "install.sh" in files or "install.py" in files:
            candidates.append(Path(root))
        # 顶层目录直接命中就不继续深入
        if Path(root) != extracted:
            dirs[:] = []
    for c in candidates:
        if (c / "tools" / "install.py").exists() or (c / "scripts" / "install.py").exists() or (c / "install.sh").exists():
            return c
    return None


def _land(src_root: Path, install_dir: Path) -> None:
    """把源码落地到持久安装目录（install 后 venv/源码/入口都在这里，不随临时目录删除）。"""
    install_dir.mkdir(parents=True, exist_ok=True)
    # 首次安装/中断重跑只补齐相同源码；跨版本通过 upgrade 的备份恢复链执行。
    # 写入前先检查全部冲突，用户文件与存量数据保持原样。
    pairs = []
    protected = {"group.db", "attachments", "backups", ".env", ".venv", "install.json", ".git", "logs"}
    for src in src_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        if rel.parts[0] in protected:
            raise RuntimeError(f"安装包包含持久数据路径：{rel}")
        dst = install_dir / rel
        if dst.exists() and (not dst.is_file() or _sha256_file(src) != _sha256_file(dst)):
            raise RuntimeError(f"安装位置存在不同内容：{rel}；请从现有版本执行 upgrade")
        pairs.append((src, dst))
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)


def main(argv=None) -> int:
    url = os.environ.get("BOOTSTRAP_URL", "").strip()
    expected = os.environ.get("BOOTSTRAP_SHA256", "").strip()
    if not url or not expected:
        print("[bootstrap] 需要 BOOTSTRAP_URL（源码归档地址）与 BOOTSTRAP_SHA256（SHA256 摘要）", file=sys.stderr)
        return 2

    archive_type = ""
    url_path = urllib.parse.urlsplit(url).path.lower()
    if url_path.endswith(".zip"):
        archive_type = "zip"
    elif url_path.endswith(".tar.gz") or url_path.endswith(".tgz"):
        archive_type = "tar.gz"
    if not archive_type:
        print("[bootstrap] 无法推断归档类型，请用 .zip 或 .tar.gz 结尾的 URL", file=sys.stderr)
        return 2

    install_dir = Path(os.environ.get("INSTALL_DIR", "") or os.getcwd())
    tmpdir = Path(tempfile.mkdtemp(prefix="bot-group-bootstrap-"))
    try:
        archive = tmpdir / ("src." + archive_type)
        print(f"[bootstrap] 下载 {url}")
        _download(url, archive)
        print("[bootstrap] 校验 SHA256")
        _verify(expected, archive)
        print("[bootstrap] 解压")
        extracted = tmpdir / "extracted"
        _extract(archive, extracted, archive_type)
        src_root = _find_source_root(extracted)
        if src_root is None:
            raise RuntimeError("[bootstrap] 归档内未找到 install.sh / install.py，请确认归档为完整项目包")
        print(f"[bootstrap] 落地到持久安装目录 {install_dir}")
        _land(src_root, install_dir)
        print("[bootstrap] 执行 install")
        installer = install_dir / "tools" / "install.py"
        if not installer.is_file():
            installer = install_dir / "scripts" / "install.py"
        r = subprocess.run([sys.executable, str(installer), "install"])
        return r.returncode
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[bootstrap] 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        # 临时目录只存下载/解压，用完即删；落地后的 install_dir 不受影响
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
