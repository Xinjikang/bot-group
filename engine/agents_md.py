"""项目 AGENTS.md 读写与并发修改保护。

只读写 `{projects.root}/AGENTS.md` 一份：不自动同步 worktree、不自动 git commit
（唤醒时的受管理 override 同步由 project_rules 模块负责）。

两个对外函数：
    read(project)                    → GET 视图（含 revision / worktree 提示）
    save(project, content, revision) → PUT 落盘（revision 冲突 409，原子替换）

文件读写的两条约束：

1. **上限按「落盘后的字节」判**，不是按提交内容的字节。CRLF 文件会把每个 LF
   展开成 2 字节，先判 content 再转换会写出超限文件。
2. **任何已存在的文件都必须能读出 revision**——包括超限的、不是 UTF-8 的。
   否则 GET 拿不到 revision → PUT 必然 409 → 文件永久锁死，用户无法用编辑器自救。
   读不出正文只降级 content（`content_error` 说明原因），不影响 revision。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from . import db

_MAX_BYTES = 2 * 1024 * 1024  # AGENTS.md 落盘字节上限（与 profile 编辑同量级）
_LOCK = threading.RLock()

_WORKTREE_NOTE = (
    "引擎在唤醒前将主目录这份同步到成员 worktree 的受管理 override。"
    "每次执行重新加载主目录规则与版本；正在运行的执行在下次唤醒时取得更新。"
)
_PLAIN_NOTE = "每次执行显式加载主目录规则与版本，适用于新旧 Bot 和恢复的会话；长文件要求本轮完整读取。"

_OVERSIZE_MSG = "文件超过 2 MB，正文未载入；保存会整份覆盖它"
_ENCODING_MSG = "文件不是有效 UTF-8，正文未载入；保存会整份覆盖它"


def _conn() -> sqlite3.Connection:
    c = db.get_connection()
    db.init_schema(c)
    return c


def _project_row(project: str) -> sqlite3.Row:
    """查项目行；未知项目 404，归档项目也允许编辑（归档不删数据）。"""
    name = (project or "").strip()
    if not name:
        raise HTTPException(400, "project is required")
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, f"项目 {name} 不存在")
    return row


def _worktree_state(row: sqlite3.Row) -> tuple[bool, str]:
    mode = row['directory_mode'] if 'directory_mode' in row.keys() else ''
    worktree = mode == 'worktree' if mode else bool((row["worktree_root"] or "").strip())
    return worktree, (_WORKTREE_NOTE if worktree else _PLAIN_NOTE)


def _root_dir(row: sqlite3.Row) -> Path:
    """projects.root 必须存在且是目录，否则 400（不静默落到别处）。"""
    raw = (row["root"] or "").strip()
    if not raw:
        raise HTTPException(400, "项目路径为空，无法定位 AGENTS.md")
    root = Path(raw)
    if root.is_symlink() or not root.is_dir():
        raise HTTPException(400, f"项目路径不存在或不是目录：{raw}")
    return root


def _target(root: Path) -> Path:
    """目标文件：符号链接或非普通文件一律 400（不写穿到别处）。"""
    path = root / "AGENTS.md"
    if path.is_symlink():
        raise HTTPException(400, "AGENTS.md 是符号链接，拒绝写入")
    if path.exists() and not path.is_file():
        raise HTTPException(400, "AGENTS.md 不是普通文件")
    return path


def _hash_file(path: Path) -> str:
    """流式算整份文件的 sha256（不把文件读进内存，多大都能算）。

    文件不存在 → ""（配合 exists=false；PUT 时当前文件仍不存在即空串匹配成功）。
    """
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise HTTPException(500, "读取 AGENTS.md 失败，请检查文件权限") from None
    return digest.hexdigest()


def _read_capped(path: Path) -> bytes | None:
    """读全文（仅当不超过上限）；超限/不存在返回 None——绝不因此拒服务。"""
    if not path.exists():
        return None
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        raise HTTPException(500, "读取 AGENTS.md 失败，请检查文件权限") from None


def _decode(content: bytes | None) -> tuple[str, str]:
    """解出正文；读不出（超限/编码错）时返回 ("", 原因)，不抛。"""
    if content is None:
        return "", ""
    try:
        return content.decode("utf-8-sig"), ""
    except UnicodeError:
        return "", _ENCODING_MSG


def read(project: str) -> dict:
    row = _project_row(project)
    root = _root_dir(row)
    path = _target(root)
    with _LOCK:
        revision = _hash_file(path)
        content = _read_capped(path)
    exists = bool(revision) or path.exists()
    if not exists:
        text, content_error, size = "", "", 0
    else:
        text, content_error = _decode(content)
        if content is None and not content_error:
            content_error = _OVERSIZE_MSG
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    worktree, note = _worktree_state(row)
    return {
        "project": row["name"],
        "path": str(path),
        "exists": exists,
        "content": text,
        "revision": revision,
        "worktree": worktree,
        "worktree_note": note,
        # 下面三个是读不出正文时的降级说明（正文正常时 content_error=""）
        "size": size,
        "content_error": content_error,
        "oversize": size > _MAX_BYTES,
    }


def _stage(folder: Path, content: bytes) -> Path:
    """同目录建临时文件（同盘才能 os.replace 原子），写满 + fsync。"""
    fd, name = tempfile.mkstemp(prefix=".group-agents-", suffix=".tmp", dir=folder)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _backup(root: Path, path: Path, original: bytes) -> str | None:
    """替换前把原文件备份到项目根已有的 {root}/backups/ 目录（不新造目录）。

    项目根目录本来就有 backups/（放 group.db 等运行时备份），AGENTS.md 的
    保存备份也归这里，按 agents-md-{时间戳}-{uuid}/ 子目录放，别另起炉灶。
    返回备份子目录相对 root 的路径；无原文件（新建）返回 None。
    """
    if original is None:
        return None
    parent = root / "backups"
    if parent.is_symlink() or parent.resolve() != parent:
        raise HTTPException(400, "备份目录路径无效，原文件保持完整")
    parent.mkdir(mode=0o700, exist_ok=True)
    name = "agents-md-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    backup = parent / name
    backup.mkdir(mode=0o700)
    try:
        (backup / path.name).write_bytes(original)
        (backup / "manifest.json").write_text(json.dumps({
            "file": path.name,
            "path": str(path),
            "sha256": hashlib.sha256(original).hexdigest(),
            "size": len(original),
        }, indent=2), encoding="utf-8")
        if (backup / path.name).read_bytes() != original:
            raise OSError("backup verification failed")
    except Exception:
        shutil.rmtree(backup, ignore_errors=True)
        raise
    return str(backup.relative_to(root))


def _encode(text: str, original: bytes | None) -> bytes:
    """保留原文件换行风格（CRLF 不被悄悄改成 LF）与 BOM。

    original=None 表示原文件不存在或读不出（超限/编码错）——此时按 LF / 无 BOM
    落盘：用户是在**整份覆盖**一个用不了的旧文件，规范成干净格式才对。
    """
    if original and b"\r\n" in original:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    bom = b"\xef\xbb\xbf" if original and original.startswith(b"\xef\xbb\xbf") else b""
    return bom + text.encode("utf-8")


def save(project: str, content, revision) -> dict:
    if not isinstance(content, str):
        raise HTTPException(400, "content 必须是字符串")
    if not isinstance(revision, str):
        raise HTTPException(400, "revision 必须是字符串")
    # 廉价早退：本身已超限（CRLF 转换只会更长，不会变短）
    if len(content.encode("utf-8")) > _MAX_BYTES:
        raise HTTPException(400, "AGENTS.md 不能超过 2 MB")

    row = _project_row(project)
    root = _root_dir(row)
    path = _target(root)

    with _LOCK:
        current = _hash_file(path)
        if revision != current:
            raise HTTPException(409, "文件已被改动，请重新打开编辑窗口后再保存")
        original = _read_capped(path)
        after = _encode(content, original)
        # 🔴 权威判定：按**落盘后的字节**判上限（CRLF 展开后的长度）。
        # 按转换前的 content 判断会漏掉换行展开导致的超限。
        if len(after) > _MAX_BYTES:
            raise HTTPException(400, "AGENTS.md 不能超过 2 MB（按换行转换后的实际字节计）")
        if after == original:
            # 字节没变：返回新 revision，但不白写一次盘（保留 mtime）
            return {"saved": True, "path": str(path), "revision": _hash_file(path), "changed": False}
        backup = _backup(root, path, original)
        staged = _stage(root, after)
        try:
            os.replace(staged, path)
        except OSError:
            staged.unlink(missing_ok=True)
            # 替换失败 = 保存没发生，原文件没变，刚建的备份也多余，一并清掉
            if backup:
                shutil.rmtree(root / backup, ignore_errors=True)
                # backups 若这次被我们建成空壳就一并移除；有既有备份（group.db 等）时 rmdir 失败，静默忽略
                try:
                    (root / "backups").rmdir()
                except OSError:
                    pass
            raise HTTPException(500, "保存 AGENTS.md 失败，原文件保持完整") from None
        finally:
            staged.unlink(missing_ok=True)
        written = _hash_file(path)
        if written != hashlib.sha256(after).hexdigest():
            raise HTTPException(500, "AGENTS.md 回读校验失败")
    return {"saved": True, "path": str(path), "revision": written, "changed": True, "backup": backup}
