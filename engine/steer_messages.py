"""Group history and recipient receipts for user-authored steering messages."""
from . import db, events


def acknowledge(conn, mid, profile, status):
    if mid is None:
        return
    changed = conn.execute("UPDATE inbox SET status=?,seen_at=? WHERE message_id=? AND profile=? AND status='steering'",
                           (status, db.now_iso() if status == 'seen' else None, mid, profile)).rowcount
    return changed


def publish(mid, profile, status):
    events.bus.publish('steer.receipt', dict(message_id=mid, profile=profile, status=status))


def enrich(conn, batch, by_id):
    params = ','.join('?' for _ in batch)
    rows = conn.execute(f'SELECT i.message_id,i.profile,i.status FROM steer_messages s JOIN messages m ON m.id=s.message_id '
                        f'JOIN json_each(m.mentions) target JOIN inbox i ON i.message_id=s.message_id AND i.profile=target.value '
                        f'WHERE s.message_id IN ({params}) ORDER BY i.id', batch)
    for row in rows:
        by_id[row['message_id']].setdefault('steer_receipts', {})[row['profile']] = row['status']
