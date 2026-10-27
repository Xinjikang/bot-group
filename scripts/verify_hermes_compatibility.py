"""Exercise real Hermes session APIs in an isolated home and engine database."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def exercise(source, home, resume):
    os.environ.update(HERMES_HOME=str(home), HERMES_BIN='', GROUP_DATA_DIR=str(home),
                      GROUP_DB=str(home / 'group.db'), GROUP_ATTACHMENTS_DIR=str(home / 'attachments'))
    from engine import db, session_adapter as adapter, hermes_runtime
    capabilities = hermes_runtime.inspect_source(source)
    with patch.object(adapter, '_hermes_src', return_value=source):
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            if resume:
                persisted = json.loads((home / 'expected.json').read_text())
                actual = adapter.resolve_or_create(conn, 'compat', 'review', 'work', None, 'review-compat-work')
                assert actual == persisted['tip'], (actual, persisted)
                assert adapter.session_exists('review', persisted['original'])
                with closing(sqlite3.connect(adapter.profile_state_db('review'))) as state, state:
                    assert state.execute("SELECT content FROM messages WHERE session_id=?", (persisted['original'],)).fetchone()[0] == 'retained history'
                print('PASS restart/persistent binding/history', flush=True)
                return
            conn.execute("INSERT INTO projects(name,root,created_at,directory_mode) VALUES('compat',?,'now','shared')", (str(home),))
            conn.commit()
            original = adapter.resolve_or_create(conn, 'compat', 'review', 'work', None, 'review-compat-work')
            assert adapter.resolve_or_create(conn, 'compat', 'review', 'work', None, 'review-compat-work') == original
            assert adapter.resolve_session_by_title('review', 'review-compat-work') == original
            legacy = adapter.create_session('legacy', 'legacy-compat-work')
            assert adapter.resolve_or_create(conn, 'compat', 'legacy', 'work', None, 'legacy-compat-work') == legacy
            with closing(sqlite3.connect(adapter.profile_state_db('review'))) as state, state:
                state.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','retained history',1)", (original,))
            changed = adapter.switch_session(conn, 'compat', 'review', 'work', None, 'review-compat-work', busy_check=lambda *args: False)
            assert changed['ok'], changed
            current = changed['session_id']
            assert current != original and adapter.session_exists('review', original)
            generation = conn.execute("SELECT generation FROM session_bindings WHERE profile='review'").fetchone()[0]
            assert generation == 1
            tip = adapter.create_session('review', 'compression-tip')
            with closing(sqlite3.connect(adapter.profile_state_db('review'))) as state, state:
                state.execute("UPDATE sessions SET ended_at=2,end_reason='compression' WHERE id=?", (current,))
                state.execute('UPDATE sessions SET parent_session_id=? WHERE id=?', (current, tip))
            assert adapter.compression_tip('review', current) == tip
            assert adapter.resolve_or_create(conn, 'compat', 'review', 'work', None, 'review-compat-work') == tip
            assert conn.execute("SELECT generation FROM session_bindings WHERE profile='review'").fetchone()[0] == generation
            invalid = adapter.resolve_or_create(conn, 'compat', 'gone', 'work', None, 'gone-compat-work')
            with closing(sqlite3.connect(adapter.profile_state_db('gone'))) as state, state:
                state.execute('DELETE FROM sessions WHERE id=?', (invalid,))
            try:
                adapter.resolve_or_create(conn, 'compat', 'gone', 'work', None, 'gone-compat-work')
            except adapter.SessionBindingInvalid:
                pass
            else:
                raise AssertionError('invalid binding was replaced')
            (home / 'expected.json').write_text(json.dumps({'original': original, 'tip': tip}))
            print('PASS create/resume/title migration/new/generation/compression/invalid binding', json.dumps(capabilities), flush=True)
        finally:
            conn.close()
            registry = sys.modules.get('hermes_state_registry')
            if registry:
                registry.close_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    source = args.source.resolve()
    if args.resume:
        exercise(source, args.home.resolve(), True)
        return
    with tempfile.TemporaryDirectory(prefix='hermes-compat-') as temporary:
        home = Path(temporary)
        exercise(source, home, False)
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                        '--source', str(source), '--home', str(home), '--resume'],
                       check=True, timeout=45, capture_output=True, text=True, encoding='utf-8',
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert 'PASS restart/persistent binding/history' in result.stdout, result.stdout + result.stderr
        print(result.stdout.strip(), flush=True)
    print('PASS isolated resources cleaned', flush=True)


if __name__ == '__main__':
    main()
