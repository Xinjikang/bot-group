"""Outbound first-join previews and administrator decisions."""
from __future__ import annotations

import hmac
import json
import secrets

from . import relay

PREVIEW_TTL = 120.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_dial_attempts (
    pairing_id TEXT PRIMARY KEY,
    member TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    link_hash TEXT NOT NULL,
    candidate TEXT,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL,
    stage TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS relay_dial_attempts_expiry ON relay_dial_attempts(expires_at);
"""


def _link_hash(member, endpoint):
    return relay.hash_credential(json.dumps([member, endpoint], sort_keys=True))


def create(conn, member, endpoint, remote_expiry):
    public = {k: endpoint[k] for k in ('host', 'port', 'tls', 'fingerprint')}
    now = relay._now()
    expires = min(now + PREVIEW_TTL, remote_expiry)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM relay_dial_attempts WHERE expires_at<?', (now - PREVIEW_TTL,))
        conn.execute('UPDATE relay_dial_attempts SET candidate=NULL WHERE expires_at<=? AND candidate IS NOT NULL', (now,))
        if expires <= relay._now() or relay.remote_member_conflict(conn, member):
            return None
        attempt = secrets.token_hex(16)
        conn.execute('INSERT INTO relay_dial_attempts '
                     '(pairing_id,member,endpoint,link_hash,candidate,expires_at,state) VALUES (?,?,?,?,?,?,?)',
                     (attempt, member, json.dumps(public), _link_hash(member, endpoint),
                      secrets.token_urlsafe(32), expires, 'pending_confirmation'))
    return view(read(conn, attempt))


def read(conn, attempt):
    return conn.execute('SELECT * FROM relay_dial_attempts WHERE pairing_id=?', (attempt,)).fetchone()


def view(row):
    state = row['state']
    if state == 'pending_confirmation' and row['expires_at'] <= relay._now():
        state = 'expired'
    endpoint = json.loads(row['endpoint'])
    result = {k: row[k] for k in ('pairing_id', 'member', 'expires_at')}
    result.update(direction='dial', state=state, host=endpoint['host'], port=endpoint['port'],
                  tls=True, fingerprint_short=endpoint['fingerprint'][:8])
    if state == 'failed':
        result.update(stage=row['stage'], reason=row['reason'])
    return result


def matches(row, member, endpoint):
    return hmac.compare_digest(row['link_hash'], _link_hash(member, endpoint))


def decide(conn, attempt, *, cancel=False, failure=None):
    """No network under the write lock. Only the selected preview can be promoted."""
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = read(conn, attempt)
        if row is None:
            return None, False
        current = view(row)
        if current['state'] != 'pending_confirmation':
            return current, False
        member = row['member']
        stage, reason = failure or (None, None)
        if cancel:
            state = 'cancelled'
        elif failure:
            state = 'failed'
        elif relay.remote_member_conflict(conn, member):
            state, stage, reason = 'failed', 'confirmation', 'name_conflict'
        else:
            key = row['candidate']
            endpoint = dict(json.loads(row['endpoint']), pairing_token=key, activate_pairing=True)
            if relay.register_remote_node(conn, member, key, direction='dial', endpoint=endpoint, commit=False) is None:
                raise RuntimeError('verified outbound identity changed inside the transaction')
            state = 'confirmed'
        conn.execute('UPDATE relay_dial_attempts SET state=?,stage=?,reason=?,candidate=NULL WHERE pairing_id=?',
                     (state, stage, reason, attempt))
        return view(read(conn, attempt)), state == 'confirmed'
