"""Explicit repair of confirmed legacy Hermes roots. Dry run by default.

Apply only after restarting engine and gateways with the root-mutation fix.
Every apply makes a SQLite backup; conflicting bindings are archived in full.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3


def repair(conn, root, legacy_roots):
    conn.row_factory = sqlite3.Row
    existing_fk_errors = {tuple(r) for r in conn.execute('PRAGMA foreign_key_check')}
    roots = {str(Path(p).resolve()).casefold() for p in [root, *legacy_roots]}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    refs = [t for t in tables if 'bot_id' in [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]]
    groups = {}
    for row in conn.execute("SELECT * FROM bots WHERE host_type='hermes'"):
        if str(Path(row['config_root']).resolve()).casefold() in roots:
            groups.setdefault(row['profile'], []).append(dict(row))
    plan = []
    for profile, bots in groups.items():
        ids = {b['id'] for b in bots}
        owners = {r[0] for r in conn.execute('SELECT bot_id FROM members WHERE profile=?', (profile,))} & ids
        if len(owners) != 1:
            raise ValueError(f'{profile}: expected one existing roster identity')
        keep = owners.pop()
        for bot in bots:
            old = bot['id']
            if old == keep:
                continue
            # Only historical duplicates: independently enrolled Bots need review.
            for table in ('members', 'project_members'):
                if conn.execute(f'SELECT 1 FROM {table} WHERE bot_id=?', (old,)).fetchone():
                    raise ValueError(f'{old}: independently enrolled identity')
            if conn.execute('SELECT 1 FROM session_bindings WHERE bot_id IN (?,?) AND switching<>0', (old, keep)).fetchone():
                raise ValueError('session switch in progress')
            conn.execute('CREATE TABLE IF NOT EXISTS identity_repair_archive '
                         '(source_table TEXT, source_id INTEGER, canonical_id INTEGER, payload TEXT, repaired_at TEXT)')
            def archive(table, row):
                conn.execute('INSERT INTO identity_repair_archive VALUES(?,?,?,?,?)',
                             (table, old, keep, json.dumps(dict(row), ensure_ascii=False), datetime.now().isoformat()))
            archive('bots', bot)
            for binding in conn.execute('SELECT rowid AS repair_rowid,* FROM session_bindings WHERE bot_id=?', (old,)).fetchall():
                archive('session_bindings', binding)
                target = conn.execute('SELECT 1 FROM session_bindings WHERE project=? AND bot_id=? AND kind=? AND thread_id=?',
                                      (binding['project'], keep, binding['kind'], binding['thread_id'])).fetchone()
                if target:
                    conn.execute('DELETE FROM session_bindings WHERE rowid=?', (binding['repair_rowid'],))
            for table in refs:
                conn.execute(f'UPDATE "{table}" SET bot_id=? WHERE bot_id=?', (keep, old))
            conn.execute('DELETE FROM bots WHERE id=?', (old,))
            plan.append({'profile': profile, 'from': old, 'to': keep})
        conn.execute('UPDATE bots SET config_root=? WHERE id=?', (str(Path(root).resolve()), keep))
    if {tuple(r) for r in conn.execute('PRAGMA foreign_key_check')} - existing_fk_errors:
        raise ValueError('foreign key check failed')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--legacy-root', action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source = sqlite3.connect(f'file:{Path(args.database).resolve().as_posix()}?mode=ro', uri=True)
    preview = sqlite3.connect(':memory:')
    source.backup(preview)
    with preview:
        plan = repair(preview, args.root, args.legacy_root)
    print(json.dumps({'merge': plan}, indent=2))
    if not args.apply:
        return
    backup = str(Path(args.database).resolve()) + '.identity-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S')
    with sqlite3.connect(backup) as target:
        source.backup(target)
    source.close()
    conn = sqlite3.connect(args.database, timeout=30)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = repair(conn, args.root, args.legacy_root)
        if result != plan:
            raise ValueError('identity data changed after preview')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps({'backup': backup, 'merged': len(plan)}))


if __name__ == '__main__':
    main()
