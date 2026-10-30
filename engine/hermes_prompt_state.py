"""Durable system-rule identity and admission-confirmed user-rule updates."""
import hashlib


SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_prompt_state (
    profile_id TEXT NOT NULL, session_id TEXT NOT NULL,
    system_hash TEXT, user_hash TEXT,
    PRIMARY KEY(profile_id, session_id)
);
CREATE TABLE IF NOT EXISTS hermes_prompt_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL UNIQUE REFERENCES hermes_executions(execution_id),
    profile_id TEXT NOT NULL, session_id TEXT NOT NULL, prompt_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS hermes_prompt_updates_session
ON hermes_prompt_updates(profile_id, session_id, state, id);
"""


def digest(prompt):
    return hashlib.sha256(prompt.encode('utf-8')).hexdigest()


def created(conn, profile_id, session_id, prompt):
    """Record only a confirmed creation's initial rules, keeping user updates independent."""
    conn.execute('INSERT INTO hermes_prompt_state(profile_id,session_id,system_hash) VALUES(?,?,?) '
                 'ON CONFLICT(profile_id,session_id) DO UPDATE SET '
                 'system_hash=COALESCE(hermes_prompt_state.system_hash,excluded.system_hash)',
                 (profile_id, session_id, digest(prompt)))


def pending_hash(conn, profile_id, session_id, prompt):
    row = conn.execute('SELECT system_hash,user_hash FROM hermes_prompt_state '
                       'WHERE profile_id=? AND session_id=?', (profile_id, session_id)).fetchone()
    effective = (row['user_hash'] or row['system_hash']) if row else None
    value = digest(prompt)
    return value if value != effective else None


def stage(conn, execution_id, profile_id, session_id, prompt_hash):
    if prompt_hash is not None:
        conn.execute('INSERT INTO hermes_prompt_updates(execution_id,profile_id,session_id,prompt_hash) '
                     'VALUES(?,?,?,?)', (execution_id, profile_id, session_id, prompt_hash))


def _project_latest(conn, update):
    key = (update['profile_id'], update['session_id'])
    latest = conn.execute("SELECT prompt_hash FROM hermes_prompt_updates WHERE profile_id=? AND session_id=? "
                          "AND state='accepted' ORDER BY id DESC LIMIT 1", key).fetchone()
    conn.execute('INSERT INTO hermes_prompt_state(profile_id,session_id,user_hash) VALUES(?,?,?) '
                 'ON CONFLICT(profile_id,session_id) DO UPDATE SET user_hash=excluded.user_hash',
                 (*key, latest['prompt_hash'] if latest else None))


def accepted(conn, execution_id):
    """Called in the same transaction as a validated admission receipt."""
    update = conn.execute('SELECT * FROM hermes_prompt_updates WHERE execution_id=?', (execution_id,)).fetchone()
    if update is None or update['state'] != 'pending':
        return
    conn.execute("UPDATE hermes_prompt_updates SET state='accepted' WHERE execution_id=?", (execution_id,))
    _project_latest(conn, update)


def withdraw(conn, execution_id, reason):
    """Retract an update after confirmed rewind or cancellation before execution started."""
    update = conn.execute('SELECT * FROM hermes_prompt_updates WHERE execution_id=?', (execution_id,)).fetchone()
    if update is not None:
        conn.execute('UPDATE hermes_prompt_updates SET state=? WHERE execution_id=?', (reason, execution_id))
        _project_latest(conn, update)
