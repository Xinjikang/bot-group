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


def _expire(conn, now, attempt=None):
    sql = "UPDATE relay_dial_attempts SET state='expired',candidate=NULL " \
          "WHERE state='pending_confirmation' AND expires_at<=?"
    if attempt is None:
        conn.execute(sql, (now,))
    else:
        conn.execute(sql + ' AND pairing_id=?', (now, attempt))


def expire_stale(conn):
    """Record expiry even if no administrator returns to the pairing screen."""
    now = relay._now()
    if conn.execute("SELECT 1 FROM relay_dial_attempts WHERE state='pending_confirmation' "
                    'AND expires_at<=? LIMIT 1', (now,)).fetchone():
        with conn:
            _expire(conn, now)
    # A registered node may be offline, with no dial worker left to clear its ticket.
    stale_tickets = conn.execute(
        "SELECT member,dial_endpoint FROM relay_nodes WHERE dial_endpoint LIKE '%\"activation_ticket\"%'"
    ).fetchall()
    for member, saved in stale_tickets:
        endpoint = json.loads(saved)
        if endpoint.get('activation_expires_at', 0) <= now:
            relay.discard_dial_activation_ticket(conn, member, endpoint, saved)


def create(conn, member, endpoint, remote_expiry):
    public = {k: endpoint[k] for k in ('host', 'port', 'tls', 'fingerprint')}
    now = relay._now()
    expires = min(now + PREVIEW_TTL, remote_expiry)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM relay_dial_attempts WHERE expires_at<?', (now - PREVIEW_TTL,))
        _expire(conn, now)
        if expires <= relay._now() or relay.remote_member_conflict(conn, member):
            return None
        attempt = secrets.token_hex(16)
        conn.execute('INSERT INTO relay_dial_attempts '
                     '(pairing_id,member,endpoint,link_hash,expires_at,state) VALUES (?,?,?,?,?,?)',
                     (attempt, member, json.dumps(public), _link_hash(member, endpoint), expires,
                      'pending_confirmation'))
    return view(read(conn, attempt))


def read(conn, attempt):
    return conn.execute('SELECT * FROM relay_dial_attempts WHERE pairing_id=?', (attempt,)).fetchone()


def read_for_confirm(conn, attempt):
    row = read(conn, attempt)
    now = relay._now()
    if row is not None and row['state'] == 'pending_confirmation' and row['expires_at'] <= now:
        with conn:
            _expire(conn, now, attempt)
        row = read(conn, attempt)
    return row, now


def view(row, *, now=None):
    state = row['state']
    if state == 'pending_confirmation' and row['expires_at'] <= (relay._now() if now is None else now):
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


def decide(conn, attempt, *, cancel=False, failure=None, verified=None):
    """Only a still-valid, rechecked preview can be promoted inside the write lock."""
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        _expire(conn, relay._now(), attempt)
        row = read(conn, attempt)
        if row is None:
            return None, False
        if row['state'] != 'pending_confirmation':
            return view(row), False
        member = row['member']
        stage, reason = failure or (None, None)
        if cancel:
            state = 'cancelled'
        elif failure:
            state = 'failed'
        elif verified is None or not matches(row, verified[0], verified[1]):
            raise ValueError('approval requires verification of the original link')
        elif relay.remote_member_conflict(conn, member):
            state, stage, reason = 'failed', 'confirmation', 'name_conflict'
        elif min(row['expires_at'], verified[2]) <= relay._now():
            state = 'expired'
        else:
            key = secrets.token_urlsafe(32)
            endpoint = dict(json.loads(row['endpoint']), pairing_token=key, activate_pairing=True,
                            activation_ticket=verified[1]['pairing_token'], activation_expires_at=verified[2])
            if relay.register_remote_node(conn, member, key, direction='dial', endpoint=endpoint, commit=False) is None:
                raise RuntimeError('verified outbound identity changed inside the transaction')
            state = 'confirmed'
        conn.execute('UPDATE relay_dial_attempts SET state=?,stage=?,reason=?,candidate=NULL WHERE pairing_id=?',
                     (state, stage, reason, attempt))
        return view(read(conn, attempt)), state == 'confirmed'
