"""Resolve engine member handles to stable Bots and physical host profiles."""
from pathlib import Path
import re


def resolve(conn, project, member, *, active_only=False):
    rows = conn.execute(
        "SELECT b.* FROM project_members pm JOIN bots b ON b.id=pm.bot_id "
        "WHERE pm.project=? AND pm.profile=?" + (" AND pm.active=1" if active_only else ""),
        (project, member),
    ).fetchall()
    ids = {r['id'] for r in rows}
    if len(ids) > 1:
        raise ValueError('ambiguous member identity; select a Bot member handle')
    return dict(rows[0]) if rows else None


def global_bot(conn, member):
    row = conn.execute('SELECT b.* FROM members m JOIN bots b ON b.id=m.bot_id WHERE m.profile=?',
                       (member,)).fetchone()
    return dict(row) if row else None


def member_views(conn):
    """One host-neutral identity projection for roster and workspace responses."""
    result = []
    for row in conn.execute(
        'SELECT m.*,b.profile AS host_profile,b.runtime, '
        "COALESCE(NULLIF(b.display_name,''),NULLIF(m.display_name,''),b.profile,m.profile) AS bot_display_name "
        'FROM members m LEFT JOIN bots b ON b.id=m.bot_id ORDER BY m.profile'
    ):
        member = dict(row)
        member['display_name'] = member.pop('bot_display_name')
        member['host_profile'] = member['host_profile'] or member['profile']
        result.append(member)
    return result


def hermes_home(member, fallback):
    if not re.fullmatch(r'bot-\d+(?:-bot)*', member):
        return fallback if member == 'default' else fallback / 'profiles' / member
    from . import db
    conn = db.get_connection()
    try:
        bot = global_bot(conn, member)
    finally:
        conn.close()
    if not bot:
        return fallback if member == 'default' else fallback / 'profiles' / member
    if bot['runtime'] != 'hermes':
        raise ValueError('Hermes state is unavailable for this runtime')
    root = Path(bot['config_root'])
    return root if bot['profile'] == 'default' else root / 'profiles' / bot['profile']


def enroll(conn, bot_id, project):
    from fastapi import HTTPException
    from . import db, hermes_profile_names
    bot = conn.execute('SELECT * FROM bots WHERE id=?', (bot_id,)).fetchone()
    if bot and bot['host_type'] == 'hermes':
        try:
            hermes_profile_names.validate(bot['profile'])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    row = conn.execute('SELECT profile FROM members WHERE bot_id=?', (bot_id,)).fetchone()
    if row:
        member = row['profile']
        conn.execute('UPDATE members SET display_name=? WHERE bot_id=?',
                     (bot['display_name'] or bot['profile'], bot_id))
    else:
        # Existing Hermes handles retain their message/task history. New Codex
        # handles derive from the Bot ID, so physical profile names can overlap.
        legacy_root = Path(bot['config_root']).resolve() == Path(db._hermes_root()).resolve()
        member = bot['profile'] if bot['host_type'] == 'hermes' and legacy_root else f'bot-{bot_id}'
        existing = conn.execute('SELECT bot_id FROM members WHERE profile=?', (member,)).fetchone()
        if existing and existing['bot_id'] != bot_id:
            member = f'bot-{bot_id}'
        while conn.execute('SELECT 1 FROM members WHERE profile=? AND (bot_id IS NULL OR bot_id<>?)',
                           (member, bot_id)).fetchone():
            member += '-bot'
        conn.execute(
            "INSERT INTO members(profile,display_name,created_at,source,bot_id) "
            "VALUES(?,?,?, 'api',?) ON CONFLICT(profile) DO UPDATE SET "
            "display_name=excluded.display_name,bot_id=excluded.bot_id",
            (member, bot['display_name'] or bot['profile'],
             db.now_iso(), bot_id),
        )
    conn.execute(
        'INSERT INTO project_members(project,bot_id,profile,active) VALUES(?,?,?,1) '
        'ON CONFLICT(project,bot_id) DO UPDATE SET active=1', (project, bot_id, member),
    )
    return member


def codex_settings(bot):
    root = Path(bot['config_root']).resolve()
    profile = bot['profile']
    if (root / f'{profile}.config.toml').is_file():
        return str(root), profile
    if profile == 'default' or root.name == profile:
        return str(root), None
    independent = root / profile
    if independent.is_dir() and any((independent / f).is_file() for f in ('config.toml', 'auth.json')):
        return str(independent), None
    return str(root), profile if profile != 'default' else None
