"""Read-only source backup; migrate only the isolated copy, emit digest evidence."""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import db
from scripts.test_project_readiness import snapshot


def digest(value):
    return hashlib.sha256(repr(value).encode('utf-8')).hexdigest()


def project_files(conn):
    files = {}
    for row in conn.execute('SELECT name,root FROM projects'):
        root = Path(row['root'])
        for name in ('AGENTS.md', 'README.md', 'doc', 'docs'):
            path = root / name
            paths = sorted(path.rglob('*')) if path.is_dir() else [path]
            for file in paths:
                if file.is_file():
                    files[(row['name'], str(file.relative_to(root)))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    source_path = args.source.resolve(strict=True)
    # URI ro opens the source without schema initialization or application writes.
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    try:
        with tempfile.TemporaryDirectory(prefix='project-readiness-upgrade-') as directory:
            copy = sqlite3.connect(str(Path(directory) / 'copy.db'))
            copy.row_factory = sqlite3.Row
            try:
                source.backup(copy)
                before = snapshot(copy)
                before_files = project_files(copy)
                before_fk = list(copy.execute('PRAGMA foreign_key_check'))
                had_column = any(r['name'] == 'readiness_status' for r in copy.execute('PRAGMA table_info(projects)'))
                for _ in range(2):
                    db.init_schema(copy)
                    after = snapshot(copy)
                    changed = [key for key in before if before[key] != after.get(key)]
                    assert not changed, f'Application data changed: {changed}'
                    assert project_files(copy) == before_files, 'Project rule/document files changed'
                    assert [tuple(r) for r in copy.execute('PRAGMA foreign_key_check')] == [tuple(r) for r in before_fk]
                    assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                states = [dict(row) for row in copy.execute('SELECT name,readiness_status FROM projects ORDER BY name')]
                if not had_column:
                    assert all(row['readiness_status'] == 'ready' for row in states)
                result = dict(status='passed', source_read_only=True, upgraded_copy_only=True,
                    source_had_readiness=had_column, migration_runs=2, integrity='ok',
                    unchanged_tables={table: dict(rows=len(rows), sha256=digest(rows)) for table, rows in before.items()},
                    file_scope='AGENTS.md, README.md, doc/**, docs/** for every registered project',
                    unchanged_files=len(before_files), project_files_digest=digest(before_files),
                    existing_foreign_key_findings=len(before_fk), projects=states)
                args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                print(f'PASS: {len(states)} projects, {len(before)} application tables, {len(before_files)} files unchanged, two migrations')
            finally:
                copy.close()
    finally:
        source.close()


if __name__ == '__main__':
    main()
