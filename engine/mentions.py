"""@解析：从消息文本里精确匹配该项目启用的 profile 名。

规则：
- 只匹配 members 表里的 profile 名（精确匹配，不靠模糊），且该 profile 在消息
  所属项目下已显式启用（project_members.active=1）。
- 支持 @name 与 @name 后跟标点/空白/行尾的边界。
- 返回去重后的 profile 名列表（保持出现顺序）。
"""

from __future__ import annotations

import re
import sqlite3


def _known_profiles(conn: sqlite3.Connection, project: str = "") -> set[str]:
    """该项目下可被 @ 的 profile 名集合（members 全册 ∩ 项目内启用）。"""
    rows = conn.execute("SELECT profile FROM members").fetchall()
    all_profiles = {r["profile"] for r in rows}
    from .db import BUILTIN_USER_PROFILE
    all_profiles.add(BUILTIN_USER_PROFILE)
    if not all_profiles or not project:
        return all_profiles
    from .router import _profile_active
    return {p for p in all_profiles if _profile_active(conn, project, p)}


def parse_mentions(text: str, conn: sqlite3.Connection, project: str = "") -> list[str]:
    """从 text 中解析出被 @ 的 profile 名（精确匹配 members 表 + 项目启停）。"""
    if not text:
        return []
    known = _known_profiles(conn, project)
    if not known:
        return []

    # 按名字长度降序匹配，避免短名是长名前缀时误匹配
    names = sorted(known, key=len, reverse=True)
    # 构造一个正则：@ 后跟名字，名字后必须是边界（非字母数字下划线连字符）
    pattern = re.compile(
        r"@(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_\-])"
    )
    seen: list[str] = []
    for m in pattern.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen
