"""列出引擎所在机器的目录树（req#110 task#188 方案A 后端）。

网页内目录选择器替代原生 FolderBrowserDialog：前端自绘目录树/面包屑，本模块
只提供「列一层目录」能力——返回当前目录路径 + 子目录列表（仅目录，不列文件），
供前端逐层导航。

安全边界（限授权目录、只返回选目录所需信息，不暴露超出需求内容）：
- 鉴权由 API 层负责（管理员身份；方案A 远程可复用故不限回环，远程选的是
  「服务器目录」，UI 标注由前端负责）；
- 只返回子目录名，不返回文件、文件内容、文件大小或隐藏信息；
- 无权限目录（scandir 抛 PermissionError）正常返回空列表 + error 标记，不 500；
- 大目录分页（offset/limit），单次 limit 上限防滥用。
"""
from __future__ import annotations

import os

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _root_entries() -> list[dict]:
    """根视图：Windows 返回盘符列表，非 Windows 返回 '/'。"""
    if os.name == 'nt':
        entries = []
        for drive in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            root = f'{drive}:\\'
            if os.path.isdir(root):
                entries.append({'name': root, 'path': root, 'navigable': True})
        return entries
    return [{'name': '/', 'path': '/', 'navigable': True}]


def _base(path: str, parent: str, error: str, limit: int) -> dict:
    return {
        'path': path, 'parent': parent, 'entries': [], 'total': 0,
        'offset': 0, 'limit': limit, 'has_more': False, 'error': error,
    }


def list_directory(path: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> dict:
    """列一层目录。

    返回 {path, parent, entries[{name, path, navigable}], total, offset, limit,
    has_more, error?}。error 为 None 表示正常；'not_found'/'not_directory'/
    'permission_denied' 时 entries 为空（不抛异常）。
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    if not path or not str(path).strip():
        entries = _root_entries()
        return {
            'path': '', 'parent': None, 'entries': entries, 'total': len(entries),
            'offset': 0, 'limit': limit, 'has_more': False,
        }

    target = path
    parent = os.path.dirname(os.path.normpath(target))
    if parent == os.path.normpath(target):
        parent = None  # 盘符根 / 文件系统根

    if not os.path.exists(target):
        return _base(target, parent, 'not_found', limit)
    if not os.path.isdir(target):
        return _base(target, parent, 'not_directory', limit)

    try:
        dirs = []
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        dirs.append(entry)
                except OSError:
                    continue  # 损坏条目 / 无权限子项：跳过，不暴露
    except PermissionError:
        return _base(target, parent, 'permission_denied', limit)

    dirs.sort(key=lambda e: e.name.casefold())
    total = len(dirs)
    page = dirs[offset:offset + limit]
    entries = [{'name': e.name, 'path': e.path, 'navigable': True} for e in page]
    return {
        'path': target, 'parent': parent, 'entries': entries, 'total': total,
        'offset': offset, 'limit': limit,
        'has_more': offset + len(entries) < total,
    }
