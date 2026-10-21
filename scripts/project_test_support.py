"""Explicit membership setup for isolated legacy workflow fixtures."""


def enable_members(conn, projects, profiles=None):
    if profiles is None:
        profiles = [r[0] for r in conn.execute("SELECT profile FROM members WHERE profile<>'user'")]
    for profile in profiles:
        conn.execute('INSERT OR IGNORE INTO members(profile,display_name) VALUES(?,?)', (profile, profile))
    for project in projects:
        conn.execute("INSERT OR IGNORE INTO projects(name,root,created_at) VALUES(?,'.','now')", (project,))
        conn.executemany('INSERT OR IGNORE INTO project_members(project,profile,active) VALUES(?,?,1)', [(project, profile) for profile in profiles])
