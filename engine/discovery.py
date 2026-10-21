"""宿主 profile 自动发现（req#87 task#150，设计稿 2.4）。

扫描 Hermes 与 Codex 的配置根目录，产出「可发现 profile 候选」：
- Hermes：根目录下 profiles/<name>/（named profile 必须显式建目录，
  Hermes assert_named_profile_home_live 强制；根自身是 default）。
- Codex：CODEX_HOME（默认 ~/.codex）下的子目录（保守判定：目录存在即候选，
  无 Codex 根时该宿主返回空列表，不报错——冷启动六步的「扫描不到任何
  profile」由 API 层给文案）。

同名 profile 双宿主靠 host_type+config_root 区分（设计稿 2.5 第 2 点；
UNIQUE(host_type, config_root, profile) 在 schema 层兜底）。

纯文件系统只读扫描 + bots/members 表比对，不写库。
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from . import codex_sandbox_notice, db

# profile 名合法性（与 profile_config._directory 同口径）：小写字母数字开头，
# 允许 - 和 _，最长 64。
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _hermes_root() -> str:
    """Hermes 根目录（与 db._hermes_root 同口径：HERMES_HOME 优先，回退 native）。"""
    return db._hermes_root()


def _codex_root() -> Path:
    """Codex 配置根目录（CODEX_HOME 优先，默认 ~/.codex）。"""
    configured = (os.environ.get("CODEX_HOME") or "").strip()
    return Path(configured).resolve() if configured else Path.home() / ".codex"


def _scan_hermes() -> list[dict]:
    """扫描 Hermes 根：default（根自身）+ profiles/<name>。"""
    root = Path(_hermes_root())
    out: list[dict] = []
    if (root / "state.db").is_file() or (root / "config.yaml").is_file() or root.is_dir():
        out.append({"host_type": "hermes", "profile": "default", "config_root": str(root)})
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir() or not _PROFILE_RE.match(entry.name):
                continue
            # 命名 profile 必须是真实 home（有 state.db/config.yaml 之一），过滤
            # 备份/临时目录之类的空壳。
            if (entry / "state.db").is_file() or (entry / "config.yaml").is_file():
                out.append({"host_type": "hermes", "profile": entry.name,
                            "config_root": str(root)})
    return out


def _scan_codex() -> list[dict]:
    """Discover the default home, native named configs and independent homes."""
    root = _codex_root()
    if not root.is_dir():
        return []
    out: list[dict] = []
    if any((root / f).is_file() for f in ('config.toml', 'auth.json')):
        out.append({'host_type': 'codex', 'profile': 'default', 'config_root': str(root)})
    names = {p.name[:-len('.config.toml')] for p in root.glob('*.config.toml')}
    config = root / 'config.toml'
    if config.is_file():
        try:
            import tomllib
            names.update(tomllib.loads(config.read_text(encoding='utf-8')).get('profiles', {}))
        except (ImportError, ValueError, OSError):
            pass
    for name in sorted(names):
        if name != 'default' and _PROFILE_RE.fullmatch(name):
            out.append({'host_type': 'codex', 'profile': name, 'config_root': str(root)})
    homes = list(root.iterdir())
    profiles_dir = root / 'profiles'
    if profiles_dir.is_dir():
        homes.extend(profiles_dir.iterdir())
    for entry in sorted(homes):
        if entry == profiles_dir:
            continue
        if not entry.is_dir() or not _PROFILE_RE.match(entry.name):
            continue
        # 保守判定：目录里至少有一个 Codex 常见工件才视为候选（过滤缓存/日志）。
        if any((entry / f).is_file() for f in ("config.toml", "auth.json", "sessions.json")):
            out.append({"host_type": "codex", "profile": entry.name, "config_root": str(entry)})
    return out


def scan_candidates(conn: sqlite3.Connection, project: str = '') -> list[dict]:
    """产出可发现 profile 候选（只读，不写库）。

    每个候选：{host_type, profile, config_root, display_name, bot_id,
    bound, already_member}——
    - bound：bots 表已有 (host_type, config_root, profile) 行（复用 Bot）；
    - bot_id：绑定行 id（未绑定为 None）；内置 user 身份不参与发现（8.1）。
    - already_member：members 花名册已有该 profile（添加流程提示用）。
    """
    bots = {
        (r["host_type"], r["config_root"], r["profile"]): dict(r)
        for r in conn.execute("SELECT id, host_type, config_root, profile, display_name FROM bots")
    }
    members = {r['bot_id']: r['profile'] for r in conn.execute('SELECT profile,bot_id FROM members') if r['bot_id'] is not None}
    project_bots = {r['bot_id'] for r in conn.execute(
        'SELECT bot_id FROM project_members WHERE project=?', (project,))} if project else set()
    out: list[dict] = []
    for cand in _scan_hermes() + _scan_codex():
        if db.is_builtin_user(cand["profile"]):
            continue  # 内置 user 身份不是扫描资源（8.1）
        bot = bots.get((cand["host_type"], cand["config_root"], cand["profile"]))
        out.append({
            "host_type": cand["host_type"],
            "profile": cand["profile"],
            "config_root": cand["config_root"],
            "display_name": bot["display_name"] if bot else "",
            "bot_id": bot["id"] if bot else None,
            "bound": bot is not None,
            "member_profile": members.get(bot['id']) if bot else None,
            "already_member": bot is not None and bot['id'] in members,
            "already_project_member": bot is not None and bot['id'] in project_bots,
            **codex_sandbox_notice.for_candidate_notices(cand),
        })
    return out


def register_bot(conn: sqlite3.Connection, host_type: str, profile: str,
                 config_root: str, display_name: str = "") -> dict:
    """Bot 注册/复用（req#87 task#150：已绑定复用，未绑定注册新 Bot）。

    返回 {bot_id, created}。created=False 表示复用既有 Bot，保留统一显示名。
    项目角色与权限由项目成员配置单独管理。
    🔴 兼容异构 bots 表（真库带 created_at NOT NULL）：按列集分叉写入。
    """
    host_type = (host_type or "").strip()
    profile = (profile or "").strip()
    config_root = (config_root or "").strip()
    if host_type not in ("hermes", "codex"):
        raise ValueError("host_type must be 'hermes' or 'codex'")
    if not profile or not _PROFILE_RE.match(profile) or db.is_builtin_user(profile):
        raise ValueError(f"invalid profile: {profile!r}")
    if not config_root:
        raise ValueError("config_root is required")
    row = conn.execute(
        "SELECT id FROM bots WHERE host_type=? AND config_root=? AND profile=?",
        (host_type, config_root, profile),
    ).fetchone()
    if row is not None:
        return {"bot_id": row["id"], "created": False}
    bot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bots)")}
    created = ", created_at" if "created_at" in bot_cols else ""
    cur = conn.execute(
        "INSERT INTO bots (host_type, profile, config_root, runtime, display_name"
        f"{created}) VALUES (?, ?, ?, ?, ?{', ?' if created else ''})",
        (host_type, profile, config_root, host_type, display_name,
          db.now_iso()) if created
        else (host_type, profile, config_root, host_type, display_name),
    )
    return {"bot_id": cur.lastrowid, "created": True}
